# Blendlight — форк Blender 5.2 под Maya-пайплайн

Общение с пользователем — на русском. Пользователь — аниматор/риггер из Maya, не C++-разработчик:
вести пошагово, перед правкой кратко объяснять что и зачем, сразу предупреждать о подводных камнях.

## Окружение и git
- Windows 10, VS 2022 Community, MSVC 14.44 (Blender требует ≥ 17.14.14).
- Репозиторий `C:\blender-git\blendlight` — клон `projects.blender.org/blender/blender.git`
  (не GitHub-зеркало: там нет LFS и `lib/windows_x64`). Ветка `joint-display` от `blender-v5.2-release`.
- **Пушить в `origin` нельзя** (сервер Blender). Git-клиент пользователя — GitHub Desktop; коммитить только по просьбе.

## Сборка
- `make` из корня репозитория, **только в cmd** (из VS Code — `cmd /c make`). Не использовать CMake Tools
  и «Open a local folder» в VS; для Visual Studio — `Blender.sln` из папки сборки.
- Результат: `C:\blender-git\build_windows_x64_vc17_Release\bin\Release\blender.exe`.
- LNK1104 на `blender.exe` = собранный Blender запущен; закрыть (проверить несохранённое) и пересобрать.
- Успех — `-- Installing: ...` в конце. Правка заголовков DNA/ED_* → долгая пересборка;
  правка только Python → `make` быстрый (скрипты копируются в сборку).

## Тестирование без UI
`blender.exe -b --factory-startup --python-exit-code 1 --python test.py -- <args>` (скрипты — в scratchpad).
- Модуль из `scripts/startup/...` грузить через `importlib` и добавлять в `sys.modules` до `exec_module`
  (иначе ломаются аннотации `from __future__ import annotations`).
- `io_scene_fbx`: сначала `addon_utils.disable("io_scene_fbx")`, затем загрузить пакет из `scripts/addons_core`
  и вызвать `register()` — иначе используется копия из папки сборки. `BLENDER_SYSTEM_SCRIPTS` для addons_core не работает.
- Структуру FBX смотреть через `scripts/addons_core/io_scene_fbx/fbx2json.py`.

## 1. Джоинты Maya в режиме Octahedral
`source/blender/draw/engines/overlay/overlay_armature.cc`: `bone_draw_maya_joint()` (блок «Maya-style joints»
перед `bone_draw`), вызов в `case ARM_DRAW_TYPE_OCTA` только для pose-костей (Object/Pose/Weight Paint);
Edit Mode — стандартный октаэдр. Контур сферы в head + проволочные пирамидки к head дочерних костей,
невидимые заливки — для выделения кликом. Радиус — `MAYA_JOINT_RADIUS` (сейчас 0.1).
- Точка отката: коммит `356c0bb`. Правки только в этом файле.
- Отдельный Display As «Joint», правки DNA/RNA/UI пользователь пробовал и отказался — не предлагать без запроса.
- Заливку не используем: шейдер смешивает основной и hint-цвет по нормали → «грани».
- Идеи (не начаты): майские цвета джоинтов независимо от темы; радиус через custom property арматуры.

## 2. Импорт FBX-анимации на арматуру
`scripts/startup/bl_operators/anim_fbx_retarget.py`, оператор `anim.fbx_animation_to_armature`,
File → Import → «FBX Animation to Selected Armature» (`TOPBAR_MT_file_import` в `bl_ui/space_topbar.py`).
- Импорт штатным C++ `wm.fbx_import` во временные объекты; перенос — расчётом матриц по F-кривым
  (без `frame_set`/bake): мировая поза кости = исходной, кости по именам (точно, затем без namespace/регистра).
- Location: «All Bones» (по умолчанию) / «Root and Hips» (таз — по имени pelvis/hips, иначе первая
  ветвящаяся кость, или вручную). Scale — галочка, выкл. C++ импортёр коннектит Pelvis к Root
  (и в риге, и во временной арматуре): location connected-костей источника читается всё равно, а кости цели,
  которые анимация двигает, оператор делает disconnected — с предупреждением.
- Результат: Action на файл/take со слотом и Fake User, последний назначается. Временные данные удаляются.
- **Особенность C++ импортёра:** верхний узел-кость (`root` из UE) становится *объектом* арматуры, его анимация —
  на объекте; оператор сопоставляет объект с корневой костью цели по имени (без `.001`).
- Временные Actions переименовываются до создания результата, иначе имя получает `.001`.

## 3. FBX-экспорт под Maya
Правится штатный `scripts/addons_core/io_scene_fbx` (`export_scene.fbx`) — при обновлении от Blender возможны
конфликты в `__init__.py`, `export_fbx_bin.py`, `fbx_utils.py`.
- Зафиксированы и скрыты (`HIDDEN` + `SKIP_SAVE`): Forward -Z / Up Y, Apply Scalings = FBX Units Scale,
  Add Leaf Bones выкл, Key All Bones вкл, Force Start/End Keying вкл, Simplify 0, Armature FBXNode Type = `NONE`.
- `NONE` (новое): у арматуры нет своего узла, корневые кости на верхнем уровне. `ObjectWrapper.is_armature_without_node()`
  + проверки в модели, Null-атрибуте, связях, BindPose, анимации. Движение объекта арматуры запекается
  в корневые кости и в детей арматуры.
- Имя тейка в All Actions — чистое имя Action (было `Объект|Action`).
- **Action Per File** (`use_action_files`): каждый подходящий Action → `<имя Action>.fbx` в выбранной папке, имя файла
  игнорируется, `<>:"/\|?*` → `_` (тейк сохраняет оригинальное имя). `save_action_files()` + фильтр `bake_anim_only_action`.
- Проверено реимпортом: кости, скиннутый меш и дети арматуры совпадают с оригиналом.
