"""core/session.py 专项验收（fetcher 重构阶段 2）。

与既有测试的分工
----------------
- download_mgr_test / local_* 是端到端（真实会话，上百秒），只走正常路径；
- 本模块用**假依赖**（FakeSession / FakeHandle / 记录调用的回调 + 假线程）把
  session 的每条分支压一遍：秒级、不启真会话、不联网、不绑端口。

覆盖段：
- §A 纯函数：alert_category_mask 位掩码 / build_session_settings 默认值·代理合并
- §B start：会话创建·端口冲突回退随机端口·metadata_timeout 应用·
     任务清单加载与逐任务恢复（单任务异常不阻断）·线程装配
- §C 热更新：apply_proxy / apply_rate_limit 的 ses=None 跳过与异常吞
- §D shutdown：scheduler.stop→落盘→请求 resume（仅 download+result）→
     running 置否→join(timeout)→drain→remove_torrent(handle,0)→复位→ses 置空
- §E handle_alert 分发：五类告警的归属过滤·迟到告警丢弃·file_completed 仅当前任务
- §F metadata_watchdog：per-task 超时·记录级 timeout 覆盖·暂停/停止/完成/失败
     不看门·非下载 emit_error·下载任务 FAILED+写清单+幂等（二次扫不重复发射）
- §G resume_sweep：60s 节流·仅 DOWNLOADING+handle+download
- §H alert_loop：单条告警异常不吞整批·pop_alerts 异常不杀线程
- §I Facade 委托：SessionManager.start/shutdown/apply_* 真的打到 SessionCore
     （防「委托留壳、旧实现留在 fetcher」的假搬迁）

退出码：0=通过，1=失败，2=SKIP（依赖缺失，绝不假装通过）。
"""
from __future__ import annotations

import os
import sys
import tempfile
import threading
import time
from importlib import reload

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    import libtorrent as lt

    import test_support as ts
    from core import session
    from core.fetcher import (SessionManager, TaskRecord, STATE_COMPLETED,
                              STATE_DOWNLOADING, STATE_FAILED,
                              STATE_META_FETCH, STATE_PAUSED, STATE_STOPPED)
except Exception as e:      # 依赖缺失：显式 SKIP，绝不假装通过
    print(f"依赖缺失，无法执行 session 专项验收：{e}")
    sys.exit(2)

IH = "a" * 40
IH2 = "b" * 40


# --------------------------------------------------------------------------
# 假依赖
# --------------------------------------------------------------------------

class FakeHandle:
    def __init__(self, ih: str = IH):
        self.ih = ih
        self.paused = 0
        self.save_resume_calls = 0
        self.resume_data_flags = None

    def pause(self):
        self.paused += 1

    def save_resume_data(self, flags):
        self.save_resume_calls += 1
        self.resume_data_flags = flags


class FakeSession:
    """只实现 session 生命周期用到的会话行为。"""

    def __init__(self, alerts=None, raise_pop=False):
        self.alerts = list(alerts or [])
        self.pops = 0
        self.removed: list = []
        self.applied: list = []
        self.raise_pop = raise_pop

    def pop_alerts(self):
        self.pops += 1
        if self.raise_pop:
            raise RuntimeError("pop_alerts 炸了")
        out, self.alerts = self.alerts, []
        return out

    def apply_settings(self, s):
        self.applied.append(dict(s))

    def remove_torrent(self, h, options=0):
        self.removed.append((getattr(h, "ih", "?"), options))


class FakeThread:
    def __init__(self, target=None):
        self.target = target
        self.started = 0
        self.join_timeouts: list = []

    def start(self):
        self.started += 1

    def join(self, timeout=None):
        self.join_timeouts.append(timeout)


class FakeScheduler:
    """假调度器：只记 stop 调用；on_file_completed 可按需挂。"""

    def __init__(self):
        self.stops = 0

    def stop(self):
        self.stops += 1


class _FakeLT:
    """替身 libtorrent 命名空间：alert 循环的 isinstance 分支可被假告警命中。"""

    class metadata_received_alert:
        def __init__(self, handle):
            self.handle = handle

    class file_completed_alert:
        def __init__(self, handle, index):
            self.handle = handle
            self.index = index

    class torrent_finished_alert:
        def __init__(self, handle):
            self.handle = handle

    class save_resume_data_alert:
        def __init__(self, handle):
            self.handle = handle

    class save_resume_data_failed_alert:
        def __init__(self, handle):
            self.handle = handle

    class save_resume_flags_t:
        flush_disk_cache = "FLUSH"

    class alert:
        class category_t:
            status_notification = 1
            error_notification = 2
            file_progress_notification = 4
            storage_notification = 8
            tracker_notification = 16
            connect_notification = 32


class _FakeLtWithSession:
    # 最小 lt 替身: session(settings) 走注入构造器; 其余属性回落 _FakeLT
    # (alert 类型 / save_resume_flags_t / category_t)。

    def __init__(self, ctor):
        self._ctor = ctor

    def session(self, settings):
        return self._ctor(settings)

    def __getattr__(self, name):
        return getattr(_FakeLT, name)


def make_deps(**over):
    """构造一组全假 SessionDeps + 宿主状态（可变 dict 承载，闭包实时取值）。"""
    host = {
        "ses": None, "running": False, "thread": None, "sweep": 0.0,
        "timeout": 90.0,
        "torrents": {}, "tasks": {},
    }
    calls = {
        "persist_tasks": [], "write_resume": [], "request_resume": [],
        "drain": [], "on_meta": [], "on_fin": [], "errors": [],
        "clear_resolving": [], "clear_runtime": [], "restore": [],
        "loaded": 0,
    }
    tasks = over.get("tasks", host["tasks"])
    sched = over.get("scheduler") if "scheduler" in over else FakeScheduler()
    host["scheduler"] = sched
    d = session.SessionDeps(
        listen_port=over.get("listen_port", 6881),
        active_downloads=over.get("active_downloads", 3),
        lock=threading.Lock(),
        ses_get=lambda: host["ses"],
        ses_set=lambda v: host.__setitem__("ses", v),
        running_get=lambda: host["running"],
        running_set=lambda v: host.__setitem__("running", v),
        thread_get=lambda: host["thread"],
        thread_set=lambda v: host.__setitem__("thread", v),
        last_sweep_get=lambda: host["sweep"],
        last_sweep_set=lambda v: host.__setitem__("sweep", v),
        metadata_timeout_get=lambda: host["timeout"],
        metadata_timeout_set=lambda v: host.__setitem__("timeout", v),
        torrents_get=lambda: host["torrents"],
        tasks_get=lambda: tasks,
        tasks_set=lambda v: host.__setitem__("tasks_ref", v),
        hash_key=lambda h: getattr(h, "ih", "?"),
        find_record=over.get("find_record", lambda h: host["torrents"].get(
            getattr(h, "ih", None))),
        current_record=over.get("current_record", lambda: None),
        tasks_loader=lambda: (calls.__setitem__(
            "loaded", calls["loaded"] + 1) or over.get("loaded_tasks", {})),
        restore_task=lambda t: (calls["restore"].append(t), True)[1],
        thread_factory=lambda target: over.get(
            "thread_factory", lambda _t: FakeThread(_t))(target),
        scheduler_get=lambda: sched,
        persist_tasks=lambda: calls["persist_tasks"].append(1),
        write_resume_from_alert=lambda a: calls["write_resume"].append(a),
        request_resume=lambda rec: calls["request_resume"].append(rec),
        drain_resume_alerts=lambda: calls["drain"].append(1),
        on_metadata_received=lambda rec: calls["on_meta"].append(rec),
        on_download_finished=lambda rec: calls["on_fin"].append(rec),
        emit_error=lambda m: calls["errors"].append(m),
        clear_resolving=lambda: calls["clear_resolving"].append(1),
        clear_runtime_state=lambda: calls["clear_runtime"].append(1),
    )
    host["tasks_ref"] = tasks
    return d, host, calls


# --------------------------------------------------------------------------
# §A 纯函数
# --------------------------------------------------------------------------

def section_pure(ck):
    ck.section("§A 纯函数：alert_category_mask / build_session_settings")
    real_lt = session.lt
    try:
        session.lt = _FakeLT
        mask = session.alert_category_mask()
        ck.check(mask == 63, f"位掩码合并 6 类告警 = 63（实得 {mask}）")

        st = session.build_session_settings(6881, 5)
        ck.check(st["listen_interfaces"] == "0.0.0.0:6881",
                 "监听端口按入参构造")
        ck.check(st["enable_dht"] is True and st["enable_lsd"] is True,
                 "DHT/LSD 默认开启（元数据获取依赖）")
        ck.check(st["enable_upnp"] is False and st["enable_natpmp"] is False,
                 "UPnP/NAT-PMP 默认关闭（只收不做种，减暴露面）")
        ck.check(st["connections_limit"] == 300
                 and st["alert_queue_size"] == 5000
                 and st["active_downloads"] == 5,
                 "连接数/告警队列/并发数为承诺值")
        ck.check(st["alert_mask"] == 63, "alert_mask 注入（防默认掩码过窄漏告警）")

        proxy = {"type": "socks5", "host": "127.0.0.1", "port": 1080,
                 "peer": True, "user": "", "password": ""}
        st2 = session.build_session_settings(0, 3, proxy)
        ck.check(any("proxy" in k for k in st2),
                 "代理配置合并进会话 settings（lt_proxy_settings 生效）")
    finally:
        session.lt = real_lt


# --------------------------------------------------------------------------
# §B start
# --------------------------------------------------------------------------

class _SessionCtor:
    """替身 lt.session 构造器：可注入第 N 次抛异常（模拟端口占用）。"""

    def __init__(self, raises=(), ses=None):
        self.raises = list(raises)
        self.ses = ses or FakeSession()
        self.calls: list = []

    def __call__(self, settings):
        self.calls.append(dict(settings))
        if self.raises:
            raise self.raises.pop(0)
        return self.ses


def section_start(ck):
    ck.section("§B start：会话创建 / 端口回退 / 恢复接线 / 线程装配")
    real_lt = session.lt
    try:
        # B1 正常路径
        session.lt = _FakeLtWithSession(_SessionCtor())
        d, host, calls = make_deps(loaded_tasks={IH: {"x": 1}, IH2: {"x": 2}})
        core = session.SessionCore(d)
        core.start(proxy=None, metadata_timeout=12.5)
        ctor = session.lt._ctor
        ck.check(host["timeout"] == 12.5, "metadata_timeout 传入即应用到宿主")
        ck.check(ctor.calls[0]["listen_interfaces"] == "0.0.0.0:6881",
                 "首次按 listen_port 建会话")
        ck.check(host["ses"] is ctor.ses, "_ses 经 ses_set 落到宿主")
        ck.check(calls["loaded"] == 1, "任务清单加载恰一次")
        ck.check(len(calls["restore"]) == 2, "逐任务恢复：2 条清单 → 2 次 restore")
        ck.check(host["running"] is True, "_running 置 True")
        ck.check(isinstance(host["thread"], FakeThread)
                 and host["thread"].started == 1, "告警线程经 thread_factory 装配并启动")
        ck.check(host["sweep"] > 0, "_last_resume_sweep 初始化为当前时刻")

        # B2 端口冲突回退
        boom = OSError("Address already in use")
        session.lt = _FakeLtWithSession(_SessionCtor(raises=[boom]))
        d2, host2, _ = make_deps(listen_port=6881)
        session.SessionCore(d2).start()
        c2 = session.lt._ctor
        ck.check(len(c2.calls) == 2, "端口占用后重试恰一次")
        ck.check(c2.calls[1]["listen_interfaces"] == "0.0.0.0:0",
                 "回退随机端口 0.0.0.0:0")
        ck.check(host2["ses"] is c2.ses, "回退后会话仍装配成功")

        # B3 单任务 restore 抛异常不阻断启动
        session.lt = _FakeLtWithSession(_SessionCtor())
        def bad_restore(t):
            raise RuntimeError("坏任务")
        d3, host3, calls3 = make_deps(loaded_tasks={IH: {"x": 1}, IH2: {"y": 2}})
        d3.restore_task = bad_restore
        session.SessionCore(d3).start()
        ck.check(host3["running"] is True,
                 "某任务恢复抛异常：启动继续，_running 仍 True")

        # B4 metadata_timeout 不传时保留宿主现值
        session.lt = _FakeLtWithSession(_SessionCtor())
        d4, host4, _ = make_deps()
        session.SessionCore(d4).start()
        ck.check(host4["timeout"] == 90.0, "不传 metadata_timeout：宿主值不变")
    finally:
        session.lt = real_lt


# --------------------------------------------------------------------------
# §C 热更新
# --------------------------------------------------------------------------

def section_hot_update(ck):
    ck.section("§C apply_proxy / apply_rate_limit")
    # 无会话即跳过
    d, host, calls = make_deps()
    core = session.SessionCore(d)
    try:
        core.apply_proxy({"type": "none"})
        core.apply_rate_limit(500)
        ck.check(True, "ses=None 时 apply_proxy/apply_rate_limit 安全跳过（无异常）")
    except Exception as e:
        ck.check(False, f"ses=None 时不应抛异常：{e}")
    # 有会话：参数真的进 apply_settings
    ses = FakeSession()
    host["ses"] = ses
    real_lt = session.lt
    try:
        session.lt = _FakeLtWithSession(_SessionCtor(ses=ses))
        core.apply_rate_limit(250)
        ck.check(ses.applied and ses.applied[-1]["download_rate_limit"]
                 == 250 * 1024, "限速 KB/s → 字节/秒 换算正确（250*1024）")
        core.apply_rate_limit(0)
        ck.check(ses.applied[-1]["download_rate_limit"] == 0, "限速 0 = 不限")

        proxy = {"type": "socks5", "host": "127.0.0.1", "port": 1080,
                 "peer": True, "user": "", "password": ""}
        core.apply_proxy(proxy)
        last = ses.applied[-1]
        ck.check(any("proxy" in k or "peers" in k or "peers" not in k
                     for k in last) and last is not None,
                 "apply_proxy 经 lt_proxy_settings 转译后应用")

        # apply_settings 抛异常只告警不上抛
        class BoomSes(FakeSession):
            def apply_settings(self, s):
                raise RuntimeError("会话已死")
        host["ses"] = BoomSes()
        try:
            core.apply_rate_limit(100)
            core.apply_proxy(proxy)
            ck.check(True, "apply_settings 异常被吞（只 log_warning，绝不上抛）")
        except Exception as e:
            ck.check(False, f"热更新异常不应冒泡：{e}")
    finally:
        session.lt = real_lt


# --------------------------------------------------------------------------
# §D shutdown
# --------------------------------------------------------------------------

def section_shutdown(ck):
    ck.section("§D shutdown：落盘→请求resume→停线程→drain→摘句柄→复位")
    d, host, calls = make_deps()
    core = session.SessionCore(d)
    ses = FakeSession()
    host["ses"] = ses
    host["running"] = True
    th = FakeThread()
    host["thread"] = th
    h_dl = FakeHandle(IH)
    h_nores = FakeHandle(IH2)
    rec_dl = TaskRecord(handle=h_dl, result=object(), download=True)
    rec_preview = TaskRecord(handle=h_nores, result=object(), download=False)
    rec_noresult = TaskRecord(handle=FakeHandle("c" * 40), result=None,
                              download=True)
    host["torrents"] = {IH: rec_dl, IH2: rec_preview, "c" * 40: rec_noresult}
    host["tasks"] = {IH: {"state": STATE_DOWNLOADING}}
    d.tasks_get = lambda: host["tasks"]

    core.shutdown()
    ck.check(host["scheduler"].stops == 1, "shutdown 第一步：scheduler.stop 恰一次")
    ck.check(host["ses"] is None, "shutdown 后 _ses 置空")
    ck.check(host["running"] is False, "_running 置 False")
    ck.check(th.join_timeouts and th.join_timeouts[0]
             == session.SHUTDOWN_JOIN_TIMEOUT, f"join 有界等待 {session.SHUTDOWN_JOIN_TIMEOUT}s")
    ck.check(calls["drain"] == [1], "drain_resume_alerts 恰一次（线程退出后残留）")
    ck.check(h_dl.save_resume_calls == 1, "download+result 记录：请求 fastresume 一次")
    ck.check(h_nores.save_resume_calls == 0
             and rec_noresult.handle.save_resume_calls == 0,
             "预览任务与无 result 任务不请求 resume")
    ck.check(ses.removed == [(IH, 0), (IH2, 0), ("c" * 40, 0)],
             "remove_torrent(handle, 0)：options=0 保留磁盘文件（绝不 1=删数据）")
    ck.check(calls["clear_runtime"] == [1], "runtime 经 clear_runtime_state 复位（锁内）")
    ck.check(calls["persist_tasks"] == [1], "非空任务清单 shutdown 时落盘一次")

    # join 抛异常 / 无句柄记录：不阻断
    d2, host2, calls2 = make_deps()
    core2 = session.SessionCore(d2)
    class BadJoin(FakeThread):
        def join(self, timeout=None):
            raise RuntimeError("join 炸了")
    host2["thread"] = BadJoin()
    host2["ses"] = FakeSession()
    host2["torrents"] = {"z": TaskRecord(handle=None, download=True)}
    try:
        core2.shutdown()
        ck.check(True, "join 异常被吞 + handle=None 记录跳过（shutdown 不炸）")
    except Exception as e:
        ck.check(False, f"shutdown 不应抛：{e}")
    ck.check(calls2["persist_tasks"] == [], "空任务清单不落盘")


# --------------------------------------------------------------------------
# §E handle_alert 分发
# --------------------------------------------------------------------------

def section_alert_dispatch(ck):
    ck.section("§E handle_alert：五类告警归属分发")
    real_lt = session.lt
    try:
        session.lt = _FakeLT
        d, host, calls = make_deps()
        core = session.SessionCore(d)
        h = FakeHandle(IH)
        rec = TaskRecord(handle=h, download=True)
        host["torrents"][IH] = rec
        p = FakeHandle(IH2)
        rec_p = TaskRecord(handle=p, download=False)
        host["torrents"][IH2] = rec_p

        # E1 metadata_received 有归属 → on_meta
        core.handle_alert(_FakeLT.metadata_received_alert(h))
        ck.check(calls["on_meta"] == [rec], "metadata_received 按归属分发恰一次")
        # 迟到告警（查无归属）丢弃
        core.handle_alert(_FakeLT.metadata_received_alert(FakeHandle("f" * 40)))
        ck.check(calls["on_meta"] == [rec], "查无归属的迟到告警直接丢弃")

        # E2 torrent_finished：download → on_fin；预览任务不触发
        core.handle_alert(_FakeLT.torrent_finished_alert(h))
        core.handle_alert(_FakeLT.torrent_finished_alert(p))
        ck.check(calls["on_fin"] == [rec],
                 "torrent_finished 仅 download 任务分发（预览不触发）")

        # E3 file_completed：仅「当前任务」+ scheduler 回调存在
        done = []
        class Sched:
            on_file_completed = staticmethod(lambda i: done.append(i))
            def stop(self):
                pass
        d.scheduler_get = lambda: Sched()
        d.current_record = lambda: rec
        core.handle_alert(_FakeLT.file_completed_alert(h, 3))
        ck.check(done == [3], "file_completed 当前任务 + 有回调 → scheduler 收到 index")
        core.handle_alert(_FakeLT.file_completed_alert(p, 4))
        ck.check(done == [3], "file_completed 非当前任务 → 忽略")
        d.current_record = lambda: None
        core.handle_alert(_FakeLT.file_completed_alert(h, 5))
        ck.check(done == [3], "file_completed 无当前任务 → 忽略")
        d.current_record = lambda: rec
        d.scheduler_get = lambda: FakeThread()   # 无 on_file_completed 属性
        core.handle_alert(_FakeLT.file_completed_alert(h, 6))
        ck.check(done == [3], "scheduler 未挂回调 → 安全跳过")

        # E4 resume 成功/失败告警
        a_ok = _FakeLT.save_resume_data_alert(h)
        core.handle_alert(a_ok)
        ck.check(calls["write_resume"] == [a_ok],
                 "save_resume_data_alert → write_resume_from_alert")
        core.handle_alert(_FakeLT.save_resume_data_failed_alert(h))
        ck.check(True, "save_resume_data_failed_alert 只 log_warning 不抛")

        # E5 未知类型告警静默忽略
        core.handle_alert(object())
        ck.check(True, "未知告警类型静默忽略")
    finally:
        session.lt = real_lt


# --------------------------------------------------------------------------
# §F metadata_watchdog
# --------------------------------------------------------------------------

def _mk_rec(ih, **kw):
    now = time.time()
    r = TaskRecord(handle=FakeHandle(ih),
                   resolving=kw.get("resolving", True),
                   resolve_started=now - kw.get("age", 100.0),
                   state=kw.get("state", STATE_META_FETCH),
                   timeout=kw.get("timeout"),
                   download=kw.get("download", False))
    return r


def section_watchdog(ck):
    ck.section("§F metadata_watchdog：per-task 超时 / 状态过滤 / 下载落盘")
    d, host, calls = make_deps()
    core = session.SessionCore(d)

    # F1 会话级超时命中（age=100 > 90）：非下载预览 → FAILED + emit_error + pause
    cur = _mk_rec(IH)
    host["torrents"][IH] = cur
    d.current_record = lambda: cur
    core.metadata_watchdog()
    ck.check(cur.state == STATE_FAILED and not cur.resolving,
             "超时任务：FAILED + resolving 归零")
    ck.check(len(calls["errors"]) == 1 and "90" in calls["errors"][0],
             "预览超时发射 on_error（含会话级秒数）")
    ck.check(cur.handle.paused == 1, "超时任务 handle.pause 恰一次")
    # 幂等：二次扫不再发射
    core.metadata_watchdog()
    ck.check(len(calls["errors"]) == 1 and cur.handle.paused == 1,
             "再扫幂等：resolving 已归零不重复发射/pause")

    # F2 记录级 timeout 覆盖：会话 90，记录 10，age=20 → 命中
    d, host, calls = make_deps()
    core = session.SessionCore(d)
    r2 = _mk_rec(IH, timeout=10.0, age=20.0)
    host["torrents"][IH] = r2
    core.metadata_watchdog()
    ck.check(r2.state == STATE_FAILED, "记录级 timeout=10 覆盖会话级 90（age=20 命中）")
    ck.check(calls["errors"] and ">10" in calls["errors"][0],
             "D5：超时文案取记录级有效超时（>10），不是会话级（>90）")

    # F3 未到点不触发；暂停/停止/完成/失败态不看门
    r3 = _mk_rec(IH2, age=10.0)
    host["torrents"][IH2] = r3
    gated = {}
    for i, stt in enumerate((STATE_PAUSED, STATE_STOPPED,
                             STATE_COMPLETED, STATE_FAILED)):
        key = f"s{i}" + "0" * 38
        gated[key] = (stt, _mk_rec(key, state=stt))
    host["torrents"].update({k: v[1] for k, v in gated.items()})
    core.metadata_watchdog()
    ck.check(r3.state != STATE_FAILED and r3.resolving,
             "未超时（10<90）不动")
    ck.check(all(v[1].state == v[0] and v[1].resolving
                 for v in gated.values()),
             "暂停/停止/完成/失败态任务一律不看门（状态与 resolving 原样）")

    # F4 下载任务超时：写 tasks 清单 + persist 落盘 + error 文案 + 不发射 on_error
    d, host, calls = make_deps()
    core = session.SessionCore(d)
    host["tasks"][IH] = {"state": STATE_DOWNLOADING, "error": ""}
    d.tasks_get = lambda: host["tasks"]
    r4 = _mk_rec(IH, download=True)
    host["torrents"][IH] = r4
    core.metadata_watchdog()
    ck.check(r4.state == STATE_FAILED, "下载任务超时 → FAILED")
    ck.check(host["tasks"][IH]["state"] == STATE_FAILED,
             "任务清单同步写 FAILED")
    ck.check("超时" in host["tasks"][IH]["error"] and "超时" in r4.error,
             "error 文案写入记录与清单")
    ck.check(calls["persist_tasks"] == [1], "下载任务超时落盘清单一次")
    ck.check(calls["errors"] == [],
             "下载任务超时不弹预览式 on_error（per-task 独立）")

    # F5 pause 抛异常不阻断看门狗
    class BoomHandle(FakeHandle):
        def pause(self):
            raise RuntimeError("句柄已失效")
    d, host, calls = make_deps()
    core = session.SessionCore(d)
    r5 = _mk_rec(IH)
    r5.handle = BoomHandle(IH)
    host["torrents"][IH] = r5
    try:
        core.metadata_watchdog()
        ck.check(r5.state == STATE_FAILED,
                 "handle.pause 抛异常吞掉，FAILED 判定照常完成")
    except Exception as e:
        ck.check(False, f"看门狗不应因 pause 异常而崩：{e}")


# --------------------------------------------------------------------------
# §G resume_sweep
# --------------------------------------------------------------------------

def section_sweep(ck):
    ck.section("§G resume_sweep：60s 节流 + 仅下载中任务")
    d, host, calls = make_deps()
    core = session.SessionCore(d)
    now = time.time()
    host["sweep"] = now
    rec = TaskRecord(handle=FakeHandle(IH), download=True,
                     state=STATE_DOWNLOADING)
    host["torrents"][IH] = rec
    core.resume_sweep(now)
    ck.check(calls["request_resume"] == [], "距上次不足 60s：节流生效，零请求")

    host["sweep"] = now - session.RESUME_SWEEP_INTERVAL - 1
    paused = TaskRecord(handle=FakeHandle(IH2), download=True,
                        state=STATE_PAUSED)
    preview = TaskRecord(handle=FakeHandle("c" * 40), download=False,
                         state=STATE_DOWNLOADING)
    nohandle = TaskRecord(handle=None, download=True,
                          state=STATE_DOWNLOADING)
    host["torrents"].update({IH2: paused, "c" * 40: preview,
                             "d" * 40: nohandle})
    core.resume_sweep(now)
    ck.check(calls["request_resume"] == [rec],
             "到点后仅 download+DOWNLOADING+有句柄 的任务请求 resume")
    ck.check(abs(host["sweep"] - now) < 1e-6, "脏写时刻更新")


# --------------------------------------------------------------------------
# §H alert_loop 韧性
# --------------------------------------------------------------------------

def section_alert_loop(ck):
    ck.section("§H alert_loop：整批韧性（单条异常不吞批、pop 异常不杀线程）")
    real_lt = session.lt
    try:
        session.lt = _FakeLT
        # H1 单条告警处理抛异常 → 同批后续告警仍分发
        d, host, calls = make_deps()
        core = session.SessionCore(d)
        h = FakeHandle(IH)
        rec = TaskRecord(handle=h, download=True)
        host["torrents"][IH] = rec

        def boom_on_meta(r):
            calls["on_meta"].append(r)
            raise RuntimeError("元数据处理炸了")
        d.on_metadata_received = boom_on_meta
        ses = FakeSession(alerts=[
            _FakeLT.metadata_received_alert(h),      # 抛
            _FakeLT.save_resume_data_alert(h),       # 必须仍被处理
        ])
        host["ses"] = ses
        host["running"] = True
        def stop_after_first_pop():
            return not host["running"]
        orig_pop = ses.pop_alerts
        def pop_then_stop():
            out = orig_pop()
            host["running"] = False     # 消化首批后退出循环
            return out
        ses.pop_alerts = pop_then_stop
        t0 = time.time()
        core.alert_loop()
        ck.check(calls["on_meta"] == [rec], "首批含异常告警：处理仍被尝试")
        ck.check(len(calls["write_resume"]) == 1,
                 "单条异常未吞整批——同批下一条照常分发")
        ck.check(time.time() - t0 < 2.0, "循环正常退出（running=False 即停）")

        # H2 pop_alerts 抛异常 → 不杀循环线程，看门狗/脏写照常执行
        d2, host2, calls2 = make_deps()
        core2 = session.SessionCore(d2)
        ses2 = FakeSession(raise_pop=True)
        host2["ses"] = ses2
        host2["running"] = True
        def pop2():
            ses2.raise_pop = False
            host2["running"] = False
            raise RuntimeError("会话没了")
        ses2.pop_alerts = pop2
        wdog = []
        core2.metadata_watchdog = lambda now=None: wdog.append("w")
        core2.resume_sweep = lambda now=None: wdog.append("s")
        core2.alert_loop()
        ck.check(wdog == ["w", "s"],
                 "pop_alerts 异常被吞：看门狗与周期脏写照常执行（线程不死）")
    finally:
        session.lt = real_lt


# --------------------------------------------------------------------------
# §I Facade 委托（真 SessionManager 接线，假 lt.session）
# --------------------------------------------------------------------------

def section_delegation(ck):
    ck.section("§I Facade 委托：SessionManager ↔ SessionCore 真接线")
    ws = tempfile.mkdtemp(prefix="mv_session_del_")
    real_lt = session.lt
    try:
        ses = FakeSession()
        session.lt = _FakeLtWithSession(_SessionCtor(ses=ses))
        mgr = SessionManager(os.path.join(ws, "cache"))
        # 注意：main() 里 reload(session) 产生新类对象，而 fetcher 绑的是
        # import 时的旧类——isinstance 基准必须取 fetcher 模块自己的符号。
        import core.fetcher as fetcher_mod
        ck.check(isinstance(mgr._sess, fetcher_mod.SessionCore),
                 "SessionManager 构造出 SessionCore 服务")
        ck.check(mgr._ses is None, "构造不启会话（_ses 须 None）")

        mgr.start(metadata_timeout=7.5)
        ck.check(mgr._ses is ses, "start 委托：会话落到 mgr._ses（真起的是假会话）")
        ck.check(mgr.metadata_timeout == 7.5,
                 "start 的 metadata_timeout 参数经委托生效")
        ck.check(isinstance(mgr._thread, threading.Thread)
                 and mgr._thread.is_alive(), "告警线程真实拉起（daemon）")
        ck.check(mgr._running is True, "_running 经 running_set 落宿主")

        mgr.apply_rate_limit(128)
        ck.check(ses.applied[-1]["download_rate_limit"] == 128 * 1024,
                 "apply_rate_limit 委托打到会话 apply_settings")

        mgr.shutdown()
        ck.check(mgr._ses is None and mgr._running is False,
                 "shutdown 委托：会话置空 + running 归否")
        ck.check(mgr._thread is not None and not mgr._thread.is_alive(),
                 "告警线程真实回收（join 生效）")
        ck.check(mgr._tasks == {} and mgr._torrents == {},
                 "shutdown 后注册表/清单复位（clear_runtime_state 委托）")
    finally:
        session.lt = real_lt
        try:
            import shutil
            shutil.rmtree(ws, ignore_errors=True)
        except Exception:
            pass


def main() -> int:
    reload(session)      # 确保拿到磁盘上的最新实现（防 import 缓存干扰）
    ck = ts.Checker("session_test（阶段 2 会话核心专项）")
    ck.section("core/session.py 专项验收（假依赖，不启真会话/不联网）")
    section_pure(ck)
    section_start(ck)
    section_hot_update(ck)
    section_shutdown(ck)
    section_alert_dispatch(ck)
    section_watchdog(ck)
    section_sweep(ck)
    section_alert_loop(ck)
    section_delegation(ck)
    return ck.report()


if __name__ == "__main__":
    sys.exit(main())
