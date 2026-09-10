"""关闭路径异步化验收（plan 阶段 A / A3）：关窗不再冻结 GUI。

断言（对应计划 V1/V2）：
  §A 首次 close() 在 <200ms 内返回，且期间主窗口仍可见（未冻结、未立刻消失）；
  §B 关闭遮罩已创建且可见（用户看得见「正在保存并退出」）；
  §C 后台线程确实完成了收尾：session.shutdown / server.shutdown 各 1 次，
     且 clear_cache_on_exit 打开时 _clear_preview_cache_now 被调用 1 次；
  §D 硬超时兜底：后台卡死（session.shutdown 睡 60s）时 _force_close() 仍能真正关窗；
  §E 重复关闭幂等：已关窗后再 close() 不抛异常、不重复起后台线程。

无头运行：QT_QPA_PLATFORM=offscreen
用法：.venv/Scripts/python close_lag_test.py     （退出码 0=PASS / 1=FAIL / 2=SKIP）
"""
from __future__ import annotations

import os
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PySide6.QtWidgets import QApplication  # noqa: E402

from core.config import AppConfig  # noqa: E402
from test_support import Checker  # noqa: E402
from ui.main_window import MainWindow  # noqa: E402

FAST_LIMIT = 0.2          # 首次 close() 允许的最大阻塞（秒）
WORK_TIMEOUT = 5.0        # 轮询后台完成标志的上限（秒）


def _pump(app, seconds: float) -> None:
    """跑事件循环 seconds 秒（让 QTimer/遮罩更新有机会执行）。"""
    t0 = time.time()
    while time.time() - t0 < seconds:
        app.processEvents()
        time.sleep(0.02)


def _wait_hidden(app, w, timeout: float = 4.0) -> bool:
    """等窗口真正隐藏：后台收尾完成后由 GUI 线程 100ms 轮询触发 _force_close。

    _shutdown_done 置真与真正关窗之间隔着一次 QTimer 轮询（≤100ms），故
    不能刚看到 done 就断言「窗口已关闭」——高负载下会抖（实测回归整跑时抖过）。
    """
    t0 = time.time()
    while time.time() - t0 < timeout:
        app.processEvents()
        if not w.isVisible():
            return True
        time.sleep(0.02)
    return not w.isVisible()


def _wait_done(app, w, timeout: float = WORK_TIMEOUT) -> bool:
    """轮询 _shutdown_done（驱动事件循环），返回是否在超时内完成。"""
    t0 = time.time()
    while time.time() - t0 < timeout:
        app.processEvents()
        if getattr(w, "_shutdown_done", False):
            return True
        time.sleep(0.02)
    return False


def main() -> int:
    app = QApplication.instance() or QApplication(sys.argv)
    ck = Checker("关闭路径异步化与超时兜底（close_lag_test）")

    cfg = AppConfig()
    orig_clear = bool(cfg.get("clear_cache_on_exit"))

    try:
        # ------------------------------------------------------------ §A/§B
        # 真会话首次关闭：close() 必须立刻返回，窗口与遮罩都还在。
        ck.section("A/B 首次 close() 不阻塞，窗口可见 + 遮罩出现")
        cfg.set("clear_cache_on_exit", False)   # 不动用户真实缓存
        w1 = MainWindow()
        w1.show()
        _pump(app, 0.2)

        t0 = time.perf_counter()
        w1.close()
        dt = time.perf_counter() - t0
        print(f"  [info] 首次 close() 阻塞 {dt * 1000:.1f}ms")
        ck.check(dt < FAST_LIMIT,
                 f"首次 close() 快速返回（{dt * 1000:.1f}ms < {FAST_LIMIT * 1000:.0f}ms）")
        ck.check(w1.isVisible(), "遮罩期间主窗口仍可见（未冻结成白屏/未立即消失）")
        ov = getattr(w1, "_shutdown_overlay", None)
        ck.check(ov is not None and ov.isVisible(),
                 "关闭遮罩已创建且可见（_shutdown_overlay）")

        done1 = _wait_done(app, w1)
        ck.check(done1, f"后台收尾在 {WORK_TIMEOUT:.0f}s 内完成（_shutdown_done）")
        ck.check(_wait_hidden(app, w1), "收尾完成后窗口已真正关闭（轮询触发的 close）")
        if getattr(w1, "_shutdown_timer", None) is not None:
            w1._shutdown_timer.stop()

        # --------------------------------------------------------------- §C
        # 替身会话/流服务：断言收尾动作各发生一次，且清缓存按开关执行。
        ck.section("C 后台确实完成收尾（session/server 各 1 次 + 缓存按开关）")
        w2 = MainWindow()
        w2.show()
        _pump(app, 0.2)
        calls = {"session": 0, "server": 0, "clear": 0, "keep": None}

        def fake_session_shutdown():
            calls["session"] += 1

        def fake_server_shutdown():
            calls["server"] += 1

        def fake_clear(keep_dirs=(), log_key=""):
            calls["clear"] += 1
            calls["keep"] = set(keep_dirs)

        w2.session.shutdown = fake_session_shutdown
        w2.server.shutdown = fake_server_shutdown
        w2._clear_preview_cache_now = fake_clear
        cfg.set("clear_cache_on_exit", True)    # 开关打开 → 必须触发清理
        try:
            w2.close()
            done2 = _wait_done(app, w2)
            ck.check(done2, "替身场景后台收尾完成（_shutdown_done）")
            ck.check(calls["session"] == 1,
                     f"session.shutdown 调用 1 次（实际 {calls['session']}）")
            ck.check(calls["server"] == 1,
                     f"server.shutdown 调用 1 次（实际 {calls['server']}）")
            ck.check(calls["clear"] == 1,
                     f"clear_cache_on_exit=True 时清理缓存 1 次（实际 {calls['clear']}）")
            ck.check(isinstance(calls["keep"], set),
                     "清理时带上了保护名单快照 keep_dirs（C1 铁律：shutdown 前取）")
        finally:
            cfg.set("clear_cache_on_exit", orig_clear)
        if getattr(w2, "_shutdown_timer", None) is not None:
            w2._shutdown_timer.stop()

        # --------------------------------------------------------------- §D
        # 硬超时兜底：关停卡死（睡 60s）时也必须能真正关掉。
        ck.section("D 硬超时兜底（后台卡死也能关窗）")
        w3 = MainWindow()
        w3.show()
        _pump(app, 0.2)
        w3.session.shutdown = lambda: time.sleep(60)
        w3.server.shutdown = lambda: None
        cfg.set("clear_cache_on_exit", False)
        n_sleep = {"n": 0}

        def counting_sleep():
            n_sleep["n"] += 1
            time.sleep(60)

        w3.session.shutdown = counting_sleep
        w3.close()                       # 起遮罩 + daemon 线程（正在睡）
        _pump(app, 0.3)
        ck.check(w3._shutdown_done is False, "后台仍在运行（_shutdown_done 为假）")
        w3._force_close()                # 硬超时路径：不依赖后台完成
        ck.check(w3._shutting_down is True,
                 "_force_close() 后 _shutting_down 为真（硬超时兜底可达）")
        ck.check(_wait_hidden(app, w3), "硬超时兜底后窗口已真正关闭")

        # --------------------------------------------------------------- §E
        # 幂等：已关窗后再 close() 不抛异常、不重复起后台线程。
        ck.section("E 重复关闭幂等")
        err = None
        try:
            w3.close()
            w3.close()
        except Exception as e:            # noqa: BLE001
            err = e
        ck.check(err is None, f"重复 close() 不抛异常（{err!r}）")
        ck.check(w3._shutting_down is True, "重复 close() 后 _shutting_down 仍为真")
        ck.check(n_sleep["n"] == 1,
                 f"后台线程只起了一次（session.shutdown 计数 {n_sleep['n']}）")
    finally:
        cfg.set("clear_cache_on_exit", orig_clear)

    return ck.report()


if __name__ == "__main__":
    sys.exit(main())
