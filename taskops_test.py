"""core/taskops.py 专项验收（fetcher 重构阶段 4）。

与 download_mgr_test 的分工：后者是端到端（真 libtorrent 会话，118s），
本模块用**假句柄/假会话/假调度器 + 真 TaskRegistry**（内存 cache_dir）把
CRUD 的每条分支压一遍，秒级、不联网。

段：
- §A add_task 入参防御（空源 / 会话未启动 / 磁力链无效）
- §B review 记录转正（D10，两条入口）：convert 在锁内路由、清单 upsert
- §C activate_download：upload_mode 解除 + auto_managed + 文件优先级 +
     所选过滤 + torrent_priority + resume 的调用序列；无句柄跳过；异常吞
- §D 操作 API：set_priority（合法域/非法值/坏状态/句柄炸）、pause_task
     （撤 auto_managed + 清单 PAUSED + 请求 resume）、resume_task
     （元数据未就绪重启看门狗 / 已就绪回 DOWNLOADING）
- §E remove_task：预览联动 stop、delete_files 0/1 两分支、current 别名
     复位与换代、无 rec 有 task 也删、守卫删目录（受管/非受管）
- §F focus_task：别名切换 + 换代 + scheduler.stop；未命中 False
- §G tasks() 快照：派生字段（progress/eta/down_rate/save_subdir/
     selected_files/seed/error 覆盖）、R-3 结构证据（status() 在锁外——
     用「status() 里回头抢锁」探针钉死）、completed 强制 progress=1
- §H Facade 接线：SessionManager 公开面真的打到 TaskOps（防假搬迁）

退出码：0=通过，1=失败，2=SKIP（依赖缺失，绝不假装通过）。
"""
from __future__ import annotations

import os
import sys
import tempfile
import threading
import time
import shutil

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    import libtorrent as lt

    import test_support as ts
    from core.fetcher import SessionManager
    from core.models import ParseResult, TorrentFile
    from core.persist import PersistDeps, TaskPersistence
    from core.registry import TaskRecord, TaskRegistry
    from core.states import (STATE_COMPLETED, STATE_DOWNLOADING,
                             STATE_FAILED, STATE_META_FETCH, STATE_PAUSED,
                             STATE_SEEDING, STATE_STOPPED)
    from core.taskops import TaskOps, lt_priority
except ImportError as e:    # 依赖缺失（模块/导出不存在）：显式 SKIP；
                            # 语法错误/逻辑错误不是 ImportError，会正常冒泡为失败
    print(f"依赖缺失，无法执行 taskops 专项验收：{e}")
    sys.exit(2)

IH = "a" * 40
IH2 = "b" * 40


# --------------------------------------------------------------------------
# 假依赖
# --------------------------------------------------------------------------

class FakeHandle:
    def __init__(self, ih=IH, num_files=0):
        self.ih = ih
        self.num_files = num_files
        self.paused = 0
        self.resumed = 0
        self.set_flags_calls: list = []
        self.unset_flags_calls: list = []
        self.prioritized: list = []
        self.torrent_priorities: list = []
        self.status_raises = False
        self.total_done = 0
        self.down_rate = 0

    def info_hash(self):
        return self.ih

    def pause(self):
        self.paused += 1

    def resume(self):
        self.resumed += 1

    def set_flags(self, f):
        self.set_flags_calls.append(f)

    def unset_flags(self, f):
        self.unset_flags_calls.append(f)

    def prioritize_files(self, prio):
        self.prioritized.append(list(prio))

    def torrent_priority(self, p):
        self.torrent_priorities.append(p)

    def torrent_file(self):
        return FakeTI(self.num_files) if self.num_files else None

    def status(self):
        if self.status_raises:
            raise RuntimeError("句柄失效")
        return FakeStatus(self.total_done, self.down_rate)

    def save_resume_data(self, flags):
        pass


class FakeTI:
    def __init__(self, n):
        self._n = n

    def num_files(self):
        return self._n


class FakeStatus:
    def __init__(self, done, rate):
        self.total_done = done
        self.download_payload_rate = rate


class FakeSes:
    def __init__(self, add_raises=False):
        self.removed: list = []
        self.add_raises = add_raises

    def remove_torrent(self, h, options=0):
        self.removed.append((h, options))

    def add_torrent(self, atp):
        raise RuntimeError("不该被调用")   # add_* 入口专项里单独 monkey


class FakeSched:
    def __init__(self, handle=None):
        self.handle = handle
        self.stops = 0

    def stop(self):
        self.stops += 1


class CountingPersist:
    """替身 TaskPersistence：只记调用。"""

    def __init__(self):
        self.persist_calls = 0
        self.resume_requests: list = []

    def persist_tasks(self):
        self.persist_calls += 1

    def request_resume(self, rec):
        self.resume_requests.append(rec)


def mk_env():
    ws = tempfile.mkdtemp(prefix="mv_taskops_")
    cache = os.path.join(ws, "cache")
    dl = os.path.join(cache, "downloads")
    os.makedirs(dl, exist_ok=True)
    ses = FakeSes()
    reg = TaskRegistry(cache_dir=cache, ses_get=lambda: ses)
    sched = FakeSched()
    ops = TaskOps(reg=reg, persist=None, ses_get=lambda: ses,
                  scheduler_get=lambda: sched, download_dir=dl)
    persist = CountingPersist()
    ops.persist = persist          # 注入替身（生产路径持真 TaskPersistence）
    return ws, cache, dl, reg, ses, sched, ops, persist


def add_rec(reg, ih, handle=None, **kw):
    rec = TaskRecord(handle=handle if handle is not None else FakeHandle(ih),
                     **kw)
    with reg.lock:
        reg.put_record_locked(ih, rec)
    return rec


# --------------------------------------------------------------------------

def section_add_guard(ck):
    ck.section("§A add_task 入参防御")
    ws, cache, dl, reg, ses, sched, ops, persist = mk_env()
    try:
        try:
            ops.add_task("   ")
            ck.check(False, "空源应抛 ValueError")
        except ValueError:
            ck.check(True, "空源 → ValueError（下载来源为空）")
        empty = TaskRegistry(cache_dir=cache, ses_get=lambda: None)
        ops2 = TaskOps(reg=empty, persist=persist, ses_get=lambda: None,
                       scheduler_get=lambda: sched, download_dir=dl)
        try:
            ops2.add_task("magnet:?xt=urn:btih:" + IH)
            ck.check(False, "会话未启动应抛 RuntimeError")
        except RuntimeError:
            ck.check(True,
                     "会话未启动 → RuntimeError（D4：引用一次取用，无检查/使用竞态）")
        try:
            ops.add_task("magnet:?xt=urn:btih:not-valid")
            ck.check(False, "坏磁力链应抛 ValueError")
        except ValueError as e:
            ck.check("磁力链接无效" in str(e), "坏磁力链 → ValueError 含原因")
    finally:
        shutil.rmtree(ws, ignore_errors=True)


def section_convert(ck):
    ck.section("§B review 转正（D10）")
    ws, cache, dl, reg, ses, sched, ops, persist = mk_env()
    try:
        result = ParseResult(info_hash=IH, name="n", total_size=100,
                             piece_size=16384, num_pieces=1,
                             files=[TorrentFile(0, "n.bin", 100, 0, 0, 0)],
                             source="magnet")
        rec = add_rec(reg, IH, result=result, state="READY")
        # 锁内路由（生产语义：add_task 在 with reg.lock 里调它）
        with reg.lock:
            got = ops._convert_to_download_locked(
                rec, IH, "demo.torrent", None, 2, True)
        ck.check(got == IH, "转正返回 info_hash")
        ck.check(rec.download and rec.seed and rec.priority == 2
                 and rec.state == STATE_DOWNLOADING,
                 "记录字段更新 download/seed/priority/state")
        ck.check(reg.tasks.get(IH, {}).get("state") == STATE_DOWNLOADING,
                 "清单 upsert 且状态 DOWNLOADING")
        ck.check(persist.persist_calls == 1, "转正即落盘一次")
        h = rec.handle
        ck.check(h.resumed == 1
                 and lt.torrent_flags.upload_mode in h.unset_flags_calls,
                 "转正即 activate：解除 upload_mode + resume")

        # 无元数据的 review 记录 → META_FETCH + 重启看门狗
        rec2 = add_rec(reg, IH2, result=None, resolving=False,
                       state=STATE_META_FETCH)
        with reg.lock:
            ops._convert_to_download_locked(rec2, IH2, "m", None, 0, False)
        ck.check(rec2.resolving and rec2.resolve_started > 0,
                 "无元数据转正：重启 per-task 看门狗计时")
        ck.check(reg.tasks[IH2]["name"] == "(获取元数据中)",
                 "无元数据清单占位名")
    finally:
        shutil.rmtree(ws, ignore_errors=True)


def section_activate(ck):
    ck.section("§C activate_download 调用序列")
    ws, cache, dl, reg, ses, sched, ops, persist = mk_env()
    try:
        result = ParseResult(info_hash=IH, name="root", total_size=0,
                             piece_size=16384, num_pieces=2,
                             files=[TorrentFile(0, "root/a", 10, 0, 0, 0),
                                    TorrentFile(1, "root/b", 10, 10, 0, 0)],
                             source="magnet")
        reg.tasks[IH] = {"info_hash": IH, "selected": ["root/a"]}
        h = FakeHandle(IH, num_files=2)
        rec = TaskRecord(handle=h, result=result, priority=3)
        with reg.lock:
            reg.put_record_locked(IH, rec)
        ops.activate_download(rec)
        ck.check(lt.torrent_flags.upload_mode in h.unset_flags_calls
                 and lt.torrent_flags.auto_managed in h.set_flags_calls,
                 "解除 upload_mode + 置 auto_managed")
        ck.check(h.prioritized == [[4, 0]],
                 "selected=[root/a] → 文件优先级 [4,0]（未选置 0）")
        ck.check(h.torrent_priorities == [255], "priority=3 → 255")
        ck.check(h.resumed == 1, "resume 恰一次")
        # 无选择清单 = 全选
        reg.tasks[IH] = {"info_hash": IH}
        h2 = FakeHandle(IH, num_files=2)
        rec2 = TaskRecord(handle=h2, result=result, priority=0)
        ops.activate_download(rec2)
        ck.check(h2.prioritized == [[4, 4]], "无 selected 清单 → 全选 4")
        ck.check(h2.torrent_priorities == [], "priority=0 不调 torrent_priority")
        ops.activate_download(TaskRecord(handle=None))
        ck.check(True, "handle=None 安全跳过")
    finally:
        shutil.rmtree(ws, ignore_errors=True)


def section_ops_api(ck):
    ck.section("§D set_priority / pause / resume")
    ws, cache, dl, reg, ses, sched, ops, persist = mk_env()
    try:
        rec = add_rec(reg, IH, download=True, result="R",
                      state=STATE_DOWNLOADING)
        reg.tasks[IH] = {"info_hash": IH, "priority": 0, "state":
                         STATE_DOWNLOADING, "error": "旧错"}
        ck.check(ops.set_priority(IH, 2) is True, "合法优先级 → True")
        ck.check(rec.priority == 2
                 and rec.handle.torrent_priorities == [150],
                 "记录与句柄同步（2 → 150）")
        ck.check(reg.tasks[IH]["priority"] == 2 and persist.persist_calls >= 1,
                 "清单同步 + 落盘")
        ck.check(ops.set_priority(IH, 9) is False, "越界 9 → False")
        ck.check(ops.set_priority(IH, "x") is False, "非数字 → False")
        ck.check(ops.set_priority("f" * 40, 1) is False, "不存在 → False")
        add_rec(reg, IH2, download=True, state=STATE_SEEDING)
        ck.check(ops.set_priority(IH2, 1) is False, "SEEDING 态不可改 → False")

        class BoomTp(FakeHandle):
            def torrent_priority(self, p):
                raise RuntimeError("坏句柄")
        rec_b = add_rec(reg, "e" * 40, download=True,
                        state=STATE_DOWNLOADING)
        rec_b.handle = BoomTp("e" * 40)
        reg.tasks["e" * 40] = {"info_hash": "e" * 40}
        ck.check(ops.set_priority("e" * 40, 1) is True,
                 "句柄 torrent_priority 炸：吞掉仍 True（状态已写）")

        # pause
        ck.check(ops.pause_task(IH) is True, "pause → True")
        ck.check(rec.handle.paused == 1
                 and lt.torrent_flags.auto_managed in rec.handle.unset_flags_calls,
                 "pause + 撤 auto_managed（P1-9 语义）")
        ck.check(rec.state == STATE_PAUSED
                 and reg.tasks[IH]["state"] == STATE_PAUSED
                 and reg.tasks[IH]["error"] == "",
                 "记录与清单 PAUSED + 清 error")
        ck.check(persist.resume_requests == [rec], "pause 后请求 fastresume")
        ck.check(ops.pause_task("f" * 40) is False, "pause 不存在 → False")

        # resume：已就绪 → DOWNLOADING
        ck.check(ops.resume_task(IH) is True, "resume → True")
        ck.check(rec.state == STATE_DOWNLOADING and rec.handle.resumed >= 1,
                 "已就绪记录 resume → DOWNLOADING + 句柄 resume")
        # resume：元数据未就绪 → 重启看门狗
        r2 = add_rec(reg, "c" * 40, download=True, result=None,
                     state=STATE_PAUSED, resolving=False)
        reg.tasks["c" * 40] = {"info_hash": "c" * 40}
        ops.resume_task("c" * 40)
        ck.check(r2.state == STATE_META_FETCH and r2.resolving
                 and r2.resolve_started > 0,
                 "无元数据 resume → META_FETCH + 看门狗重启（失败重试语义）")
    finally:
        shutil.rmtree(ws, ignore_errors=True)


def section_remove(ck):
    ck.section("§E remove_task（守卫删文件 D9）")
    ws, cache, dl, reg, ses, sched, ops, persist = mk_env()
    try:
        # 预览联动：句柄正在被预览 → 先停
        h = FakeHandle(IH)
        add_rec(reg, IH, download=True, handle=h)
        sched.handle = h
        reg.tasks[IH] = {"info_hash": IH}
        ck.check(ops.remove_task(IH) is True, "remove → True")
        ck.check(sched.stops == 1, "预览中句柄：先 scheduler.stop")
        ck.check(ses.removed == [(h, 0)],
                 "delete_files=False：remove_torrent(handle, 0) 保留磁盘文件")
        ck.check(IH not in reg.torrents and IH not in reg.tasks,
                 "记录与清单双注销")

        # current 别名复位 + 换代
        add_rec(reg, IH2, download=True)
        reg.tasks[IH2] = {"info_hash": IH2}
        rec2 = reg.torrents[IH2]
        with reg.lock:
            reg.apply_current_locked(rec2, IH2)
            reg.result = None
        g0 = reg.gen
        ops.remove_task(IH2)
        ck.check(reg.current_ih is None and reg.handle is None
                 and reg.gen == g0 + 1,
                 "删除当前任务：别名复位 + 换代（陈旧解析自弃）")

        # delete_files=True：选项 1 + 守卫删目录
        task_key = "7" * 40
        managed = os.path.join(dl, task_key)
        os.makedirs(managed, exist_ok=True)
        open(os.path.join(managed, "f.bin"), "wb").write(b"x")
        add_rec(reg, task_key, download=True, save_path=managed)
        reg.tasks[task_key] = {"info_hash": task_key, "save_path": managed}
        ck.check(ops.remove_task(task_key, delete_files=True) is True,
                 "delete_files=True → True")
        ck.check(ses.removed[-1][1] == 1, "选项 1：libtorrent 删文件")
        ck.check(not os.path.isdir(managed), "受管目录名==任务键 → 删除")

        # 守卫：目录名不符 → 拒绝
        outside = os.path.join(dl, "user-data")
        os.makedirs(outside, exist_ok=True)
        k2 = "8" * 40
        add_rec(reg, k2, download=True, save_path=outside)
        reg.tasks[k2] = {"info_hash": k2}
        ops.remove_task(k2, delete_files=True)
        ck.check(os.path.isdir(outside), "目录名 != 任务键 → 拒绝删除（D9）")
        shutil.rmtree(outside)

        # 无 rec 有 task（重启后句柄失效残留）：也删清单
        k3 = "9" * 40
        reg.tasks[k3] = {"info_hash": k3}
        ck.check(ops.remove_task(k3) is True, "仅清单有记录 → 删除成功")
        ck.check(ops.remove_task("z" * 40) is False, "两头都没有 → False")
    finally:
        shutil.rmtree(ws, ignore_errors=True)


def section_focus(ck):
    ck.section("§F focus_task")
    ws, cache, dl, reg, ses, sched, ops, persist = mk_env()
    try:
        rec = add_rec(reg, IH, download=True, result="R",
                      resolving=False)
        reg.current_ih = IH2   # 焦点在别处
        g0 = reg.gen
        ck.check(ops.focus_task(IH) is True, "focus → True")
        ck.check(sched.stops == 1, "focus 先停当前预览")
        ck.check(reg.current_ih == IH and reg.result == "R"
                 and reg.gen == g0 + 1, "别名切换 + 换代")
        ck.check(ops.focus_task("f" * 40) is False, "未命中 → False")
    finally:
        shutil.rmtree(ws, ignore_errors=True)


def section_tasks(ck):
    ck.section("§G tasks() 快照 + R-3 出锁派生")
    ws, cache, dl, reg, ses, sched, ops, persist = mk_env()
    try:
        h = FakeHandle(IH)
        h.total_done = 50
        h.down_rate = 10
        add_rec(reg, IH, download=True, handle=h, priority=2, seed=True,
                save_path=dl, error="网络抖动")
        reg.tasks[IH] = {"info_hash": IH, "name": "n", "total_size": 100,
                         "state": STATE_DOWNLOADING, "selected": ["a"],
                         "save_path": os.path.join(cache, "rel")}
        out = ops.tasks()
        ck.check(len(out) == 1, "快照条数=清单条数")
        t = out[0]
        ck.check(t["id"] == IH and t["info_hash"] == IH, "id/info_hash 注入")
        ck.check(t["priority"] == 2 and t["seed"] is True,
                 "priority/seed 以记录（运行时权威）覆盖清单")
        ck.check(t["save_subdir"] == os.path.relpath(
            os.path.join(cache, "rel"), cache).replace("\\", "/"),
            "save_subdir = 清单 save_path 相对 cache_dir")
        ck.check(t["selected_files"] == ["a"], "selected_files 拷贝")
        ck.check(t["progress"] == 0.5 and t["down_rate"] == 10
                 and t["eta"] == 5.0, "progress/eta 由句柄 status 派生")
        ck.check(t["error"] == "网络抖动", "rec.error 覆盖清单 error")

        # total_size=0 → progress 0；COMPLETED → 1.0
        add_rec(reg, IH2, download=True,
                handle=FakeHandle(IH2))
        reg.tasks[IH2] = {"info_hash": IH2, "total_size": 0,
                          "state": STATE_COMPLETED}
        t2 = [x for x in ops.tasks() if x["id"] == IH2][0]
        ck.check(t2["progress"] == 1.0, "COMPLETED 强制 progress=1.0")

        # 句柄 status 抛 → 派生字段归零不抛
        bh = FakeHandle("c" * 40)
        bh.status_raises = True
        add_rec(reg, "c" * 40, download=True, handle=bh)
        reg.tasks["c" * 40] = {"info_hash": "c" * 40, "total_size": 10,
                               "state": STATE_DOWNLOADING}
        t3 = [x for x in ops.tasks() if x["id"] == "c" * 40][0]
        ck.check(t3["progress"] == 0.0 and t3["down_rate"] == 0
                 and t3["eta"] is None, "status() 抛：安全降级零值")

        # R-3 结构钉死：status() 被调时任务锁必须已释放
        # （假句柄的 status 里抢锁——若实现持锁派生，此调用必然超时失败）
        probe_ok = {"v": True}

        class LockProbeHandle(FakeHandle):
            def status(self):
                got = reg.lock.acquire(timeout=0.5)
                if got:
                    reg.lock.release()
                else:
                    probe_ok["v"] = False   # 还在锁内 → 记录失败
                return FakeStatus(0, 0)
        reg.tasks.clear()
        reg.torrents.clear()
        add_rec(reg, "d" * 40, download=True, handle=LockProbeHandle("d" * 40))
        reg.tasks["d" * 40] = {"info_hash": "d" * 40, "total_size": 1}
        ops.tasks()
        ck.check(probe_ok["v"],
                 "R-3：handle.status() 在锁外派生（锁探针在锁内即失败）")
    finally:
        shutil.rmtree(ws, ignore_errors=True)


def section_facade(ck):
    ck.section("§H Facade 接线：SessionManager ↔ TaskOps")
    ws = tempfile.mkdtemp(prefix="mv_taskops_facade_")
    try:
        mgr = SessionManager(os.path.join(ws, "cache"))
        ck.check(isinstance(mgr._taskops, TaskOps),
                 "SessionManager 构造出 TaskOps（真 registry/persist 注入）")
        ck.check(mgr._taskops.reg is mgr._registry
                 and mgr._taskops.persist is mgr._persist,
                 "TaskOps 复用同一 registry/persist 实例（无第二套状态）")
        # 委托路由探针：换 spy 打 tasks/pause/remove/focus 公开面
        real = mgr._taskops
        seen = []

        class Spy:
            def __getattr__(self, name):
                def w(*a, **k):
                    seen.append(name)
                    return getattr(real, name)(*a, **k)
                return w
        mgr._taskops = Spy()
        try:
            try:
                mgr.add_task("  ")
            except ValueError:
                pass   # add_task 空源按预期抛出（路由已发生）
            mgr.set_priority("0" * 40, 1)
            mgr.pause_task("0" * 40)
            mgr.resume_task("0" * 40)
            mgr.remove_task("0" * 40)
            mgr.focus_task("0" * 40)
            mgr.tasks()
        finally:
            mgr._taskops = real
        want = ["add_task", "set_priority", "pause_task", "resume_task",
                "remove_task", "focus_task", "tasks"]
        missing = [n for n in want if n not in seen]
        ck.check(not missing, f"7 个任务 API 全部路由到 TaskOps（缺 {missing or '无'}）")
        # 别名与快照同源的端到端证据：pause 写进 registry 的 tasks
        mgr._registry.tasks[IH] = {"info_hash": IH, "total_size": 0,
                                   "state": STATE_DOWNLOADING}
        snaps = mgr.tasks()
        ck.check(any(x["id"] == IH for x in snaps),
                 "mgr.tasks() 快照看到 registry 清单（property 单源）")
    finally:
        shutil.rmtree(ws, ignore_errors=True)


def main() -> int:
    ck = ts.Checker("taskops_test（阶段 4 任务 CRUD 专项）")
    ck.section("core/taskops.py 专项验收（假句柄 + 真注册表，不联网）")
    section_add_guard(ck)
    section_convert(ck)
    section_activate(ck)
    section_ops_api(ck)
    section_remove(ck)
    section_focus(ck)
    section_tasks(ck)
    section_facade(ck)
    return ck.report()


if __name__ == "__main__":
    sys.exit(main())
