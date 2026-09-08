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


def _cleanup(player, *handlers):
    for slot in handlers:
        try:
            player.mediaStatusChanged.disconnect(slot)
            player.errorOccurred.disconnect(slot)
            player.positionChanged.disconnect(slot)
        except (RuntimeError, TypeError):
            pass
    player.stop()
    player.setSource(QUrl())
    player.deleteLater()


def try_seek(url: str, target_ms: int, seek_timeout_ms: int = 40_000) -> dict:
    """开播后「拖动进度条」到 target_ms，验证位置能否真正推进。

    定位说明：这是**正向验证**（渐进服务下拖动能成功），不是旧行为的回归
    判据 —— 实测关闭渐进服务后 QMediaPlayer 仍能推进到目标位置（它可能只
    发小范围 Range，或从 416 中恢复）。真实环境下旧实现的失败取决于下载
    速度与文件大小：本地模拟是「0.15s 补齐」的无限带宽，公网慢速源下
    8~20s 等待不足以让整个剩余区间就绪，差异才会显现。
    """
    app = QApplication.instance() or QApplication(sys.argv)
    player = QMediaPlayer()
    result = {"opened": False, "seeked": False, "pos": -1, "error": None}
    loops: list = []

    def quit_loops():
        for lo in list(loops):
            lo.quit()

    def on_status(status):
        if status in OPEN_OK_STATUSES:
            result["opened"] = True
            quit_loops()

    def on_error(error, message):
        result["error"] = f"{error.name}: {message}"
        quit_loops()

    def on_position(pos):
        result["pos"] = pos
        if abs(pos - target_ms) <= 3000:
            result["seeked"] = True
            quit_loops()

    player.mediaStatusChanged.connect(on_status)
    player.errorOccurred.connect(on_error)
    player.positionChanged.connect(on_position)

    loop = QEventLoop()
    loops.append(loop)
    QTimer.singleShot(TIMEOUT_MS, quit_loops)
    player.setSource(QUrl(url))
    loop.exec()                      # 等开播
    if not result["opened"]:
        _cleanup(player, on_status, on_error, on_position)
        return result
    player.setPosition(target_ms)    # 拖动到未下载位置
    loop2 = QEventLoop()
    loops.append(loop2)
    QTimer.singleShot(seek_timeout_ms, quit_loops)
    loop2.exec()                     # 等位置推进
    _cleanup(player, on_status, on_error, on_position)
    return result


def run_seek_case(tmp: str, name: str, have: set[int], target_ms: int,
                  expect_seek: bool = True, demand_max: int = 60) -> bool:
    """点播按真实调度器语义**有块数上限**（PreviewScheduler.request_range）。"""
    disk = os.path.join(tmp, "demo.mp4")
    size = os.path.getsize(disk)
    pm = PieceMap(PL, 0, 0, (size - 1) // PL, size, have.__contains__)

    def demand(path, start, end_excl):
        first = start // PL
        last = min(max(0, end_excl - 1) // PL, first + demand_max - 1)

        def fill():
            time.sleep(0.15)          # 模拟调度器按需补拉
            have.update(range(first, last + 1))
        threading.Thread(target=fill, daemon=True).start()

    srv = StreamServer(tmp, pieces_cb=lambda p: pm if p == disk else None,
                       demand_cb=demand, wait_timeout=8.0)
    srv.start()
    try:
        r = try_seek(srv.url_for("demo.mp4"), target_ms)
    finally:
        srv.shutdown()
    ok = (r["seeked"] is True) == expect_seek
    tag = "通过" if ok else "失败"
    print(f"[{name}] 期望{'可拖动' if expect_seek else '拖不动'} → "
          f"seeked={r['seeked']} pos={r['pos']} err={r['error']}  [{tag}]")
    return ok


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
        # D：拖动到未下载位置 → 位置必须真正推进（否则就是「进度条失灵」）。
        # 目标落在头部 64KB 与尾部 4MB 窗口之间的空洞里。
        hole_mid = (HEAD_BYTES + max(0, size - TAIL_BYTES)) // 2
        target_ms = int(hole_mid / size * 30.0 * 1000)
        have_d = set(range(0, HEAD_BYTES // PL))
        have_d |= set(range(tail_first, (size - 1) // PL + 1))
        print(f"[D] 拖动目标 {hole_mid} B（{target_ms} ms）落在空洞内")
        ok_d = run_seek_case(tmp, "D 拖动到未下载位置", have_d, target_ms,
                             expect_seek=True)
        ok = ok_a and ok_b and ok_c and ok_d
        print("\n=== QMediaPlayer 开播验证" + ("全部通过" if ok else "未通过") + " ===")
        return 0 if ok else 1
    finally:
        time.sleep(0.2)
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())