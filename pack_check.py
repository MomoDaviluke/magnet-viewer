"""打包产物自检：确认「该有的都在、该砍的确实砍了」，并报告体积构成。

存在的理由：spec 里的瘦身过滤是一串黑名单，改 Qt 版本或换 Qt 构建时可能
「砍过头」（缺 DLL）或「没砍动」（体积回升）——两者在源码测试里都看不出来，
只有打开 exe 才会发现（前者直接黑屏/开播失败）。本脚本把这两类回归钉在
打包之后、交付之前。

用法：
    python pack_check.py                     # 默认查 dist/MagnetViewer
    python pack_check.py dist/OtherName      # 指定产物目录

退出码沿用项目约定：
    0 = 通过（含体积告警）
    1 = 失败（缺必需组件）
    2 = SKIP（产物目录不存在——还没打包，不假装通过）
"""
from __future__ import annotations

import os
import sys

DEFAULT_DIST = os.path.join("dist", "MagnetViewer")
EXE_NAME = "MagnetViewer.exe"

# 必需：少一件 = 应用起不来或播不了（与 spec 里的 MUST_KEEP 对应）
REQUIRED = [
    "MagnetViewer.exe",
    "_internal/PySide6/Qt6Core.dll",
    "_internal/PySide6/Qt6Gui.dll",
    "_internal/PySide6/Qt6Widgets.dll",
    # 多媒体链（缺任何一件 → QMediaPlayer ResourceError 'Not available'）
    "_internal/PySide6/Qt6Multimedia.dll",
    "_internal/PySide6/Qt6MultimediaWidgets.dll",
    "_internal/PySide6/plugins/multimedia/ffmpegmediaplugin.dll",
    "_internal/PySide6/avcodec-61.dll",
    "_internal/PySide6/avformat-61.dll",
    "_internal/PySide6/avutil-59.dll",
]

# 已瘦身项：存在即说明过滤没生效（体积白给），只告警不算失败
SLIMMED = {
    "_internal/PySide6/opengl32sw.dll": "20MB 软件 OpenGL（Quick 专用）",
    "_internal/PySide6/Qt6Qml.dll": "QML 引擎（Widgets 应用不需要）",
    "_internal/PySide6/Qt6Quick.dll": "Quick 渲染栈（Widgets 应用不需要）",
    "_internal/PySide6/Qt6Pdf.dll": "PDF 支持（本应用不显示 PDF）",
}

WARN_SIZE_MB = 130      # 瘦身生效时约 103MB；失效会回到 147MB，这条就是哨兵


def dir_size_mb(path: str) -> float:
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            fp = os.path.join(root, f)
            try:
                total += os.path.getsize(fp)
            except OSError:
                pass
    return total / 1024 / 1024


def top_files(path: str, limit: int = 5):
    items = []
    for root, _dirs, files in os.walk(path):
        for f in files:
            fp = os.path.join(root, f)
            try:
                items.append((os.path.getsize(fp), os.path.relpath(fp, path)))
            except OSError:
                pass
    items.sort(reverse=True)
    return items[:limit]


def main() -> int:
    dist = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_DIST
    if not os.path.isdir(dist):
        print(f"[SKIP] 产物目录不存在：{dist}（还没打包，不算通过）")
        return 2

    missing = [r for r in REQUIRED
               if not os.path.isfile(os.path.join(dist, r.replace("/", os.sep)))]
    leftover = [(k, v) for k, v in SLIMMED.items()
                if os.path.isfile(os.path.join(dist, k.replace("/", os.sep)))]

    size = dir_size_mb(dist)
    print(f"[0] 产物：{os.path.abspath(dist)}  总大小 {size:.1f} MB")

    for k, v in leftover:
        print(f"[WARN] 瘦身项仍在：{k}（{v}）——过滤没生效，体积白给")

    if size > WARN_SIZE_MB:
        print(f"[WARN] 体积 {size:.1f}MB 超过 {WARN_SIZE_MB}MB 阈值，建议复查 "
              f"spec 的瘦身过滤是否仍生效")

    print("[1] 体积前几名：")
    for sz, rel in top_files(dist):
        print(f"    {sz / 1024 / 1024:6.1f} MB  {rel}")

    if missing:
        print(f"[FAIL] 缺必需组件 {len(missing)} 项：")
        for m in missing:
            print(f"    - {m}")
        return 1

    print(f"[PASS] 必需组件 {len(REQUIRED)} 项齐全"
          + (f"；瘦身项 {len(SLIMMED) - len(leftover)}/{len(SLIMMED)} 已剔除"
             if not leftover else "（有瘦身项残留，见上）"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
