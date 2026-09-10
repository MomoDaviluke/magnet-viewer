"""UI 截图工具（离屏，人工验收用）：双主题对比 + **真内容**。

用法：
    .venv/Scripts/python ui_shot.py .hermes/shots-light light
    .venv/Scripts/python ui_shot.py .hermes/shots-dark  dark
    （第三个参数省略 = light；默认输出目录 .hermes/shots/）

产物（窗口 1200x800，含真内容的五张 PNG）：
    01-empty     空态（未解析）
    02-files     文件列表：**真种子**（离线假种子解析结果，目录/大小/占比都是真的）
    03-preview   图片画廊：真缩略图 + 真大图（离线写出的示例图，产品代码真加载）
    04-downloads 下载页：任务列表 + 详情（演示数据，不连网、不起真实下载）
    05-settings  设置对话框：四个分组（界面/网络与代理/缓存与预览/下载）+ 中文按钮
                 （真构造 SettingsDialog，不 exec 模态；高度 = 内容自然高度）

为什么不是空壳截图：改造前版本只截了空窗口，看不出配色/控件/字号改动。本工具
用 ``test_support.build_payload`` 造离线载荷（1 大视频 + 2 图 + 1 文本），
libtorrent 生成 .torrent 后走**产品同款** ``core.parser.parse_torrent_file``
解析，结果喂给 ``w.tree.populate`` / ``w.preview.gallery.set_result``，并
``setTabEnabled(预览, True)``——完全离线（不解析磁力链、不加种子、不下载）。

注意：下载页的任务行与状态栏数字是**演示数据**（screenshot-only），不来自真实
会话；文件树与画廊内容来自真种子与真图片文件。
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import libtorrent as lt                                # noqa: E402
from PySide6.QtCore import QItemSelectionModel, QPointF, QRectF, Qt  # noqa: E402
from PySide6.QtGui import (QColor, QFont, QFontDatabase, QImage,  # noqa: E402
                           QLinearGradient, QPainter)
from PySide6.QtWidgets import QApplication             # noqa: E402

from core.config import AppConfig                      # noqa: E402
from core.models import disk_root, file_disk_path, human_size  # noqa: E402
from core.parser import parse_torrent_file             # noqa: E402
from test_support import build_payload, make_torrent   # noqa: E402
from ui.downloads_pane import COL_NAME                 # noqa: E402
from ui.main_window import MainWindow                  # noqa: E402
from ui.settings_dialog import SettingsDialog          # noqa: E402
from ui.style import install as install_chevron_style  # noqa: E402
from ui.theme import apply_theme                       # noqa: E402
import ui.theme as theme                               # noqa: E402

SIZE = (1200, 800)          # 与 MainWindow 默认尺寸一致（布局改动才看得出来）
PIECE = 16 * 1024
NAME = "DemoPayload"
# 离线假种子内容：1 大视频 + 2 图 + 1 文本
PAYLOAD = {
    "movie/big_demo.mp4": 3 * 1024 * 1024,
    "pics/cover.jpg": 180 * 1024,
    "pics/screenshot.png": 120 * 1024,
    "readme.txt": 2048,
}
SHOTS = ("01-empty", "02-files", "03-preview", "04-downloads", "05-settings")

# 离屏平台在本机**没有字体库**（QFontDatabase.families() == 0 → 截图里全是
# 豆腐块，看不到内容）。显式注册 Windows 常见字体：中文/数字才是真的可读，
# 截图才具备验收价值。字体文件缺失时静默跳过（不阻断截图）。
FONT_FILES = ("msyh.ttc", "msyhbd.ttc", "consola.ttf", "consolab.ttf",
              "simsun.ttc", "seguiemj.ttf", "seguisym.ttf")


def load_fonts() -> list[str]:
    """注册系统字体，返回成功加载的文件名（离屏截图的排版保真前置）。"""
    root = os.path.join(os.environ.get("WINDIR", "C:/Windows"), "Fonts")
    loaded: list[str] = []
    for name in FONT_FILES:
        path = os.path.join(root, name)
        if os.path.isfile(path) and QFontDatabase.addApplicationFont(path) >= 0:
            loaded.append(name)
    return loaded


# --------------------------------------------------------------------------
# 离线假种子（不连网、不加种子、不下载）
# --------------------------------------------------------------------------

def build_fake_torrent(tmp: str) -> tuple[str, str]:
    """造离线 .torrent：返回 (种子文件路径, 载荷目录)。"""
    payload = os.path.join(tmp, NAME)
    build_payload(payload, PAYLOAD)      # 共享基建：写随机字节载荷
    make_torrent(payload, PIECE)         # 共享基建：确认种子可构造（同参数）
    fs = lt.file_storage()
    lt.add_files(fs, payload)
    ct = lt.create_torrent(fs, PIECE)
    lt.set_piece_hashes(ct, os.path.dirname(payload))
    tpath = os.path.join(tmp, "demo.torrent")
    with open(tpath, "wb") as fh:
        fh.write(lt.bencode(ct.generate()))
    return tpath, payload


def _write_image(path: str, w: int, h: int, sky_top: str, sky_bottom: str,
                 ground: str) -> None:
    """写一张真实可解码的示例图（画廊会真的渲染它，而不是占位文字）。

    画成"风景照"观感（天空渐变 + 太阳 + 山脊 + 地面）：缩略图/大图都读得出
    内容，便于验收配色与控件改动；这是真 PNG/JPEG 字节，产品代码真解码。
    """
    img = QImage(w, h, QImage.Format_RGB32)
    p = QPainter(img)
    p.setRenderHint(QPainter.Antialiasing, True)
    grad = QLinearGradient(0, 0, 0, h * 0.72)
    grad.setColorAt(0.0, QColor(sky_top))
    grad.setColorAt(1.0, QColor(sky_bottom))
    p.fillRect(QRectF(0, 0, w, h), QColor(sky_bottom))
    p.fillRect(QRectF(0, 0, w, h * 0.72), grad)
    p.setPen(Qt.NoPen)
    p.setBrush(QColor("#ffd166"))
    p.drawEllipse(QPointF(w * 0.72, h * 0.24), h * 0.10, h * 0.10)
    p.setBrush(QColor("#2f6b4f"))
    p.drawPolygon([QPointF(0, h), QPointF(w * 0.26, h * 0.44), QPointF(w * 0.58, h)])
    p.setBrush(QColor("#1f4d38"))
    p.drawPolygon([QPointF(w * 0.36, h), QPointF(w * 0.64, h * 0.52),
                   QPointF(w, h)])
    p.fillRect(QRectF(0, h * 0.82, w, h * 0.18), QColor(ground))
    p.end()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    img.save(path)


def materialize(result, payload: str) -> int:
    """按产品同款磁盘布局摆放载荷文件（画廊/预览读的就是这些真文件）。"""
    root = disk_root(result.cache_dir, getattr(result, "save_subdir", ""))
    scenes = {                       # 文件名 -> (天顶色, 天际色, 地面色)
        "cover.jpg": ("#4a86e0", "#cfe4ff", "#27452f"),
        "screenshot.png": ("#6b4aa0", "#ffb177", "#2b2438"),
    }
    for f in result.files:
        dst = file_disk_path(root, f)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        if f.is_image:
            sky_top, sky_bottom, ground = scenes.get(
                f.name, ("#5b6472", "#c9cfd8", "#1f2328"))
            _write_image(dst, 640, 360, sky_top, sky_bottom, ground)
        else:
            src = os.path.join(payload, *f.path.split("/"))
            if os.path.isfile(src):
                shutil.copyfile(src, dst)     # 视频/文本：真实字节
    return len(result.files)


# --------------------------------------------------------------------------
# 演示数据（截图专用；不来自真实会话）
# --------------------------------------------------------------------------

def _demo_tasks(cache_dir: str, total: int) -> list[dict]:
    """下载页演示任务行（4 条，覆盖下载中/完成/暂停/失败四种状态色）。"""
    def _task(tag: str, name: str, size: int, state: str, pct: float,
              rate: int, eta, prio: int) -> dict:
        return {"info_hash": tag * 40, "name": name, "total_size": size,
                "state": state, "progress": pct, "down_rate": rate, "eta": eta,
                "priority": prio, "selected_files": [],
                "save_path": os.path.join(cache_dir, "downloads", tag * 40)}
    return [
        _task("a", f"{NAME}（本机演示）", total, "DOWNLOADING", 0.62,
              145_000, 9, 2),
        _task("b", "ArchLinux-2026.09-x86_64.iso", 1_280_000_000,
              "DOWNLOADING", 0.18, 820_000, 1280, 1),
        _task("c", "海洋-Ocean.1080p.BluRay.x264.mkv", 2_450_000_000,
              "COMPLETED", 1.0, 0, None, 1),
        _task("d", "ubuntu-26.04-desktop-amd64.iso", 5_600_000_000,
              "PAUSED", 0.41, 0, None, 0),
    ]


# --------------------------------------------------------------------------

def _shot(app, w, out: str, name: str) -> str:
    app.processEvents()
    path = os.path.join(out, f"{name}.png")
    w.grab().save(path)
    print("saved", path)
    return path


def main() -> int:
    out = sys.argv[1] if len(sys.argv) > 1 else os.path.join(".hermes", "shots")
    mode = sys.argv[2] if len(sys.argv) > 2 else "light"
    os.makedirs(out, exist_ok=True)
    tmp = tempfile.mkdtemp(prefix="mv_shot_")

    app = QApplication([])
    print("fonts loaded:", load_fonts() or "（无系统字体，文本可能不可读）")
    app.setFont(QFont("Microsoft YaHei UI", 9))   # 与 main.py 一致（排版保真）
    # 箭头绘制：必须在 apply_theme 之前装（与 main.py 同序）
    install_chevron_style(app)
    try:
        apply_theme(app, mode)
    except TypeError:
        # 兼容改造前的 theme.apply_theme(app)（T4「改造前」worktree 对照用）
        apply_theme(app)
    w = MainWindow()
    w.resize(*SIZE)
    w.show()
    app.processEvents()
    # 700ms 状态轮询会覆盖注入的演示任务/真状态 → 截图期间停掉
    for timer in (getattr(w, "_status_timer", None),
                  getattr(w, "_cache_timer", None)):
        if timer is not None:
            timer.stop()

    try:
        paths = [_shot(app, w, out, SHOTS[0])]       # 01 空态

        tpath, payload = build_fake_torrent(tmp)
        result = parse_torrent_file(tpath)           # 产品同款解析入口
        result.cache_dir = os.path.join(tmp, "cache")
        result.save_subdir = f".preview/{result.info_hash}"
        materialize(result, payload)
        w._on_metadata(result)                       # 树 + 画廊 + 预览页签（真内容）
        w.tree.select_file(result.videos[0])         # 选中主视频（真行）
        w.status_panel.set_state(
            f"解析完成：{result.name} · {len(result.view_files)} 个文件 · "
            f"共 {result.total_size / 1024 / 1024:.1f} MB")
        w.status_panel.update_status(
            {"num_seeds": 12, "num_peers": 46, "download_rate": 1_450_000,
             "buffer": 0.62, "preview_file": None})
        w.status_panel.set_cache_usage(
            f"缓存 {human_size(1_450_000_000)} / {human_size(8 * 1024 ** 3)}")
        w.tabs.setCurrentIndex(0)
        paths.append(_shot(app, w, out, SHOTS[1]))   # 02 文件列表（真种子）

        # 03 画廊：注入「图片已下载完成」状态 → 缩略图 + 大图都真渲染
        w.preview.show_gallery(result.images[0])
        progress = [0] * len(result.files)
        for f in result.files:
            progress[f.index] = f.size
        w.preview.gallery.update_status({"file_progress": progress})
        w.preview.gallery._poll_completed()          # 走产品同款缩略图载入路径
        w.preview.gallery._show_index(0)
        w.tabs.setCurrentIndex(1)
        paths.append(_shot(app, w, out, SHOTS[2]))   # 03 画廊（真图）

        # 04 下载页（演示数据）：任务列表 + 详情区
        w.downloads.set_tasks(_demo_tasks(result.cache_dir, result.total_size))
        model = w.downloads.tree.model()
        if model.rowCount() > 0:
            w.downloads.tree.selectionModel().select(
                model.index(0, COL_NAME),
                QItemSelectionModel.SelectionFlag.Select
                | QItemSelectionModel.SelectionFlag.Rows)
        w.status_panel.set_state(
            f"已提交下载任务：{NAME}（重复 info_hash 自动去重不重复下载）")
        w.tabs.setCurrentIndex(2)
        paths.append(_shot(app, w, out, SHOTS[3]))   # 04 下载页

        # 05 设置对话框：真构造（不 exec 模态、不联网），高度 = 内容自然高度
        # （尺寸靠窗；离屏屏幕较小时内容靠 QScrollArea 兜底）
        dlg = SettingsDialog(AppConfig(), os.path.join(tmp, "cache"),
                             on_clear_cache=None)
        # 截图连贯性：下拉初值读的是**已保存配置**，截图用的是命令行主题 →
        # 让下拉与当前渲染主题一致，避免"深色界面里显示浅色档"的误读
        _idx = dlg.theme.findData(mode if mode in ("light", "dark")
                                  else theme.active_mode())
        if _idx >= 0:
            dlg.theme.setCurrentIndex(_idx)
        dlg.show()
        app.processEvents()
        dlg.resize(max(660, dlg.sizeHint().width()),
                   min(dlg.full_height(), 1400))
        app.processEvents()
        paths.append(_shot(app, dlg, out, SHOTS[4]))  # 05 设置面板
        settings_h = dlg.height()
        dlg.close()
        dlg.deleteLater()

        print(f"theme={mode}  shots={len(paths)}  "
              f"settings_h={settings_h}  "
              f"files={len(result.view_files)}  "
              f"images={len(result.images)}  videos={len(result.videos)}")
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
