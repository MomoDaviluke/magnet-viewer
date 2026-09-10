"""关闭路径异步化验收（plan 阶段 A / A3 + 阶段 A 审查整改）：关窗不再冻结 GUI。

断言（对应计划 V1/V2 + 审查 REQUEST_CHANGES 的 I1-I4 / Minor 5-10）：

  §A 首次 close() 在 <200ms 内返回，且期间主窗口仍可见（未冻结、未立刻消失）；
  §B 关闭遮罩已创建且可见（用户看得见「正在保存并退出」）；
  §C 后台线程确实完成了收尾：session.shutdown / server.shutdown 各 1 次，
     且 clear_cache_on_exit 打开时 _clear_preview_cache_now 被调用 1 次。
     **C1 真判别（I3）**：关窗**之前**登记活任务目录、替身 shutdown 复刻
     「清空注册表」→ 断言 keep 快照**含**该目录（事后取必空）；
  §D 硬超时兜底：后台卡死（session.shutdown 睡 60s）时 _force_close() 仍能真正关窗；
  §E 重复关闭幂等：已关窗后再 close() 不抛异常、不重复起后台线程；
  §F（I1）硬超时**自动触发**：SHUTDOWN_HARD_TIMEOUT_MS=300（实例覆盖类属性）后
     只驱动事件循环即可关窗——不手工调 _force_close()；
  §G（I2）停机窗口守卫：窗口期内全部入口/桥回调早退（不起新工作、不弹模态、
     不改页签/状态栏/历史），另加**源码级清点**（守卫集合与「不加」集合）；
  §H（Minor 6）closeEvent 逆常回退：异步化组件抛异常时窗口仍必须能关掉；
  §I（Minor 7）窗口期内重复关闭：不重复起后台线程、不提前关窗；
  §J（Minor 9 / I4）遮罩随 resize 跟动 + 遮罩真的画出来了（像素级绘制断言）。

无头运行：QT_QPA_PLATFORM=offscreen
用法：.venv/Scripts/python close_lag_test.py     （退出码 0=PASS / 1=FAIL / 2=SKIP）
"""
from __future__ import annotations

import ast
import inspect
import os
import shutil
import sys
import tempfile
import textwrap
import threading
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PySide6.QtCore import QMimeData, QPointF, Qt  # noqa: E402
from PySide6.QtGui import QDropEvent  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from core.config import AppConfig  # noqa: E402
from core.models import ParseResult, TorrentFile  # noqa: E402
from core.registry import TaskRecord  # noqa: E402
from test_support import Checker  # noqa: E402
import ui.main_window as mq  # noqa: E402
from ui.main_window import MainWindow  # noqa: E402
from ui.theme import BG, QSS  # noqa: E402

FAST_LIMIT = 0.2          # 首次 close() 允许的最大阻塞（秒）
WORK_TIMEOUT = 5.0        # 轮询后台完成标志的上限（秒）

# ---------------------------------------------------------------- I2 清点表
# 「加守卫」集合：能触发**新工作**（会话/下载/播放/清理）或**模态弹窗**
# 的用户入口槽与会话桥回调。它们的首条语句必须是
# ``if self._shutdown_started: return``（下面 §G 用 AST 逐条验证）。
GUARDED = {
    "_on_metadata",          # 桥回调：populate/切页签/写状态
    "_on_error",             # 桥回调：**模态 QMessageBox**
    "_resolve",              # 解析入口（写历史 + session.resolve）
    "_resolve_input",        # 输入框回车 / 「解析」按钮
    "_pick_torrent",         # 「打开种子文件…」（**模态 QFileDialog**）
    "dropEvent",             # 拖放落点入口
    "_add_download_flow",    # 「添加下载」（**模态 QInputDialog**）
    "_confirm_add_task",     # 添加下载确认（**模态 AddDownloadDialog** + add_task）
    "_preview_to_download",  # 预览页「转为下载」
    "_open_settings",        # 「设置」（**模态 SettingsDialog**）
    "_open_preview",         # 文件树双击预览（start_preview/set_stream）
    "_on_gallery_file",      # 画廊切图（start_preview + 配额清理）
    "_on_stream_failed",     # 播放失败重试上游（排 singleShot）
    "_retry_stream",         # singleShot 自排重试链
    "_task_pause",           # 下载页任务操作
    "_task_resume",
    "_task_remove",          # **模态 QMessageBox** + remove_task
    "_task_priority",
    "_task_open_preview",    # 下载页「打开预览」（focus_task + 切页签）
    "_on_seek",              # 播放器跳转 → scheduler 新工作
    "_on_scrub_preview",     # 拖动预取 → scheduler 新工作
    "_clear_cache_now",      # 设置对话框「立即清理」注入的回调
}
# 「不加守卫」集合（已逐条确认对已关窗口无副作用；§G 同样验证它们**没有**
# 被守卫——防止将来顺手加守反而误伤）：
NOT_GUARDED = {
    "_refresh_status",       # 700ms 定时器槽：closeEvent 首步即停定时器；
                             # 无弹窗、无用户可见动作、自带 try/except
    "_stop_preview",         # 停机语义（幂等、不启新工作），且被清理/收尾路径复用
    "_task_open_dir",        # 仅 QDesktopServices 打开资源管理器：无模态、无新下载
    "_enforce_cache_quota",  # 内部方法，入口（_open_preview/_on_gallery_file）已守
    "_clear_preview_cache_now",  # 内部共用件：**收尾线程**会调它，加守会误伤清理
    "resizeEvent",           # 几何事件：只同步遮罩几何
    "closeEvent",            # 关闭入口本身
}


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


class _Gate:
    """后台收尾闸门：让收尾线程卡在 wait() 上（复刻 3.8s 停机窗口）。"""

    def __init__(self, hold: float = 30.0):
        self.event = threading.Event()
        self.entered = threading.Event()
        self._hold = hold

    def block(self, *_a, **_k):
        self.entered.set()
        self.event.wait(self._hold)

    def release(self) -> None:
        self.event.set()


def _enter_window(app, w, gate, timeout: float = 3.0) -> bool:
    """把窗口推进「停机窗口」：close() 后收尾线程卡在闸门上、窗口尚未关闭。"""
    w.session.shutdown = gate.block       # 收尾线程卡住（不完成）
    w.server.shutdown = lambda: None
    w.close()
    t0 = time.time()
    while time.time() - t0 < timeout:
        app.processEvents()
        if gate.entered.is_set():
            return True
        time.sleep(0.02)
    return gate.entered.is_set()


def _rgb(img, x: int, y: int):
    c = img.pixelColor(x, y)
    return (c.red(), c.green(), c.blue())


def _bg_rgb():
    return tuple(int(BG[i:i + 2], 16) for i in (1, 3, 5))


def _first_guard_state(name: str) -> str:
    """返回 MainWindow.<name> 方法体的形态（AST，跳过 docstring）。

    "guard" = 首条语句恰为 ``if self._shutdown_started: return``；
    "absent" = 本类未定义该方法（如从 QWidget 继承的 C++ 方法）；
    其余返回 "other:<语句类型>"。
    """
    fn = getattr(MainWindow, name)
    if not hasattr(fn, "__code__"):        # C++ 侧方法（未在 ui/main_window.py 覆写）
        return "absent"
    src = textwrap.dedent(inspect.getsource(fn))
    fn = ast.parse(src).body[0]
    stmts = list(fn.body)
    if (stmts and isinstance(stmts[0], ast.Expr)
            and isinstance(stmts[0].value, ast.Constant)
            and isinstance(stmts[0].value.value, str)):
        stmts = stmts[1:]                      # 去 docstring
    if not stmts:
        return "empty"
    first = stmts[0]
    ok = (isinstance(first, ast.If)
          and isinstance(first.test, ast.Attribute)
          and first.test.attr == "_shutdown_started"
          and len(first.body) == 1 and isinstance(first.body[0], ast.Return)
          and not first.orelse)
    return "guard" if ok else f"other:{type(first).__name__}"


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
        ck.section("C 后台确实完成收尾（session/server 各 1 次 + 缓存按开关 + C1 真判别）")
        w2 = MainWindow()
        w2.show()
        _pump(app, 0.2)
        calls = {"session": 0, "server": 0, "clear": 0, "keep": None}

        # ---- I3：C1 断言换真判别 --------------------------------------
        # 原断言 isinstance(calls["keep"], set) 恒真（替身只可能回 set）——
        # 零判别力，删。改为：关窗**之前**往注册表登记一个活任务落盘目录，
        # 替身 session.shutdown 复刻真实 shutdown 的末段效应（清空注册表，
        # 见 core/session.SessionCore.shutdown → clear_runtime_state_locked），
        # 于是「shutdown 之后才取名单」必得空集：只有取自 shutdown **之前**
        # 的快照才含该目录。
        live_ih = "cd" * 20
        live_root = tempfile.mkdtemp(prefix="mv_c1_")
        live_dir = os.path.join(live_root, ".preview", live_ih)
        os.makedirs(os.path.join(live_dir, "data"), exist_ok=True)
        with open(os.path.join(live_dir, "data", "chunk.bin"), "wb") as fh:
            fh.write(b"c1")
        live_nc = os.path.normcase(live_dir)

        def fake_session_shutdown():
            calls["session"] += 1
            with w2.session._registry.lock:
                w2.session._registry.torrents.clear()   # 复刻：shutdown 末段清注册表

        def fake_server_shutdown():
            calls["server"] += 1

        def fake_clear(keep_dirs=(), log_key=""):
            calls["clear"] += 1
            calls["keep"] = {os.path.normcase(p) for p in keep_dirs}

        with w2.session._registry.lock:
            w2.session._registry.torrents[live_ih] = TaskRecord(save_path=live_dir)
        ck.check(live_nc in {os.path.normcase(p)
                             for p in w2.session.protected_dirs()},
                 "测试装置：关窗**之前** protected_dirs() 含活任务目录")

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
            ck.check(calls["keep"] is not None
                     and live_nc in calls["keep"],
                     f"清理拿到的 keep 快照**含**活任务目录（{live_ih[:8]}…）"
                     f"——证明快照取自 shutdown **之前**（实得 {calls['keep']}）")
            ck.check(live_nc not in {os.path.normcase(p)
                                     for p in w2._live_cache_dirs()},
                     "shutdown 之后 protected_dirs() 已空：**事后**取名单必空"
                     "（本断言非恒真，与上一断言互为对照）")
        finally:
            cfg.set("clear_cache_on_exit", orig_clear)
            with w2.session._registry.lock:
                w2.session._registry.torrents.pop(live_ih, None)
            shutil.rmtree(live_root, ignore_errors=True)
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

        # --------------------------------------------------------------- §F
        # I1：硬超时**自动触发**（10s 常量被实例属性压到 300ms），全程只驱动
        # 事件循环——不手工调 _force_close()，从而真正覆盖 singleShot 那条线。
        ck.section("F I1 硬超时自动触发（定时器真触发，无需手工兜底）")
        w4 = MainWindow()
        w4.show()
        _pump(app, 0.2)
        cfg.set("clear_cache_on_exit", False)
        gate4 = _Gate()
        w4.session.shutdown = gate4.block          # 后台卡死（永不完成）
        w4.server.shutdown = lambda: None
        w4.SHUTDOWN_HARD_TIMEOUT_MS = 300          # 实例属性覆盖类属性（10s → 300ms）
        t0 = time.perf_counter()
        w4.close()
        dt_close = time.perf_counter() - t0
        ck.check(dt_close < FAST_LIMIT,
                 f"紧超时下 close() 仍立即返回（{dt_close * 1000:.1f}ms）")
        ck.check(w4._shutdown_done is False,
                 "后台仍卡死（_shutdown_done 为假 → 排除「收尾完成」路径）")
        t0 = time.perf_counter()
        while time.perf_counter() - t0 < 2.0 and not w4._shutting_down:
            app.processEvents()                    # 只驱动事件循环
            time.sleep(0.02)
        dt_hard = time.perf_counter() - t0
        ck.check(0.2 <= dt_hard < 2.0,
                 f"硬超时在 300ms 量级触发（{dt_hard * 1000:.0f}ms ∈ [200, 2000)）")
        ck.check(w4._shutting_down is True,
                 "硬超时定时器**自动**置 _shutting_down"
                 "（未手工调 _force_close）")
        ck.check(w4.isHidden(), "硬超时后窗口自动隐藏（isHidden）")
        ck.check(w4._shutdown_done is False,
                 "关窗时后台仍未完成（确是硬超时触发，而非收尾完成）")
        gate4.release()
        if getattr(w4, "_shutdown_timer", None) is not None:
            w4._shutdown_timer.stop()

        # ---------------------------------------------- §G/§J/§I 共用一个窗口
        # 一个处于「停机窗口」的窗口（收尾线程卡在闸门上、尚未关窗），依次跑：
        #   I4 遮罩绘制（像素级）→ I2 守卫清点 → Minor 9 遮罩跟动 →
        #   Minor 7 重复关闭幂等。
        ck.section("G/J/I4 停机窗口：遮罩绘制（像素）· 入口守卫 · 跟动 · 重复关闭")
        app.setStyleSheet(QSS)      # 遮罩背景来自主题 QSS：本进程默认未挂主题
        w5 = MainWindow()
        w5.show()
        _pump(app, 0.3)
        w5.SHUTDOWN_HARD_TIMEOUT_MS = 30_000       # 窗口期内不自动关窗
        cw = w5.centralWidget().geometry()
        sample_pts = [(cw.center().x(), cw.center().y()), (10, 10)]
        base_pts = [_rgb(w5.grab().toImage(), x, y) for x, y in sample_pts]
        gate5 = _Gate()
        ck.check(_enter_window(app, w5, gate5),
                 "窗口已进入停机窗口（收尾线程卡住、窗口尚未关闭）")

        # ---- I4：遮罩真的画出来了 -------------------------------------
        ov5 = w5._shutdown_overlay
        ck.check(ov5 is not None and ov5.isVisible(), "遮罩存在且可见")
        ck.check(bool(ov5.testAttribute(Qt.WA_StyledBackground)),
                 "遮罩已置 WA_StyledBackground（I4：不依赖 QSS polish 的隐式置位）")
        ck.check(ov5.geometry() == w5.centralWidget().geometry(),
                 f"遮罩几何 = centralWidget 几何（{ov5.geometry()}）")
        ov_pts = [_rgb(w5.grab().toImage(), x, y) for x, y in sample_pts]
        for i, (b, o) in enumerate(zip(base_pts, ov_pts)):
            print(f"  [info] 采样点{i}{sample_pts[i]}：被遮内容 RGB{b} → 遮罩层 RGB{o}")
        ck.check(all(b != o for b, o in zip(base_pts, ov_pts)),
                 "遮罩像素 ≠ 被遮内容原像素（QSS 背景真的绘制了）")
        ck.check(any(b != _bg_rgb() for b in base_pts),
                 "至少一个采样点基线 ≠ 主题底色（两种判据可区分，非同一断言）")
        ck.check(all(o != _bg_rgb() for o in ov_pts),
                 f"遮罩像素 ≠ 主题底色 {_bg_rgb()}（不是「没画出来→露出窗口底色」）")

        # ---- Minor 9：遮罩随 resize 跟动 ------------------------------
        old_ov_geo = ov5.geometry()
        w5.resize(1200, 820)
        _pump(app, 0.25)
        ck.check(w5.centralWidget().geometry() != old_ov_geo,
                 "前置条件：resize 真的改变了 centralWidget 几何")
        ck.check(ov5.geometry() == w5.centralWidget().geometry(),
                 f"遮罩几何跟随 centralWidget（{ov5.geometry()} == "
                 f"{w5.centralWidget().geometry()}）")

        # ---- I2：窗口期内全部入口/桥回调早退 --------------------------
        calls2: dict = {}

        def spy(name, ret=None):
            def inner(*_a, **_k):
                calls2[name] = calls2.get(name, 0) + 1
                return ret
            return inner

        class _FakeQMessageBox:
            AcceptRole, DestructiveRole, RejectRole = 0, 1, 2

            def __init__(self, *_a, **_k):
                calls2["QMessageBox"] = calls2.get("QMessageBox", 0) + 1

            def setWindowTitle(self, *_a):
                pass

            def setText(self, *_a):
                pass

            def addButton(self, *_a, **_k):
                return object()

            def exec(self):
                return 0

            def clickedButton(self):
                return None

            @staticmethod
            def warning(*_a, **_k):
                calls2["QMessageBox.warning"] = calls2.get("QMessageBox.warning", 0) + 1

            @staticmethod
            def information(*_a, **_k):
                calls2["QMessageBox.information"] = (
                    calls2.get("QMessageBox.information", 0) + 1)

        class _FakeDialog:
            class DialogCode:                 # noqa: N801
                Accepted = 1

            def __init__(self, *_a, **_k):
                calls2["dialog"] = calls2.get("dialog", 0) + 1

            def exec(self):
                return 0

            def save_path(self):
                return ""

            def priority(self):
                return 1

            def seed_after_complete(self):
                return False

        class _FakeFileDlg:
            @staticmethod
            def getOpenFileName(*_a, **_k):
                calls2["QFileDialog"] = calls2.get("QFileDialog", 0) + 1
                return "", ""

        class _FakeInputDlg:
            @staticmethod
            def getText(*_a, **_k):
                calls2["QInputDialog"] = calls2.get("QInputDialog", 0) + 1
                return "", False

        class _FakeTimer:
            @staticmethod
            def singleShot(*_a, **_k):
                calls2["singleShot"] = calls2.get("singleShot", 0) + 1

        # 上游替身：会话/流服务/预览/状态栏/历史——任何一次调用都是「窗口期内
        # 起了新工作或做了用户可见动作」的铁证。
        targets = []
        for obj, name, ret in (
                (mq, "QMessageBox", None), (mq, "QFileDialog", None),
                (mq, "QInputDialog", None), (mq, "SettingsDialog", None),
                (mq, "AddDownloadDialog", None), (mq, "QTimer", None),
                (w5.session, "resolve", None), (w5.session, "stop_preview", None),
                (w5.session, "start_preview", None), (w5.session, "add_task", None),
                (w5.session, "pause_task", None), (w5.session, "resume_task", None),
                (w5.session, "remove_task", None), (w5.session, "focus_task", None),
                (w5.session, "task_result", None), (w5.session, "set_priority", None),
                (w5.session.scheduler, "request_range", None),
                (w5.session.scheduler, "seek_to_byte", None),
                (w5, "_clear_preview_cache_now", None),
                (w5.preview, "reset", None), (w5.preview.video, "set_stream", None),
                (w5.cfg, "push_recent", [])):
            targets.append((obj, name, getattr(obj, name)))
        fake_of = {"QMessageBox": _FakeQMessageBox, "QFileDialog": _FakeFileDlg,
                   "QInputDialog": _FakeInputDlg, "SettingsDialog": _FakeDialog,
                   "AddDownloadDialog": _FakeDialog, "QTimer": _FakeTimer}
        try:
            for obj, name, _orig in targets:
                if name in fake_of:
                    v = fake_of[name]
                elif name == "push_recent":
                    v = spy("cfg.push_recent", [])
                else:
                    v = spy(name)
                setattr(obj, name, v)
            fake_file = TorrentFile(1, "root/big.mkv", 1000, 0, 0, 0)
            w5.result = ParseResult(info_hash="ab" * 20, name="root",
                                    total_size=1000, piece_size=1024,
                                    num_pieces=1, files=[fake_file],
                                    source="magnet")
            w5._last_source = "magnet:?xt=urn:btih:" + "a" * 40
            w5.input.setText("magnet:?xt=urn:btih:" + "b" * 40)
            tab_before = w5.tabs.currentIndex()
            state_before = w5.status_panel.state.text()
            md = QMimeData()
            md.setText("magnet:?xt=urn:btih:" + "f" * 40)
            drop_ev = QDropEvent(QPointF(5, 5), Qt.CopyAction | Qt.MoveAction,
                                 md, Qt.LeftButton, Qt.NoModifier)
            task = {"info_hash": "e" * 40, "name": "task-X", "priority": 1}
            for fn in (lambda: w5._on_error("模拟解析失败"),
                       lambda: w5._retry_stream(),
                       lambda: w5._on_stream_failed(),
                       lambda: w5._resolve("magnet:?xt=urn:btih:" + "c" * 40),
                       lambda: w5._resolve_input(),
                       lambda: w5.dropEvent(drop_ev),
                       lambda: w5._pick_torrent(),
                       lambda: w5._confirm_add_task(
                           "magnet:?xt=urn:btih:" + "d" * 40),
                       lambda: w5._preview_to_download(),
                       lambda: w5._open_settings(),
                       lambda: w5._open_preview(fake_file),
                       lambda: w5._on_gallery_file(fake_file),
                       lambda: w5._task_pause(task),
                       lambda: w5._task_resume(task),
                       lambda: w5._task_priority(task, +1),
                       lambda: w5._task_remove(task),
                       lambda: w5._task_open_preview(task),
                       lambda: w5._on_seek(0),
                       lambda: w5._on_scrub_preview(0),
                       lambda: w5._clear_cache_now()):
                fn()
            w5.input.setText("")        # 非磁力内容 → 走 QInputDialog 分支
            w5._add_download_flow()
            w5.input.setText("magnet:?xt=urn:btih:" + "b" * 40)
        finally:
            for obj, name, orig in targets:
                setattr(obj, name, orig)
        ck.check(not calls2,
                 f"窗口期内 22 个入口/桥回调全部早退（副作用计数 {calls2 or '{}'}）")
        ck.check(w5.tabs.currentIndex() == tab_before,
                 f"页签未被切换（仍 {w5.tabs.currentIndex()}）")
        ck.check(w5.status_panel.state.text() == state_before,
                 "状态栏文案未被改写")
        ck.check(w5._pending_video is None and w5._preview_file is None,
                 "预览/开播状态未被改动")
        ck.check(w5.result is not None and w5._last_source.startswith("magnet:"),
                 "解析结果/来源未被覆盖（早退在赋值之前）")

        # ---- I2 清点：源码级（AST）验证守卫集合与「不加」集合 ----------
        guard_state = {n: _first_guard_state(n)
                       for n in sorted(GUARDED | NOT_GUARDED)}
        bad = {n: s for n, s in guard_state.items()
               if n in GUARDED and s != "guard"}
        ck.check(not bad,
                 f"源码清点：{len(GUARDED)} 个入口/桥回调首条语句均为"
                 f" `if self._shutdown_started: return`（异常 {bad}）")
        leaked = {n: s for n, s in guard_state.items()
                  if n in NOT_GUARDED and s == "guard"}
        ck.check(not leaked,
                 f"源码清点：{len(NOT_GUARDED)} 个「有意不加守卫」的方法确实未加"
                 f"（被误加 {leaked}）")

        # ---- Minor 7：窗口期内重复关闭幂等 ----------------------------
        n_thread = {"n": 0}
        orig_thread = mq.threading.Thread

        class _CountingThread(orig_thread):
            def __init__(self, *a, **k):
                n_thread["n"] += 1
                super().__init__(*a, **k)

        mq.threading.Thread = _CountingThread
        err5 = None
        try:
            w5.close()
            w5.close()
        except Exception as e:            # noqa: BLE001
            err5 = e
        finally:
            mq.threading.Thread = orig_thread
        ck.check(err5 is None, f"窗口期内重复 close() 不抛异常（{err5!r}）")
        ck.check(n_thread["n"] == 0,
                 f"重复 close() **不再起**后台线程（新起 {n_thread['n']} 个）")
        ck.check(w5.isVisible(), "重复 close() 不提前关窗（窗口仍可见）")
        ck.check(w5._shutting_down is False,
                 "重复 close() 不置 _shutting_down（收尾仍未完成）")
        ck.check(w5._shutdown_done is False and ov5.isVisible(),
                 "重复 close() 不重置遮罩/后台状态（遮罩仍在）")

        gate5.release()
        _wait_done(app, w5)
        _wait_hidden(app, w5)
        if getattr(w5, "_shutdown_timer", None) is not None:
            w5._shutdown_timer.stop()
        app.setStyleSheet("")           # 还原：其余段落按无主题基线跑

        # --------------------------------------------------------------- §H
        # Minor 6：异步化组件逆常时 closeEvent 必须回退放行——否则事件被
        # ignore 后窗口永远关不掉（只能杀进程）。
        ck.section("H Minor 6 closeEvent 逆常回退（组件抛异常也能关窗）")
        w6 = MainWindow()
        w6.show()
        _pump(app, 0.2)

        def _boom_overlay():
            raise RuntimeError("模拟遮罩创建失败")

        w6._show_shutdown_overlay = _boom_overlay
        err6 = None
        try:
            w6.close()
        except Exception as e:            # noqa: BLE001
            err6 = e
        ck.check(err6 is None, f"(a) 遮罩异常不外逸出 closeEvent（{err6!r}）")
        ck.check(_wait_hidden(app, w6, timeout=1.0),
                 "(a) 遮罩创建异常：窗口仍被关掉（回退 super().closeEvent）")
        ck.check(w6._shutting_down is True,
                 "(a) 逆常回退置 _shutting_down（不重复进入关闭流程）")
        if getattr(w6, "_shutdown_timer", None) is not None:
            w6._shutdown_timer.stop()

        w7 = MainWindow()
        w7.show()
        _pump(app, 0.2)

        class _BoomThread:
            def __init__(self, *_a, **_k):
                raise RuntimeError("模拟线程创建失败")

        mq.threading.Thread = _BoomThread
        err7 = None
        try:
            w7.close()
        except Exception as e:            # noqa: BLE001
            err7 = e
        finally:
            mq.threading.Thread = orig_thread
        ck.check(err7 is None, f"(b) 线程异常不外逸出 closeEvent（{err7!r}）")
        ck.check(_wait_hidden(app, w7, timeout=1.0),
                 "(b) 收尾线程创建异常：窗口仍被关掉")
        ck.check(w7._shutting_down is True, "(b) 逆常回退置 _shutting_down")
        if getattr(w7, "_shutdown_timer", None) is not None:
            w7._shutdown_timer.stop()
    finally:
        cfg.set("clear_cache_on_exit", orig_clear)

    return ck.report()


if __name__ == "__main__":
    sys.exit(main())
