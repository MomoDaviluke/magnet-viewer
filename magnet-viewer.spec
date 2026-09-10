# -*- mode: python ; coding: utf-8 -*-
"""magnet-viewer PyInstaller 打包 spec（onedir + windowed）。

用法：
    .venv\\Scripts\\pyinstaller --noconfirm --clean magnet-viewer.spec
产物：
    dist/MagnetViewer/MagnetViewer.exe   （连目录整个拷走即可运行）

选型（2026-09-07 决策，理由记录在案）：
- **onedir 而非 one-file**：①启动不往 %TEMP% 解包（BT 工具落临时目录
  极易触发杀软启发式，且解包慢）；②Qt 插件 DLL 目录结构保持原样，
  multimedia/ffmpegmediaplugin 的查找路径最可靠。分发=整个目录 zip。
- **windowed（--noconsole）**：GUI 应用；无控制台黑窗。
- PySide6 由官方 hook 自动收集插件与 DLL；此处仅显式补两处实测易漏：
  QtMultimedia/QtMultimediaWidgets 模块与 libtorrent 的 C 扩展。
- **多媒体后端（2026-09-08 补，关键前提）**：内嵌播放器要的
  `plugins/multimedia/ffmpegmediaplugin.dll` 与 avcodec/avformat/avutil/
  swresample/swscale 只随 **PySide6_Addons** 分发——PySide6_Essentials 的
  plugins 下根本没有 multimedia 目录。打包机必须先装 Addons，否则产物能
  解析能下载、双击视频却开播必败（QMediaPlayer ResourceError
  'Not available'）。装上后产物 128MB → 147MB（+19MB 即 FFmpeg 后端），
  目标机无需额外安装 ffmpeg 或播放器。
"""
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

hiddenimports = [
    "libtorrent",
    "PySide6.QtCore",
    "PySide6.QtGui",
    "PySide6.QtWidgets",
    "PySide6.QtMultimedia",
    "PySide6.QtMultimediaWidgets",
] + collect_submodules("ui") + collect_submodules("core")

# 多媒体后端插件（QMediaPlayer/FFmpeg 后端）——edge 用例的命脉；
# hook 通常已带，双保险显式收集（数据文件形式，含 plugins 目录）。
datas = collect_data_files("PySide6", includes=[
    "plugins/multimedia/*",
    "plugins/imageformats/*",
])

# UI 箭头资源（ui/assets/*.svg）：下拉/微调右侧「按钮」区的箭头由 QSS
# `image: url(<绝对路径>)` 读取（ui.theme.asset_path），**不打包就画不出箭头**
# （asset_path 会写 WARNING 日志，界面静默缺图）。目标路径必须与源码布局一致
# ——bundle 内 `ui/assets/<name>`，asset_path 按 `sys._MEIPASS/ui/assets` 找。
# 两份 spec（onedir + onefile）必须同步；缺文件在构建期直接失败。
import os as _os

UI_ASSETS = ("chevron-down-light.svg", "chevron-up-light.svg",
             "chevron-down-dark.svg", "chevron-up-dark.svg")
for _name in UI_ASSETS:
    _src = _os.path.join("ui", "assets", _name)
    if not _os.path.isfile(_src):
        raise SystemExit(f"[spec] 缺少 UI 箭头资源：{_src}")
    datas.append((_src, _os.path.join("ui", "assets")))

a = Analysis(
    ["main.py"],
    pathex=["."],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "pytest", "IPython",
              # Quick/Qml/Pdf 全家：Widgets 应用不加载 QML 引擎，也不显示 PDF
              "PySide6.QtQml", "PySide6.QtQuick", "PySide6.QtQuickWidgets",
              "PySide6.QtQuickControls2", "PySide6.QtPdf",
              "PySide6.QtPdfWidgets", "PySide6.Qt3DCore", "PySide6.Qt3DRender",
              "PySide6.QtCharts", "PySide6.QtDataVisualization",
              "PySide6.QtBluetooth", "PySide6.QtNfc", "PySide6.QtPositioning",
              "PySide6.QtSensors", "PySide6.QtSerialPort", "PySide6.QtTest",
              "PySide6.QtDesigner", "PySide6.QtHelp", "PySide6.QtWebEngineCore",
              "PySide6.QtWebEngineWidgets", "PySide6.QtWebSockets",
              "PySide6.QtSql", "PySide6.QtNetworkAuth", "PySide6.QtRemoteObjects",
              ],
    noarchive=False,
)

# ---------------- 瘦身过滤（2026-09-08） ----------------
# 原则：只砍「本应用确定用不到」的大件，每条写清依据；砍完必须重跑
#       `pack_check.py` + 三层实测 + 开播探针（build.bat 已串好）。
#
# 1) opengl32sw.dll（20MB）：Mesa 软件 OpenGL，只服务 Qt Quick 的渲染后端。
#    本应用是纯 QtWidgets，视频画面走 Qt Multimedia 自己的渲染路径，不碰它。
#    删错的表现是窗口黑屏 / 视频不出画，三层实测与抓帧探针会立刻暴露。
# 2) Quick / Qml 全家（约 12.5MB）：被 Qt6Multimedia 与 virtualkeyboard
#    插件间连带拖进来的，Widgets 应用不会加载 QML 引擎。
# 3) Qt6Pdf（4.5MB）：来自 imageformats 的 qpdf.dll，本应用不显示 PDF。
# 4) PySide6/translations（7.1MB）：Qt 自带界面译文，本项目文案硬编码中文。
# 5) 冷门图像格式插件：qicns/qtga/qwbmp/qwebp（图标与画廊用不到）。
DROP_BIN_EXACT = {"opengl32sw.dll"}
DROP_BIN_PREFIX = ("Qt6Qml", "Qt6Quick", "QtQml", "QtQuick",
                   "Qt6Pdf", "QtPdf")
DROP_DATA_PREFIX = ("PySide6/translations/", "PySide6/qml/")
DROP_DATA_NAME = {"qpdf.dll", "qicns.dll", "qtga.dll", "qwbmp.dll",
                  "qwebp.dll"}


def _norm(name: str) -> str:
    """TOC 里的目标路径在 Windows 上可能是反斜杠，统一成正斜杠再判定。"""
    return name.replace("\\", "/")


def _base(name: str) -> str:
    """binaries/datas 的目标名可能带子目录（如 PySide6/Qt6Core.dll），
    判定一律按文件名，免得换 Qt 版本改了布局就悄悄失效。"""
    return _norm(name).rsplit("/", 1)[-1]


a.binaries = [
    b for b in a.binaries
    if _base(b[0]) not in DROP_BIN_EXACT
    and not _base(b[0]).startswith(DROP_BIN_PREFIX)
]
a.datas = [
    d for d in a.datas
    if not _norm(d[0]).startswith(DROP_DATA_PREFIX)
    and _base(d[0]) not in DROP_DATA_NAME
]

# 误删防护：少任一件都说明过滤过火——构建期直接失败，别等运行时才黑屏。
MUST_KEEP = {
    "Qt6Core.dll", "Qt6Gui.dll", "Qt6Widgets.dll",
    "Qt6Multimedia.dll", "Qt6MultimediaWidgets.dll",
    "avcodec-61.dll", "avformat-61.dll", "avutil-59.dll",
}
missing = MUST_KEEP - {_base(b[0]) for b in a.binaries}
if missing:
    raise SystemExit(f"[spec] 瘦身过滤误删必需组件：{sorted(missing)}")
# ---------------- 瘦身过滤结束 ----------------

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="MagnetViewer",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,            # 不压：UPX 壳是杀软误报头号诱因，且体积收益有限
    console=False,        # windowed
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="MagnetViewer",
)
