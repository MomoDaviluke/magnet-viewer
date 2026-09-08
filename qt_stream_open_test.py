"""Qt 客户端实测：QMediaPlayer（FFmpeg 后端）能否打开“moov 在尾部”的边下边播流。

与 moov_stream_test.py（ffprobe 探测）互补：本脚本使用应用同款播放器栈
（PySide6 + Qt Multimedia FFmpeg 后端，界面置为 offscreen 无头运行），
直接验证 QMediaPlayer 的开播行为：

- 仅头部 64KB（旧行为）             → 期望 errorOccurred（复现 moov atom not found）；
- 头部 + 尾部 4MB 索引窗口（修复后） → 期望成功进入 Loaded/Buffered/Buffering；
- 全部下载（基线）                  → 期望成功。

用法：QT_QPA_PLATFORM=offscreen python qt_stream_open_test.py
"""
import os
import shutil
import sys
import tempfile
import threading
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PySide6.QtCore import QEventLoop, QTimer, QUrl  # noqa: E402
from PySide6.QtMultimedia import QMediaPlayer  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from core.models import PieceMap  # noqa: E402
from core.stream_server import StreamServer  # noqa: E402
from moov_stream_test import (PL, TAIL_BYTES, HEAD_BYTES,  # noqa: E402
                              make_tail_moov_mp4)

OPEN_OK_STATUSES = (QMediaPlayer.LoadedMedia, QMediaPlayer.BufferedMedia,
                    QMediaPlayer.BufferingMedia, QMediaPlayer.EndOfMedia)
TIMEOUT_MS = 30_000


def try_open(url: str, expect_open: bool) -> dict:
    """用一个 QMediaPlayer 打开 url，等待成功/失败/超时。"""
    app = QApplication.instance() or QApplication(sys.argv)
    player = QMediaPlayer()
    result = {"opened": False, "error": None, "status_text": None}
    loop = QEventLoop()

    def on_status(status):
        if status in OPEN_OK_STATUSES:
            result["opened"] = True
            result["status_text"] = status.name
            loop.quit()
        elif result["status_text"] is None:
            result["status_text"] = status.name

    def on_error(error, message):
        result["error"] = f"{error.name}: {message}"
        loop.quit()

    player.mediaStatusChanged.connect(on_status)
    player.errorOccurred.connect(on_error)
    QTimer.singleShot(TIMEOUT_MS, loop.quit)
    # 只验证「能否打开媒体」（元数据/索引解析），不调用 play()，
    # 避免合成测试文件的零数据触发解码期误报
    player.setSource(QUrl(url))
    loop.exec()

    # 先断开再 stop()，避免 stop 触发的 NoMedia 覆盖已记录的状态
    try:
        player.mediaStatusChanged.disconnect(on_status)
        player.errorOccurred.disconnect(on_error)
    except RuntimeError:
        pass
    player.stop()
    player.setSource(QUrl())
    player.deleteLater()
    return result


def run_case(tmp: str, name: str, have: set[int], expect_open: bool,
             fill_on_demand: bool = False) -> bool:
    disk = os.path.join(tmp, "demo.mp4")
    size = os.path.getsize(disk)
    pm = PieceMap(PL, 0, 0, (size - 1) // PL, size, have.__contains__)

    def demand(path, start, end_excl):
        if not fill_on_demand:
            return
        first, last = start // PL, max(0, end_excl - 1) // PL

        def fill():
            time.sleep(0.15)          # 模拟调度器按需补拉
            have.update(range(first, last + 1))
        threading.Thread(target=fill, daemon=True).start()

    srv = StreamServer(tmp, pieces_cb=lambda p: pm if p == disk else None,
                       demand_cb=demand, wait_timeout=8.0)
    srv.start()
    try:
        r = try_open(srv.url_for("demo.mp4"), expect_open)
    finally:
        srv.shutdown()
    ok = (r["opened"] is True) == expect_open
    tag = "通过" if ok else "失败"
    detail = r["error"] or r["status_text"] or "timeout"
    print(f"[{name}] 期望{'开播' if expect_open else '打不开'} → "
          f"opened={r['opened']} status={r['status_text']} err={r['error']}  [{tag}]")
    return ok


def main():
    try:
        from PySide6.QtMultimedia import QMediaPlayer as _m
        _m  # noqa
    except Exception as e:
        print(f"[SKIP] PySide6 QtMultimedia 不可用（{e}），Qt 开播验证跳过")
        return 2    # 退出码 2 = 显式跳过（区别于「通过=0 / 失败=1」假绿）
    # 后端可用性探针：QMediaPlayer 能 import 不代表多媒体栈可用。
    # 无音频/显示会话的环境（无头 CI、服务器）实例化即 ResourceError
    # 'Not available'，此时整组用例必红却与代码无关 —— 显式 SKIP，
    # 与既有「不假绿也不假红」的约定保持一致。
    # 2026-09-08 补：另一种更常见的「不可用」是只装了 PySide6_Essentials——
    # 多媒体后端插件（plugins/multimedia/ffmpegmediaplugin.dll + avcodec/
    # avformat/avutil）只随 PySide6_Addons 分发。装上 Addons 后本组用例
    # 在本机首次真正执行（A/B/C 全绿），此前长期停在 SKIP 分支。
    probe = _m()
    if probe.error() != _m.NoError:
        print(f"[SKIP] Qt 多媒体后端不可用（{probe.errorString()}），Qt 开播验证跳过")
        return 2
    probe.deleteLater()
    tmp = tempfile.mkdtemp(prefix="mv_qtopen_")
    try:
        disk = os.path.join(tmp, "demo.mp4")
        if not make_tail_moov_mp4(disk):
            print("[SKIP] 无法用 ffmpeg 生成测试视频，Qt 开播验证跳过")
            return 2    # 退出码 2 = 显式跳过，避免「跳过=通过」假绿
        size = os.path.getsize(disk)
        print(f"[0] 真实尾部-moov MP4：{size / 1024 / 1024:.1f} MB")

        head_only = set(range(0, HEAD_BYTES // PL))
        tail_first = (max(0, (size - TAIL_BYTES)) + PL - 1) // PL
        head_and_tail = set(range(0, HEAD_BYTES // PL))
        head_and_tail |= set(range(tail_first, (size - 1) // PL + 1))
        all_pieces = set(range(0, (size - 1) // PL + 1))

        ok_a = run_case(tmp, "A 仅头部(旧)", head_only, expect_open=False)
        ok_b = run_case(tmp, "B 头+尾窗口+按需补拉", head_and_tail,
                        expect_open=True, fill_on_demand=True)
        ok_c = run_case(tmp, "C 全部下载", all_pieces, expect_open=True)
        ok = ok_a and ok_b and ok_c
        print("\n=== QMediaPlayer 开播验证" + ("全部通过" if ok else "未通过") + " ===")
        return 0 if ok else 1
    finally:
        time.sleep(0.2)
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())