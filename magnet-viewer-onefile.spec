# -*- mode: python ; coding: utf-8 -*-
"""magnet-viewer PyInstaller 打包 spec（onefile + windowed，发布用单文件）。

用法：
    .venv\\Scripts\\python.exe -m PyInstaller --noconfirm magnet-viewer-onefile.spec
产物：
    dist/MagnetViewer.exe   （单文件，下载双击即用）

与 magnet-viewer.spec（onedir）的关系（2026-09-09 决策）：
- onedir 仍是**开发/自用首选**：启动不解包、杀软误报低、Qt 插件路径最稳。
- onefile 为 **GitHub Release 分发**而设：普通用户拿到一个 exe 下载即用，
  不存在"拷贝时漏掉 _internal 目录"的问题。代价（已知且接受）：
  ①每次启动往 %TEMP% 解包约 100MB，冷启动慢数秒；②未签名 exe 单文件
  更易触发杀软启发式，误报时可引导用户改下 portable zip（Release 同时
  提供两种产物）。
- 两个 spec 共享同一套 Analysis/瘦身过滤。改动瘦身规则时**两份同步改**，
  MUST_KEEP 误删防护在构建期兜底。
- **多媒体后端**：内嵌播放器依赖 plugins/multimedia/ffmpegmediaplugin.dll
  与 avcodec 等 FFmpeg DLL，只随 **PySide6_Addons** 分发。打包机必须先装
  Addons，否则产物能解析能下载、双击视频却开播必败（QMediaPlayer
  ResourceError 'Not available'）。
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

# ---------------- 瘦身过滤（与 magnet-viewer.spec 保持同步） ----------------
# 1) opengl32sw.dll（20MB）：Mesa 软件 OpenGL，只服务 Qt Quick 渲染后端。
# 2) Quick / Qml 全家（约 12.5MB）：Widgets 应用不加载 QML 引擎。
# 3) Qt6Pdf（4.5MB）：来自 imageformats 的 qpdf.dll，本应用不显示 PDF。
# 4) PySide6/translations（7.1MB）：界面文案硬编码中文。
# 5) 冷门图像格式插件：qicns/qtga/qwbmp/qwebp。
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
    """binaries/datas 的目标名可能带子目录，判定一律按文件名。"""
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

# onefile：binaries/datas 全部并入 EXE，无 COLLECT 段。
# UPX 依旧不压（杀软误报头号诱因）；windowed 无黑窗。
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="MagnetViewer",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,        # windowed
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
