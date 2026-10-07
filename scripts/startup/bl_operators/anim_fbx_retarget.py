# SPDX-FileCopyrightText: 2026 Blender Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

"""
Import animation from FBX files onto the active armature.

Each file is imported with the built-in FBX importer into temporary objects, the
animation is transferred onto the target armature by matching bone names (so that
every bone matches its source pose in world space), and the temporary data is removed.
"""

from __future__ import annotations

import math
import os
import re

import bpy
from bpy.types import (
    Operator,
    OperatorFileListElement,
)
from bpy.props import (
    BoolProperty,
    CollectionProperty,
    EnumProperty,
    StringProperty,
)
from bpy_extras import anim_utils
from bpy_extras.io_utils import ImportHelper
from mathutils import (
    Euler,
    Matrix,
    Quaternion,
    Vector,
)

# Data-block collections that are checked for temporary data created by the FBX importer.
_TEMP_ID_COLLECTIONS = (
    "objects",
    "meshes",
    "armatures",
    "materials",
    "images",
    "textures",
    "cameras",
    "lights",
    "curves",
    "collections",
    "actions",
)

# Mapping value for a target bone driven by the source armature object itself (bone names are never empty).
# The FBX importer turns a root bone at the top of the hierarchy (``root`` in Unreal Engine files)
# into the armature object, so its animation is on the object and not on a bone.
_SOURCE_OBJECT = ""

# Bone names that are recognized as the hips when the hips bone is not given explicitly.
_HIPS_NAMES = ("pelvis", "hips", "hip")


def _ids_snapshot():
    return {
        attr: {id_data.session_uid for id_data in getattr(bpy.data, attr)}
        for attr in _TEMP_ID_COLLECTIONS
    }


def _ids_new_since(snapshot):
    return {
        attr: [id_data for id_data in getattr(bpy.data, attr) if id_data.session_uid not in snapshot[attr]]
        for attr in _TEMP_ID_COLLECTIONS
    }


def _short_name(name):
    """Bone name without a namespace prefix (``mixamorig:Hips`` -> ``Hips``)."""
    for sep in (":", "|"):
        name = name.rpartition(sep)[2]
    return name.lower()


def _bones_hierarchy_order(armature):
    """Bones of the armature, every parent before its children."""
    order = []
    stack = [bone for bone in reversed(armature.bones) if bone.parent is None]
    while stack:
        bone = stack.pop()
        order.append(bone)
        stack.extend(reversed(bone.children))
    return order


def _bone_mapping(target_arm, source):
    """Map target bone names to source bone names: exact match first, then without namespace and case."""
    source_arm = source.data
    source_names = [bone.name for bone in source_arm.bones]
    source_exact = set(source_names)
    source_short = {}
    for name in source_names:
        source_short.setdefault(_short_name(name), name)

    mapping = {}
    for bone in target_arm.bones:
        if bone.name in source_exact:
            mapping[bone.name] = bone.name
        else:
            src_name = source_short.get(_short_name(bone.name))
            if src_name is not None:
                mapping[bone.name] = src_name

    # A root bone that became the armature object: match by object name (without a ".001" suffix).
    source_object_name = _short_name(re.sub(r"\.\d{3,}$", "", source.name))
    for bone in target_arm.bones:
        if bone.parent is None and bone.name not in mapping and _short_name(bone.name) == source_object_name:
            mapping[bone.name] = _SOURCE_OBJECT
            break
    return mapping


def _find_hips(target_arm):
    """Hips bone of the target armature: by name, otherwise the first branching bone below a root."""
    for bone in _bones_hierarchy_order(target_arm):
        if _short_name(bone.name) in _HIPS_NAMES:
            return bone
    for root in target_arm.bones:
        if root.parent is not None:
            continue
        bone = root
        while len(bone.children) == 1:
            bone = bone.children[0]
        if bone.children:
            return bone
    return None


def _location_bone_names(target_arm, hips_name):
    """Roots, the hips bone and every bone in between."""
    names = {bone.name for bone in target_arm.bones if bone.parent is None}
    hips = target_arm.bones.get(hips_name) if hips_name else _find_hips(target_arm)
    while hips is not None:
        names.add(hips.name)
        hips = hips.parent
    return names


def _without_connected(target_arm, names):
    return {name for name in names if not target_arm.bones[name].use_connect}


def _slot_for_object(action, obj):
    identifier = "OB" + obj.name
    for slot in action.slots:
        if slot.identifier == identifier:
            return slot
    anim_data = obj.animation_data
    if anim_data and anim_data.action == action:
        return anim_data.action_slot
    return None


def _fcurve_map(action, obj):
    slot = _slot_for_object(action, obj)
    channelbag = anim_utils.action_get_channelbag_for_slot(action, slot)
    if channelbag is None:
        return None
    return {(fcu.data_path, fcu.array_index): fcu for fcu in channelbag.fcurves}


def _eval_channel(fcurves, data_path, default, frame):
    result = []
    for index, value in enumerate(default):
        fcu = fcurves.get((data_path, index))
        result.append(fcu.evaluate(frame) if fcu is not None else value)
    return result


def _eval_basis(fcurves, prefix, owner, frame):
    """Evaluate the local transform of an object or pose bone from F-Curves (falling back to current values)."""
    loc = _eval_channel(fcurves, prefix + "location", owner.location, frame)
    scale = _eval_channel(fcurves, prefix + "scale", owner.scale, frame)
    rotation_mode = owner.rotation_mode
    if rotation_mode == 'QUATERNION':
        quat = Quaternion(_eval_channel(fcurves, prefix + "rotation_quaternion", owner.rotation_quaternion, frame))
        if quat.magnitude < 1e-8:
            quat = Quaternion()
        rot = quat.normalized().to_matrix()
    elif rotation_mode == 'AXIS_ANGLE':
        angle, *axis = _eval_channel(fcurves, prefix + "rotation_axis_angle", owner.rotation_axis_angle, frame)
        axis = Vector(axis)
        rot = Matrix.Rotation(angle, 3, axis.normalized()) if axis.length > 1e-8 else Matrix.Identity(3)
    else:
        rot = Euler(_eval_channel(fcurves, prefix + "rotation_euler", owner.rotation_euler, frame),
                    rotation_mode).to_matrix()
    return Matrix.LocRotScale(Vector(loc), rot, Vector(scale))


def _object_world_matrix(obj, frame, fcurve_maps):
    fcurves = fcurve_maps.get(obj.name)
    basis = _eval_basis(fcurves, "", obj, frame) if fcurves else obj.matrix_basis.copy()
    parent = obj.parent
    if parent is None:
        return basis
    if obj.parent_type == 'OBJECT':
        return _object_world_matrix(parent, frame, fcurve_maps) @ obj.matrix_parent_inverse @ basis
    # Parenting to bones/vertices is not expected in imported FBX files: use the static transform.
    return obj.matrix_world.copy()


def _pose_matrix(bone, basis, pose_matrices):
    parent = bone.parent
    if bone.use_connect:
        # Location of connected bones is ignored by Blender.
        basis = basis.copy()
        basis.translation = (0.0, 0.0, 0.0)
    if parent is None:
        return bone.convert_local_to_pose(basis, bone.matrix_local)
    return bone.convert_local_to_pose(
        basis,
        bone.matrix_local,
        parent_matrix=pose_matrices[parent.name],
        parent_matrix_local=parent.matrix_local,
    )


def _basis_from_pose(bone, pose, pose_matrices):
    parent = bone.parent
    if parent is None:
        return bone.convert_local_to_pose(pose, bone.matrix_local, invert=True)
    return bone.convert_local_to_pose(
        pose,
        bone.matrix_local,
        parent_matrix=pose_matrices[parent.name],
        parent_matrix_local=parent.matrix_local,
        invert=True,
    )


def _frame_range(fcurve_maps):
    start = math.inf
    end = -math.inf
    for fcurves in fcurve_maps.values():
        for fcu in fcurves.values():
            if not fcu.keyframe_points:
                continue
            fcu_start, fcu_end = fcu.range()
            start = min(start, fcu_start)
            end = max(end, fcu_end)
    if start > end:
        return None
    return math.floor(start + 1e-4), math.ceil(end - 1e-4)


def _write_fcurve(channelbag, data_path, index, group_name, frames, values):
    fcu = channelbag.fcurves.new(data_path, index=index, group_name=group_name)
    points = fcu.keyframe_points
    points.add(len(frames))
    points.foreach_set("co", [c for co in zip(frames, values) for c in co])
    fcu.update()


class ANIM_OT_fbx_animation_to_armature(Operator, ImportHelper):
    """Import animation from FBX files onto the active armature, matching bones by name"""

    bl_idname = "anim.fbx_animation_to_armature"
    bl_label = "Import FBX Animation to Armature"
    bl_options = {'REGISTER', 'UNDO'}

    filename_ext = ".fbx"
    filter_glob: StringProperty(default="*.fbx", options={'HIDDEN'})
    files: CollectionProperty(type=OperatorFileListElement, options={'HIDDEN', 'SKIP_SAVE'})
    directory: StringProperty(subtype='DIR_PATH', options={'HIDDEN', 'SKIP_SAVE'})

    location_mode: EnumProperty(
        name="Location",
        description="Which bones receive location animation",
        items=(
            ('ROOT_HIPS', "Root and Hips",
             "Transfer location only for root bones and the hips (safe for different proportions)"),
            ('ALL', "All Bones", "Transfer location for every bone"),
        ),
        default='ROOT_HIPS',
    )
    hips_bone: StringProperty(
        name="Hips Bone",
        description="Hips bone of the target armature (leave empty to detect automatically)",
    )
    use_scale: BoolProperty(
        name="Scale",
        description="Transfer scale animation",
        default=False,
    )

    @staticmethod
    def _target(context):
        obj = context.active_object
        return obj if (obj is not None and obj.type == 'ARMATURE') else None

    def draw(self, context):
        layout = self.layout
        layout.use_property_split = True
        layout.use_property_decorate = False
        layout.prop(self, "location_mode")
        target = self._target(context)
        row = layout.row()
        row.active = self.location_mode == 'ROOT_HIPS'
        if target is not None:
            row.prop_search(self, "hips_bone", target.data, "bones")
        else:
            row.prop(self, "hips_bone")
        layout.prop(self, "use_scale")

    def invoke(self, context, event):
        if self._target(context) is None:
            self.report({'ERROR'}, "Select the target armature (it must be the active object)")
            return {'CANCELLED'}
        return ImportHelper.invoke(self, context, event)

    def execute(self, context):
        target = self._target(context)
        if target is None:
            self.report({'ERROR'}, "Select the target armature (it must be the active object)")
            return {'CANCELLED'}
        if not bpy.app.build_options.io_fbx:
            self.report({'ERROR'}, "This build of Blender has no FBX importer")
            return {'CANCELLED'}
        if self.hips_bone and self.hips_bone not in target.data.bones:
            self.report({'ERROR'}, "Bone \"{:s}\" not found in the target armature".format(self.hips_bone))
            return {'CANCELLED'}

        if self.files and self.files[0].name:
            filepaths = [os.path.join(self.directory, f.name) for f in self.files]
        else:
            filepaths = [self.filepath]

        view_layer = context.view_layer
        selected_names = [obj.name for obj in context.selected_objects]
        prev_mode = target.mode
        if prev_mode != 'OBJECT':
            bpy.ops.object.mode_set(mode='OBJECT')

        results = []
        skipped_bones = set()
        try:
            for filepath in filepaths:
                try:
                    results.extend(self._import_file(context, target, filepath, skipped_bones))
                except RuntimeError as ex:
                    self.report({'WARNING'}, "{:s}: {:s}".format(os.path.basename(filepath), str(ex)))
        finally:
            for obj in context.selected_objects:
                obj.select_set(False)
            for name in selected_names:
                obj = bpy.data.objects.get(name)
                if obj is not None and obj.name in view_layer.objects:
                    obj.select_set(True)
            view_layer.objects.active = target
            if prev_mode != 'OBJECT':
                bpy.ops.object.mode_set(mode=prev_mode)

        if not results:
            self.report({'ERROR'}, "No animation was imported")
            return {'CANCELLED'}

        action, slot = results[-1]
        anim_data = target.animation_data or target.animation_data_create()
        anim_data.action = action
        anim_data.action_slot = slot

        if skipped_bones:
            names = sorted(skipped_bones)
            text = ", ".join(names[:30]) + (" ..." if len(names) > 30 else "")
            self.report({'WARNING'}, "Bones not found in \"{:s}\" ({:d}): {:s}".format(
                target.name, len(names), text))
        self.report({'INFO'}, "Imported {:d} action(s): {:s}".format(
            len(results), ", ".join(action.name for action, _slot in results)))
        return {'FINISHED'}

    def _import_file(self, context, target, filepath, skipped_bones):
        """Import one FBX file, return a list of (action, slot) created for the target."""
        snapshot = _ids_snapshot()
        temp_ids = None
        try:
            result = bpy.ops.wm.fbx_import(filepath=filepath, use_anim=True)
            temp_ids = _ids_new_since(snapshot)
            if 'FINISHED' not in result:
                raise RuntimeError("FBX import failed")

            # The source armature is the one sharing the most bone names with the target.
            source = None
            mapping = {}
            for obj in temp_ids["objects"]:
                if obj.type != 'ARMATURE':
                    continue
                obj_mapping = _bone_mapping(target.data, obj)
                if source is None or len(obj_mapping) > len(mapping):
                    source, mapping = obj, obj_mapping
            if source is None:
                raise RuntimeError("no armature in file")
            if not mapping:
                raise RuntimeError("no bones with matching names")

            mapped_sources = set(mapping.values())
            skipped_bones.update(bone.name for bone in source.data.bones if bone.name not in mapped_sources)

            # Every action of the file containing animation for the source armature is a take.
            takes = []
            for action in temp_ids["actions"]:
                if _fcurve_map(action, source):
                    takes.append(action)
            if not takes:
                raise RuntimeError("no animation for armature \"{:s}\"".format(source.name))

            base_name = os.path.splitext(os.path.basename(filepath))[0]
            if len(takes) == 1:
                names = [base_name]
            else:
                names = ["{:s}|{:s}".format(base_name, action.name) for action in takes]
            # Free the names of the temporary actions, so the new actions don't get a ".001" suffix.
            for action in temp_ids["actions"]:
                action.name = "_fbx_import_temp"
            results = []
            for action, name in zip(takes, names):
                results.append(self._transfer_take(target, source, mapping, action, name))
            return results
        finally:
            # Only data created by the importer, not the actions created for the target.
            if temp_ids is None:
                temp_ids = _ids_new_since(snapshot)
            bpy.data.batch_remove([id_data for ids in temp_ids.values() for id_data in ids])

    def _transfer_take(self, target, source, mapping, source_action, name):
        # Animation of the source armature object, its bones and its parent objects.
        fcurve_maps = {}
        obj = source
        while obj is not None:
            fcurves = _fcurve_map(source_action, obj)
            if fcurves:
                fcurve_maps[obj.name] = fcurves
            obj = obj.parent
        frame_range = _frame_range(fcurve_maps)
        if frame_range is None:
            frame_range = (0, 0)
        frames = range(frame_range[0], frame_range[1] + 1)
        source_fcurves = fcurve_maps.get(source.name, {})

        source_order = _bones_hierarchy_order(source.data)
        target_order = _bones_hierarchy_order(target.data)
        if self.location_mode == 'ALL':
            location_bones = {bone.name for bone in target_order}
        else:
            location_bones = _location_bone_names(target.data, self.hips_bone)
        location_bones = _without_connected(target.data, location_bones)

        target_world_inv = target.matrix_world.inverted_safe()
        source_prefix = {
            bone.name: "pose.bones[\"{:s}\"].".format(bpy.utils.escape_identifier(bone.name))
            for bone in source_order
        }

        # Per mapped target bone: channel name -> list of per-frame value tuples.
        keys = {bone_name: {"location": [], "rotation": [], "scale": []} for bone_name in mapping}
        prev_rotation = {}

        for frame in frames:
            source_world = _object_world_matrix(source, frame, fcurve_maps)
            source_pose = {}
            for bone in source_order:
                basis = _eval_basis(source_fcurves, source_prefix[bone.name], source.pose.bones[bone.name], frame)
                source_pose[bone.name] = _pose_matrix(bone, basis, source_pose)

            target_pose = {}
            for bone in target_order:
                pose_bone = target.pose.bones[bone.name]
                source_name = mapping.get(bone.name)
                if source_name is None:
                    basis = pose_bone.matrix_basis.copy()
                else:
                    if source_name == _SOURCE_OBJECT:
                        pose = target_world_inv @ source_world
                    else:
                        pose = target_world_inv @ source_world @ source_pose[source_name]
                    loc, rot, scale = _basis_from_pose(bone, pose, target_pose).decompose()
                    if bone.name not in location_bones:
                        loc = Vector((0.0, 0.0, 0.0))
                    if not self.use_scale:
                        scale = Vector((1.0, 1.0, 1.0))
                    basis = Matrix.LocRotScale(loc, rot, scale)

                    rotation_mode = pose_bone.rotation_mode
                    prev = prev_rotation.get(bone.name)
                    if rotation_mode == 'QUATERNION':
                        if prev is not None and rot.dot(prev) < 0.0:
                            rot.negate()
                        value = tuple(rot)
                    elif rotation_mode == 'AXIS_ANGLE':
                        if prev is not None and rot.dot(prev) < 0.0:
                            rot.negate()
                        axis, angle = rot.to_axis_angle()
                        value = (angle, *axis)
                    else:
                        euler = rot.to_euler(rotation_mode, prev) if prev is not None else rot.to_euler(rotation_mode)
                        value = tuple(euler)
                        rot = euler
                    prev_rotation[bone.name] = rot

                    channels = keys[bone.name]
                    channels["location"].append(tuple(loc))
                    channels["rotation"].append(value)
                    channels["scale"].append(tuple(scale))
                target_pose[bone.name] = _pose_matrix(bone, basis, target_pose)

        action = bpy.data.actions.new(name)
        action.use_fake_user = True
        slot = action.slots.new(id_type='OBJECT', name=target.name)
        channelbag = anim_utils.action_ensure_channelbag_for_slot(action, slot)

        rotation_paths = {'QUATERNION': "rotation_quaternion", 'AXIS_ANGLE': "rotation_axis_angle"}
        frame_list = list(frames)
        for bone in target_order:
            channels = keys.get(bone.name)
            if channels is None:
                continue
            pose_bone = target.pose.bones[bone.name]
            prefix = "pose.bones[\"{:s}\"].".format(bpy.utils.escape_identifier(bone.name))
            written = [("rotation", rotation_paths.get(pose_bone.rotation_mode, "rotation_euler"))]
            if bone.name in location_bones:
                written.append(("location", "location"))
            else:
                pose_bone.location = (0.0, 0.0, 0.0)
            if self.use_scale:
                written.append(("scale", "scale"))
            else:
                pose_bone.scale = (1.0, 1.0, 1.0)
            for channel, prop in written:
                values = channels[channel]
                for index in range(len(values[0])):
                    _write_fcurve(channelbag, prefix + prop, index, bone.name,
                                  frame_list, [value[index] for value in values])
        return action, slot


classes = (
    ANIM_OT_fbx_animation_to_armature,
)
