# Blendlight — форк Blender с майскими джоинтами

Общение с пользователем — на русском.

## Цель
Свой форк Blender, в котором кости в режиме отображения **Octahedral** выглядят как джоинты Maya:
контур сферы в head кости + проволочная пирамидка от джоинта к head каждой дочерней кости.
Всё живёт внутри существующего режима Octahedral. Отдельный пункт Display As («Joint»), правки DNA/RNA
и UI пользователь пробовал и отказался — не предлагать заново без запроса.

## Окружение
- Windows 10, Visual Studio 2022 Community, компилятор MSVC 14.44
  (Blender требует ≥ 17.14.14; старый набор 14.38 удалён из установки VS).
- Репозиторий: `C:\blender-git\blendlight`, клон с `https://projects.blender.org/blender/blender.git`
  (не с GitHub-зеркала: с зеркала не скачиваются LFS-файлы и библиотеки `lib/windows_x64`).
- Рабочая ветка `joint-display`, создана от `blender-v5.2-release`. Пушить в `origin` нельзя (это сервер Blender).
- Git-клиент пользователя — GitHub Desktop.

## Сборка
- Команда: `make` из `C:\blender-git\blendlight`, **только в cmd** (не PowerShell; из VS Code — `cmd /c make`).
- Сборка инкрементальная: пересобираются только изменённые файлы. Правка заголовков DNA/ED_* ведёт к долгой пересборке.
- Результат: `C:\blender-git\build_windows_..._Release\bin\Release\blender.exe`.
- Перед сборкой собранный Blender должен быть закрыт (иначе ошибка записи exe / LNK1104).
- Успех — в конце вывода `-- Installing: ...`. При ошибке смотреть первую строку с `error`.
- Не использовать CMake Tools в VS Code и «Open a local folder» в Visual Studio — они конфигурируют сборку сами.
  Для Visual Studio открывать `Blender.sln` из папки сборки.

## Как работать с пользователем
- Пошагово, сразу предупреждать о подводных камнях и о том, что может пойти не так.
- Перед каждой правкой кратко объяснять, что и зачем меняется.
- Пользователь — аниматор/риггер из Maya, разработчиком C++ не является.

## Состояние репозитория (на 2026-10-07)
История ветки `joint-display`: один коммит `joints` (включал больше, чем нужно) и затем
`Revert "joints"`. **Сейчас код полностью совпадает с оригинальным Blender 5.2** — майских джоинтов в нём нет.

Целевое состояние, к которому пользователь хочет вернуться, — «майский Octahedral»: только
Object / Pose / Weight Paint, Edit Mode рисуется стандартно, никаких правок кроме `overlay_armature.cc`.
Эта реализация ранее собиралась и работала; эталонный код — ниже.

## Эталонная реализация «майского Octahedral»
Файл: `source/blender/draw/engines/overlay/overlay_armature.cc`.

### 1. Блок перед функцией `static void bone_draw(`
```cpp
/* -------------------------------------------------------------------- */
/** \name Maya-style joints
 * \{ */

/* Радиус сферы джоинта в единицах арматуры (аналог Joint Size в Maya). */
static constexpr float MAYA_JOINT_RADIUS = 0.03f;
/* Минимальная толщина линий: без неё невыделенный скелет в Object Mode невидим
 * (у невыделенной арматуры ctx->const_wire == 0). */
static constexpr float MAYA_JOINT_MIN_WIRE = 1.0f;

/* Базис сегмента: dir — направление на дочерний джоинт, X и Z — перпендикуляры к нему. */
static void maya_segment_basis(const float4x4 &joint_mat,
                               const float3 &dir,
                               float3 &r_x,
                               float3 &r_z)
{
  float3 x = math::normalize(joint_mat.x_axis());
  x -= dir * math::dot(x, dir);
  if (math::length_squared(x) < 1e-8f) {
    ortho_v3_v3(x, dir);
  }
  r_x = math::normalize(x);
  r_z = math::cross(r_x, dir);
}

static void bone_draw_maya_joint(const Armatures::DrawContext *ctx,
                                 const UnifiedBonePtr bone,
                                 const eBone_Flag boneflag,
                                 const int select_id)
{
  const bPoseChannel *pchan = bone.as_posebone();
  const float *col_solid = get_bone_solid_with_consts_color(ctx, bone, boneflag);

  float col_wire[4];
  copy_v4_v4(col_wire, get_bone_wire_color(ctx, boneflag));
  col_wire[3] = max_ff(col_wire[3], MAYA_JOINT_MIN_WIRE);

  auto sel_id = ctx->res->select_id(*ctx->ob_ref, select_id | BONESEL_BONE);
  const bool is_select = ctx->res->is_selection();
  const float4x4 &obmat = ctx->ob->object_to_world();
  const float r = MAYA_JOINT_RADIUS;

  const float4x4 joint_mat = float4x4(pchan->pose_mat);
  const float3 head = joint_mat.location();

  /* 1. Сфера джоинта: только контур (в единичном пространстве радиус 0.05). */
  float4x4 sphere_mat = float4x4::identity();
  sphere_mat.x_axis() *= r * 20.0f;
  sphere_mat.y_axis() *= r * 20.0f;
  sphere_mat.z_axis() *= r * 20.0f;
  sphere_mat.location() = head;
  sphere_mat = obmat * sphere_mat;

  ctx->bone_buf->sphere_outline_buf.append({sphere_mat, col_wire}, sel_id);
  if (is_select) {
    /* Невидимая заливка только для клика. */
    ctx->bone_buf->sphere_fill_buf.append({sphere_mat, col_solid, col_solid}, sel_id);
  }

  /* 2. Пирамидки к каждому видимому дочернему джоинту. */
  for (bPoseChannel *child : ListBaseWrapper<bPoseChannel>(ctx->ob->pose->chanbase)) {
    if (child->parent != pchan) {
      continue;
    }
    if (!animrig::bone_is_visible(ctx->armature, {child, child->bone_get(*ctx->ob)})) {
      continue;
    }
    const float3 child_head = float3(child->pose_mat[3]);
    float3 dir = child_head - head;
    const float len = math::length(dir);
    if (len < 2.0f * r) {
      continue; /* Джоинты почти совпадают. */
    }
    dir /= len;

    float3 x, z;
    maya_segment_basis(joint_mat, dir, x, z);

    /* Вершина пирамидки упирается в сферу дочернего джоинта. */
    const float3 apex = math::transform_point(obmat, child_head - dir * r);
    const float3 base[4] = {head + x * r, head + z * r, head - x * r, head - z * r};
    for (const float3 &b : base) {
      ctx->bone_buf->wire_buf.append(
          math::transform_point(obmat, b), apex, float4(col_wire), sel_id);
    }

    if (is_select) {
      /* Невидимый октаэдр вдоль сегмента (полуширина октаэдра 0.1), чтобы кликать по всей пирамидке. */
      float4x4 seg_mat = float4x4::identity();
      seg_mat.x_axis() = x * (r * 10.0f);
      seg_mat.y_axis() = dir * len;
      seg_mat.z_axis() = z * (r * 10.0f);
      seg_mat.location() = head;
      ctx->bone_buf->octahedral_fill_buf.append({obmat * seg_mat, col_solid, col_solid},
                                                sel_id);
    }
  }
}

/** \} */
```

### 2. В функции `bone_draw`
```cpp
    case ARM_DRAW_TYPE_OCTA:
      if (bone.is_posebone()) {
        bone_draw_maya_joint(ctx, bone, boneflag, select_id);
      }
      else {
        bone_draw_octa(ctx, bone, boneflag, select_id);
      }
      break;
```

После восстановления — сразу закоммитить (например, «Maya-style joints in Octahedral (pose/object)»),
чтобы у этого этапа была собственная точка отката.

## Полезные места в коде
- Отрисовка костей: `draw/engines/overlay/overlay_armature.cc`; `Armatures::DrawContext`
  и буферы (`sphere_*_buf`, `octahedral_*_buf`, `wire_buf`) — в `overlay_armature.hh`.
- Шейдеры костей: `draw/engines/overlay/shaders/overlay_armature_*.glsl`
  (заливка смешивает основной и hint-цвет по нормали — отсюда «грани»; поэтому рисуем только контур).
- Типы и флаги арматуры: `makesdna/DNA_armature_types.h` (`eArmature_Drawtype`, `BONE_*`).

## Идеи на будущее (не начаты)
- Майские цвета джоинтов (синий в покое, зелёный/белый при выделении) независимо от темы.
- Радиус джоинтов без пересборки (например, через custom property арматуры).

## Импорт FBX-анимации на арматуру (2026-10-08)
- Оператор `anim.fbx_animation_to_armature`, меню File → Import → «FBX Animation to Selected Armature».
- Код: `scripts/startup/bl_operators/anim_fbx_retarget.py` (зарегистрирован в `bl_operators/__init__.py`),
  пункт меню — `TOPBAR_MT_file_import` в `scripts/startup/bl_ui/space_topbar.py`.
- Импорт через штатный C++ `wm.fbx_import`, перенос — расчётом матриц по F-кривым (без frame_set и bake).
  Совпадение мировой позы, кости по именам (точно, затем без namespace и регистра).
- Location: «Root and Hips» (по умолчанию; таз ищется по имени pelvis/hips, иначе первая ветвящаяся кость,
  можно указать вручную) или «All Bones». У connected-костей location не переносится (Blender его игнорирует).
- Правка только Python — для обновления в сборке достаточно `make` (C++ не пересобирается).
- Особенность C++ FBX-импортёра: если верхний узел скелета — кость (`root` в экспорте из UE), он становится
  **объектом арматуры**, а не костью; его анимация — на объекте. Оператор сопоставляет такой объект
  с корневой костью цели по имени объекта (без суффикса `.001`).
- Временные Actions импортёра переименовываются перед созданием результата — иначе имя получало `.001`.

## FBX-экспорт под Maya (2026-10-08)
Правится штатный экспортёр `scripts/addons_core/io_scene_fbx` (оператор `export_scene.fbx`, File → Export → FBX).
- Зафиксированы и скрыты из окна экспорта (`HIDDEN` + `SKIP_SAVE`, из Python задать можно):
  Forward -Z / Up Y, Apply Scalings = FBX Units Scale, Add Leaf Bones = выкл, Key All Bones = вкл,
  Force Start/End Keying = вкл, Simplify = 0, Armature FBXNode Type = `NONE`.
- `NONE` — новый режим: у арматуры нет своего узла в FBX, корневые кости на верхнем уровне файла.
  Реализация — `ObjectWrapper.is_armature_without_node()` в `fbx_utils.py` и проверки в `export_fbx_bin.py`
  (модель, Null-атрибут, связи, BindPose, анимация). Движение объекта арматуры запекается в корневые кости
  и в детей арматуры.
- Проверено реимпортом: позы костей, скиннутый меш и дети арматуры совпадают с оригиналом.
- Имя тейка в режиме All Actions — чистое имя Action (было `Объект|Action`), см. `fbx_animations_do`.
- Опция **Action Per File** (`use_action_files`): каждый Action, подходящий экспортируемым объектам, — отдельный
  файл `<имя Action>.fbx` в папке из пути (имя файла игнорируется; недопустимые символы `<>:"/\|?*` → `_`).
  Реализация — `save_action_files()` и фильтр `bake_anim_only_action` в настройках экспортёра.
