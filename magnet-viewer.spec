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
    excludes=["tkinter", "pytest", "IPython"],
    noarchive=False,
)
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
