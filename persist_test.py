"""core/persist.py 专项验收（fetcher 重构阶段 1）。

与 download_mgr_test 的分工
--------------------------
download_mgr_test §6 / §10-B4 是**端到端**：真实 libtorrent 会话跑一遍
「退出写 resume → 重启读回续传 / resume 损坏重建」。那条链路一次上百秒，
且难以触达边界分支（来源失效、add 失败、暂停态、临时键等）。

本模块用**假依赖**（FakeHandle / FakeSession / 记录调用的回调）把 persist
的每条分支压一遍，秒级完成、不启会话、不联网：

- §A 纯函数：is_within / task_dir / save_subdir_of / safe_task_save_path / read_resume
- §B 任务清单：原子写成功、写失败只告警不抛
- §C fastresume：request_resume 的跳过条件、write_resume_from_alert 的归属校验
- §D 退出清理：有界等待、幂等重发、超时告警、异常不扩散
- §E 启动恢复：磁力链 / resume 损坏 / .torrent / 来源失效 / add 失败 /
     暂停停止完成态 / 元数据就绪 / 入表持锁
- §F Facade 委托：确认 SessionManager 的薄委托真的打到了 persist
     （防「委托留壳、实现留在 fetcher 里」的假搬迁）

真 resume data 的编解码与 libtorrent 互通由 smoke_test [2d] 覆盖，
端到端续传由 download_mgr_test §6/§7 覆盖，此处不重复。

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
    from core import persist
    from core.fetcher import SessionManager, TaskRecord
    from core.parser import bdecode, bencode
    from core.persist import PersistDeps, TaskPersistence
    from core.resume import RESUME_SUBDIR, resume_dir, resume_path, write_resume
    from core.states import (BOOTSTRAP_TRACKERS, STATE_COMPLETED,
                             STATE_DOWNLOADING, STATE_FAILED, STATE_META_FETCH,
                             STATE_PAUSED, STATE_STOPPED)
    from core.taskstore import load_tasks
except ImportError as e:    # 依赖缺失（模块/导出不存在）：显式 SKIP；
                            # 语法错误/逻辑错误不是 ImportError，会正常冒泡为失败
    print(f"依赖缺失，无法执行 persist 专项验收：{e}")
    sys.exit(2)

IH = "a" * 40                       # 合法 v1 info_hash（fastresume 文件名要求）
IH2 = "b" * 40
MAGNET = f"magnet:?xt=urn:btih:{IH}&dn=demo"


# --------------------------------------------------------------------------
# 假依赖
# --------------------------------------------------------------------------

class FakeHandle:
    """只实现 persist 会用到的句柄行为，并计数。"""

    def __init__(self, ih: str = IH, has_meta: bool = False):
        self.ih = ih
        self.has_meta = has_meta
        self.paused = 0
        self.unset = 0
        self.resumed = 0
        self.save_resume_calls = 0

    def pause(self):
        self.paused += 1

    def unset_flags(self, flags):
        self.unset += 1

    def resume(self):
        self.resumed += 1

    def save_resume_data(self, flags):
        self.save_resume_calls += 1

    def torrent_file(self):
        return object() if self.has_meta else None


class FakeAlert:
    def __init__(self, handle, resume_data=None):
        self.handle = handle
        self.resume_data = resume_data


class _FakeLT:
    """替身 libtorrent 命名空间：让 drain 里的 isinstance 分支可被假告警命中。

    真实 lt.alert 类型无法凭空构造，而 drain 只用到两个类型与一个常量，
    用替身即可覆盖那两条分支。
    """

    class save_resume_data_alert(FakeAlert):
        pass

    class save_resume_data_failed_alert(FakeAlert):
        pass

    class save_resume_flags_t:
        flush_disk_cache = "FLUSH"


class FakeSession:
    def __init__(self, alerts=None, raise_pop=False, has_meta=False):
        self.alerts = list(alerts or [])
        self.pops = 0
        self.added: list = []
        self.handles: list = []
        self.raise_pop = raise_pop
        self.has_meta = has_meta

    def pop_alerts(self):
        self.pops += 1
        if self.raise_pop:
            raise RuntimeError("pop_alerts 炸了")
        out, self.alerts = self.alerts, []
        return out

    def add_torrent(self, atp):
        self.added.append(atp)
        h = FakeHandle(has_meta=self.has_meta)
        self.handles.append(h)
        return h


class Recorder:
    """记录宿主回调的调用参数。"""

    def __init__(self, result=None):
        self.calls: list = []
        self.result = result

    def __call__(self, *a, **kw):
        self.calls.append((a, kw))
        return self.result


# --------------------------------------------------------------------------
# 依赖装配
# --------------------------------------------------------------------------

def _makedirs(*paths: str) -> None:
    for p in paths:
        os.makedirs(p, exist_ok=True)


def make_deps(ws: ts.WorkSpace, ses=None, torrents=None, tasks=None):
    """构造一组可插拔依赖；返回 (deps, ctx)，ctx 暴露所有可变状态与调用记录。"""
    cache_dir = ws.cache
    download_dir = os.path.join(cache_dir, "downloads")
    _makedirs(cache_dir, download_dir)
    ctx = {
        "lock": threading.Lock(),
        "torrents": torrents if torrents is not None else {},
        "tasks": tasks if tasks is not None else {},
        "ses": [ses],
        "put": Recorder(),
        "result_from_ti": Recorder(result="PARSE_RESULT"),
        "activate": Recorder(),
        "find": Recorder(),
    }
    deps = PersistDeps(
        cache_dir=cache_dir, download_dir=download_dir, lock=ctx["lock"],
        tasks_get=lambda: ctx["tasks"],
        torrents_get=lambda: ctx["torrents"],
        gen_get=lambda: 7,
        ses_get=lambda: ctx["ses"][0],
        hash_key=lambda h: getattr(h, "ih", IH),
        find_record=lambda h: ctx["find"](h),
        put_record=ctx["put"],
        record_cls=TaskRecord,
        result_from_torrent_info=ctx["result_from_ti"],
        activate_download=ctx["activate"])
    return deps, ctx


def write_torrent_file(payload: str, out: str) -> str:
    """生成真实 .torrent 文件（restore_task 的种子分支需要可解析的文件）。"""
    fs = lt.file_storage()
    lt.add_files(fs, payload)
    ct = lt.create_torrent(fs, 16 * 1024)
    lt.set_piece_hashes(ct, os.path.dirname(payload))
    with open(out, "wb") as f:
        f.write(bencode(ct.generate()))
    return out


def _raise(exc: Exception):
    raise exc


# --------------------------------------------------------------------------
# §A 纯函数
# --------------------------------------------------------------------------

def section_pure_funcs(ck: ts.Checker) -> None:
    ck.section("§A 纯函数：路径卫生与目录计算")
    root = os.path.abspath(os.path.join(tempfile.gettempdir(), "mv_persist_root"))
    # 上一次运行可能在共享临时根里留下 resume 文件，先清掉再断言「不存在」
    try:
        os.remove(resume_path(root, IH))
    except OSError:
        pass

    ck.check(persist.is_within(root, os.path.join(root, "sub", "f.bin")),
             "is_within：root 内子孙路径为真")
    ck.check(persist.is_within(root, root), "is_within：root 自身为真")
    ck.check(not persist.is_within(root, root + "_evil"),
             "is_within：同前缀兄弟目录为假（startswith 的经典绕过）")
    ck.check(not persist.is_within(root, os.path.join(root + "_evil", "x")),
             "is_within：同前缀兄弟目录下的文件为假")
    ck.check(not persist.is_within(root, os.path.join(root, "..", "root_evil", "x")),
             "is_within：用 .. 跳出后为假（先规范化再判定）")
    ck.check(persist.is_within(root, os.path.join(os.path.dirname(root),
                                                  os.path.basename(root), "x")),
             "is_within：等价写法（含冗余目录名）仍为真")
    if os.name == "nt":
        ck.check(persist.is_within(root.upper(), os.path.join(root, "x")),
                 "is_within：大小写差异不影响判定（normcase）")
    other = "D:\\other_root\\x" if os.name == "nt" else "relative/x"
    ck.check(not persist.is_within(root, other),
             "is_within：无公共前缀（ValueError）时安全返回假而非崩溃")

    dl = os.path.join(root, "downloads")
    ck.check(persist.task_dir(dl, IH) == os.path.join(dl, IH),
             "task_dir：默认子目录 = info_hash")
    ck.check(persist.task_dir(dl, IH, "season1") == os.path.join(dl, "season1"),
             "task_dir：自定义子目录生效")
    ck.check(persist.task_dir(dl, "") == dl,
             "task_dir：ih 与子目录皆空时回退下载根")
    escaped = persist.task_dir(dl, IH, "../../escape_evil")
    ck.check(persist.is_within(dl, escaped) and ".." not in escaped,
             f"task_dir：穿越型 save_subdir 被净化（{os.path.basename(escaped)}）")
    ck.check(persist.task_dir(dl, IH, "/abs/sub") == os.path.join(dl, "abs", "sub"),
             "task_dir：绝对型子目录降级为相对段")
    ck.check(persist.task_dir(dl, IH, "..") == os.path.join(dl, "unnamed"),
             "task_dir：纯 .. 子目录不逃逸（safe_rel_path 兜底命名）")

    ck.check(persist.save_subdir_of(root, os.path.join(root, "downloads", IH))
             == f"downloads/{IH}", "save_subdir_of：缓存目录内给相对路径（正斜杠）")
    outside = os.path.abspath(os.path.join(root, "..", "outside", IH))
    ck.check(persist.save_subdir_of(root, outside) == outside,
             "save_subdir_of：缓存目录外给绝对路径")

    ck.check(persist.safe_task_save_path(root, dl, IH,
                                         os.path.join(root, "downloads", IH))
             == os.path.join(root, "downloads", IH),
             "safe_task_save_path：cache_dir 内的落盘路径原样保留")
    ck.check(persist.safe_task_save_path(root, dl, IH, os.path.join(dl, "keep"))
             == os.path.join(dl, "keep"),
             "safe_task_save_path：download_dir 内的落盘路径原样保留")
    ck.check(persist.safe_task_save_path(root, dl, IH,
                                         os.path.join(root, "..", "evil"))
             == os.path.join(dl, IH),
             "safe_task_save_path：逃出受管范围则回退默认任务目录")
    ck.check(persist.safe_task_save_path(root, dl, IH, "")
             == os.path.join(dl, IH),
             "safe_task_save_path：空值回退默认任务目录")

    ck.check(persist.read_resume(root, IH) is None,
             "read_resume：文件不存在返回 None（不抛）")
    write_resume(root, IH, {b"info-hash": b"\x01" * 20})
    ck.check(bool(persist.read_resume(root, IH)),
             "read_resume：已落盘的 resume 可读回")
    ck.check(RESUME_SUBDIR in resume_path(root, IH),
             "resume 路径约定不变：<cache>/.resume/<ih>.fastresume")

    ck.check(persist.is_resume_key(IH) and persist.is_resume_key("c" * 64),
             "is_resume_key：40/64 位 hex 为真")
    ck.check(not persist.is_resume_key("tmp-140123")
             and not persist.is_resume_key("")
             and not persist.is_resume_key("../evil"),
             "is_resume_key：临时键/空串/穿越串为假")


def probe_missing_and_pending(ck: ts.Checker) -> None:
    """键筛选：临时键不得进入「待落盘」集合（否则退出清理会 ValueError）。"""
    with ts.WorkSpace(prefix="mv_persist_keys_") as ws:
        rec_ok = TaskRecord(handle=FakeHandle(IH), result="r", download=True)
        rec_tmp = TaskRecord(handle=FakeHandle("tmp-9"), result="r",
                             download=True)
        rec_prev = TaskRecord(handle=FakeHandle(IH2), result="r",
                              download=False)
        torrents = {IH: rec_ok, "tmp-9": rec_tmp, IH2: rec_prev}
        miss = persist.missing_resume_keys(ws.cache, torrents)
        ck.check(sorted(miss) == sorted([IH, IH2]),
                 f"missing_resume_keys：跳过临时键，保留合法键（{sorted(miss)}）")
        pend = persist.pending_resume_keys(ws.cache, torrents)
        ck.check(pend == [IH],
                 "pending_resume_keys：进一步排除预览记录（非 download）")
        write_resume(ws.cache, IH, {b"info-hash": b"\x01" * 20})
        ck.check(persist.pending_resume_keys(ws.cache, torrents) == [],
                 "pending_resume_keys：已落盘的任务不再计入待落盘")
        ck.check(persist.pending_resume_keys(ws.cache, {}) == [],
                 "pending_resume_keys：空注册表不报错")


# --------------------------------------------------------------------------
# §B 任务清单
# --------------------------------------------------------------------------

def section_task_list(ck: ts.Checker) -> None:
    ck.section("§B 任务清单：原子写与失败不扩散")
    with ts.WorkSpace(prefix="mv_persist_list_") as ws:
        tasks = {IH: {"info_hash": IH, "name": "demo", "state": STATE_DOWNLOADING,
                      "total_size": 1024, "files": [], "selected": [],
                      "priority": 0, "save_path": "", "error": "", "retries": 0,
                      "created_at": time.time(), "finished_at": None, "seed": False,
                      "source": MAGNET}}
        deps, _ = make_deps(ws, tasks=tasks)
        TaskPersistence(deps).persist_tasks()
        back = load_tasks(ws.cache)
        ck.check(IH in back and back[IH]["name"] == "demo",
                 "persist_tasks：写出的清单可被 load_tasks 读回")

        origin = persist.save_tasks
        try:
            persist.save_tasks = lambda *a, **kw: _raise(OSError("磁盘已满"))
            TaskPersistence(deps).persist_tasks()
            ck.check(True, "persist_tasks：写失败只告警、不向上抛")
        finally:
            persist.save_tasks = origin
        ck.check(IH in load_tasks(ws.cache),
                 "persist_tasks：写失败后旧清单未被破坏")


# --------------------------------------------------------------------------
# §C fastresume 请求与落盘
# --------------------------------------------------------------------------

def section_resume(ck: ts.Checker) -> None:
    ck.section("§C fastresume：请求条件与告警归属")
    with ts.WorkSpace(prefix="mv_persist_res_") as ws:
        deps, ctx = make_deps(ws)
        tp = TaskPersistence(deps)

        h = FakeHandle()
        rec = TaskRecord(handle=h, result=None, download=True)
        tp.request_resume(rec)
        ck.check(h.save_resume_calls == 0, "request_resume：结果未就绪的请求任务跳过")
        rec.result = "PARSE_RESULT"
        tp.request_resume(rec)
        ck.check(h.save_resume_calls == 1, "request_resume：下载任务正常发起请求")

        tp.request_resume(TaskRecord(handle=None, result="r", download=True))
        tp.request_resume(TaskRecord(handle=FakeHandle(), result="r",
                                     download=False))
        tp.request_resume(None)
        ck.check(True,
                 "request_resume：句柄缺失/非下载任务/空记录三类入参均安全跳过")

        boom = TaskRecord(handle=FakeHandle(), result="r", download=True)
        boom.handle.save_resume_data = lambda flags: _raise(RuntimeError("句柄失效"))
        tp.request_resume(boom)
        ck.check(True, "request_resume：句柄抛异常只告警、不扩散")

        seen: list = []
        origin_write = persist.write_resume
        try:
            persist.write_resume = lambda *a: seen.append(a) or "path"
            ctx["find"].result = None
            tp.write_resume_from_alert(FakeAlert(FakeHandle()))
            ck.check(not seen,
                     "write_resume_from_alert：查不到归属记录则不落盘（迟到告警）")
            ctx["find"].result = TaskRecord(handle=FakeHandle(IH2), download=True)
            alert = FakeAlert(FakeHandle(IH2), {b"info-hash": b"\x02" * 20})
            tp.write_resume_from_alert(alert)
            ck.check(seen and seen[-1][1] == IH2
                     and seen[-1][2] == alert.resume_data,
                     "write_resume_from_alert：归属命中后按句柄 info_hash 落盘")
        finally:
            persist.write_resume = origin_write
        ck.check(persist.write_resume.__module__.endswith("resume"),
                 "write_resume 已还原为 resume 模块实现（无测试污染）")


# --------------------------------------------------------------------------
# §D 退出清理
# --------------------------------------------------------------------------

def section_drain(ck: ts.Checker) -> None:
    ck.section("§D 退出清理：有界等待与异常不扩散")
    base_task = lambda: {"info_hash": IH, "source": MAGNET,      # noqa: E731
                         "state": STATE_DOWNLOADING, "name": "d",
                         "total_size": 1, "files": [], "selected": [],
                         "priority": 0, "save_path": "", "error": "",
                         "retries": 0, "created_at": time.time(),
                         "finished_at": None, "seed": False}

    with ts.WorkSpace(prefix="mv_persist_drain_") as ws:
        deps, _ = make_deps(ws, ses=None, tasks={IH: base_task()})
        TaskPersistence(deps).drain_resume_alerts(0.1)
        ck.check(True, "drain：会话已停（ses=None）直接返回，不触碰磁盘")

        ses = FakeSession()
        deps, _ = make_deps(ws, ses=ses, tasks={IH: base_task()})
        t0 = time.time()
        TaskPersistence(deps).drain_resume_alerts(0.5)
        ck.check(time.time() - t0 < 0.15 and ses.pops == 1,
                 "drain：无待落盘任务时立即返回（单次 pop，不退避）")

        # 有挂起任务：退避重试 + 幂等重发
        deps, ctx = make_deps(ws, ses=FakeSession(), tasks={IH: base_task()})
        h = FakeHandle()
        ctx["torrents"][IH] = TaskRecord(handle=h, result="r", download=True)
        tp = TaskPersistence(deps)
        t0 = time.time()
        tp.drain_resume_alerts(0.3)
        ck.check(time.time() - t0 > 0.15, "drain：仍有挂起任务时退避重试（0.2s/轮）")
        ck.check(h.save_resume_calls > 0, "drain：重发 save_resume_data（幂等）")

        bad = FakeSession(raise_pop=True)
        deps, ctx = make_deps(ws, ses=bad, tasks={IH: base_task()})
        TaskPersistence(deps).drain_resume_alerts(0.1)
        ck.check(True, "drain：pop_alerts 抛异常被吞掉、不阻断退出流程")

        # 告警分发：替身 lt 命名空间命中 isinstance 两条分支
        real_lt = persist.lt
        origin_write = persist.write_resume
        try:
            persist.lt = _FakeLT
            written: list = []
            persist.write_resume = lambda *a: written.append(a) or "p"
            h2 = FakeHandle(IH2)
            ses2 = FakeSession(alerts=[
                _FakeLT.save_resume_data_alert(h2, {b"k": b"v"}),
                _FakeLT.save_resume_data_failed_alert(h2)])
            deps, ctx = make_deps(ws, ses=ses2, tasks={IH: base_task()})
            ctx["find"].result = TaskRecord(handle=h2, result="r", download=True)
            TaskPersistence(deps).drain_resume_alerts(0.1)
            ck.check(len(written) == 1 and written[0][1] == IH2,
                     "drain：save_resume_data_alert 触发落盘、failed_alert 仅告警")
        finally:
            persist.lt = real_lt
            persist.write_resume = origin_write

        # 注册表同时含临时键与合法键：临时键被跳过，合法任务照常退避重发
        deps, ctx = make_deps(ws, ses=FakeSession(), tasks={IH: base_task()})
        tmp_h = FakeHandle("tmp-140123")
        ctx["torrents"]["tmp-140123"] = TaskRecord(handle=tmp_h, result="r",
                                                   download=True)
        ok_h = FakeHandle(IH)
        ctx["torrents"][IH] = TaskRecord(handle=ok_h, result="r", download=True)
        try:
            TaskPersistence(deps).drain_resume_alerts(0.3)
            ck.check(True, "drain：注册表含非法 info_hash 的临时键时不崩溃")
        except Exception as e:
            ck.check(False, f"drain：临时键导致异常 {type(e).__name__}：{e}")
        ck.check(tmp_h.save_resume_calls == 0,
                 "drain：临时键被拦在 resume 路径之外（不参与落盘重发）")
        ck.check(ok_h.save_resume_calls > 0,
                 "drain：合法任务不受临时键牵连，照常重发")


# --------------------------------------------------------------------------
# §E 启动恢复
# --------------------------------------------------------------------------

def task_dict(source: str = MAGNET, **over) -> dict:
    t = {"info_hash": IH, "source": source, "name": "demo", "total_size": 1024,
         "files": [], "selected": [], "state": STATE_DOWNLOADING, "priority": 0,
         "save_path": "", "error": "", "retries": 0, "seed": False,
         "created_at": time.time(), "finished_at": None}
    t.update(over)
    return t


def section_restore(ck: ts.Checker) -> None:
    ck.section("§E 启动恢复：八组路径")

    # 1) 磁力链 + 无 resume
    with ts.WorkSpace(prefix="mv_persist_r1_") as ws:
        ses = FakeSession()
        t = task_dict()
        deps, ctx = make_deps(ws, ses=ses, tasks={IH: t})
        ck.check(TaskPersistence(deps).restore_task(t) is True,
                 "restore：磁力链（无 resume）恢复成功")
        atp = ses.added[0]
        # 注：无 resume 分支刻意不设 atp.url（libtorrent 只认 info_hash +
        # trackers + DHT 去换元数据），有 resume 分支才回填 source，见 2b。
        ck.check(str(atp.info_hash) == IH
                 and list(atp.trackers) == list(BOOTSTRAP_TRACKERS),
                 "restore：无 resume 时按磁力链参数加入（info_hash + bootstrap tracker）")
        ck.check(os.path.abspath(atp.save_path)
                 == os.path.join(ws.cache, "downloads", IH)
                 and os.path.isdir(atp.save_path),
                 "restore：落盘目录回到 downloads/<ih> 且已建目录")
        ck.check(ctx["put"].calls and ctx["put"].calls[0][0][0] == IH
                 and ctx["put"].calls[0][1].get("make_current") is False,
                 "restore：按 info_hash 入注册表且 make_current=False")
        rec = ctx["put"].calls[0][0][1]
        ck.check(rec.download is True and rec.gen == 7
                 and rec.resolving is True and rec.resolve_started > 0,
                 "restore：无元数据 → 带当前代次、标记待解析并起表计时")
        ck.check(rec.state == STATE_DOWNLOADING,
                 "restore：清单态 DOWNLOADING 原样读回（不倒退成 META_FETCH）")
        ck.check(rec.handle.resumed == 1, "restore：无元数据且非暂停态 → resume 催元数据")
        ck.check(ctx["activate"].calls == [], "restore：元数据未就绪时不激活下载")
        ck.check(t["save_path"] == atp.save_path,
                 "restore：消毒后的 save_path 回写任务清单镜像")

    # 1b) 清单态不属于下载态机（历史脏数据）→ 回落到 META_FETCH
    with ts.WorkSpace(prefix="mv_persist_r1b_") as ws:
        deps, ctx = make_deps(ws, ses=FakeSession(), tasks={IH: task_dict()})
        TaskPersistence(deps).restore_task(task_dict(state="READY"))
        rec = ctx["put"].calls[-1][0][1]
        ck.check(rec.state == STATE_META_FETCH and rec.resolving is True,
                 "restore：清单态非法/非下载态 → 回落 META_FETCH")

    # 2) resume 损坏 → 静默降级全新加入
    with ts.WorkSpace(prefix="mv_persist_r2_") as ws:
        _makedirs(resume_dir(ws.cache))
        with open(resume_path(ws.cache, IH), "wb") as f:
            f.write(b"this-is-not-bencode-at-all")
        ses = FakeSession()
        deps, ctx = make_deps(ws, ses=ses, tasks={IH: task_dict()})
        ck.check(TaskPersistence(deps).restore_task(task_dict()) is True,
                 "restore：fastresume 损坏仍恢复成功（不阻断启动）")
        ck.check(ses.added and str(ses.added[0].info_hash) == IH
                 and list(ses.added[0].trackers) == list(BOOTSTRAP_TRACKERS),
                 "restore：损坏时静默降级为全新加入（走磁力链参数）")

    # 2b) resume 有效 → 官方装载分支（回填 url / save_path，沿用 resume 的 pieces）
    with ts.WorkSpace(prefix="mv_persist_r2b_") as ws:
        _makedirs(os.path.join(ws.cache, ".resume"))
        probe = lt.parse_magnet_uri(MAGNET)
        probe.save_path = ws.cache
        write_resume(ws.cache, IH, bdecode(lt.write_resume_data_buf(probe)))
        ses = FakeSession()
        deps, ctx = make_deps(ws, ses=ses, tasks={IH: task_dict()})
        t = task_dict()
        ck.check(TaskPersistence(deps).restore_task(t) is True,
                 "restore：fastresume 有效 → 恢复成功")
        ck.check(ses.added and ses.added[0].url == MAGNET,
                 "restore：resume 分支走官方装载（回填磁力链 url）")
        ck.check(os.path.abspath(ses.added[0].save_path)
                 == os.path.join(ws.cache, "downloads", IH),
                 "restore：resume 分支的 save_path 被当前落盘目录覆盖")

    # 3) 本地 .torrent 来源
    with ts.WorkSpace(prefix="mv_persist_r3_") as ws:
        ts.build_payload(ws.payload, {"demo.bin": 48 * 1024})
        tpath = write_torrent_file(ws.payload, os.path.join(ws.root, "demo.torrent"))
        ses = FakeSession()
        deps, ctx = make_deps(ws, ses=ses)
        ck.check(TaskPersistence(deps).restore_task(task_dict(source=tpath)) is True
                 and ses.added, "restore：本地 .torrent 来源恢复成功")
        ck.check(getattr(ses.added[0], "ti", None) is not None,
                 "restore：种子分支装载 torrent_info")

    # 4) 来源不可用 → 仅该任务 FAILED
    with ts.WorkSpace(prefix="mv_persist_r4_") as ws:
        t = task_dict(source="")
        deps, ctx = make_deps(ws, ses=FakeSession(), tasks={IH: t})
        ck.check(TaskPersistence(deps).restore_task(t) is False
                 and t["state"] == STATE_FAILED,
                 "restore：来源不可用 → 标记 FAILED 且不抛")
        ck.check(t["error"].startswith("重启恢复失败"),
                 "restore：失败原因写回清单（UI 可见）")
        ck.check(IH in load_tasks(ws.cache),
                 "restore：失败状态已落盘（下次启动不再重试同一来源）")

    # 5) add_torrent 失败 → 同样只标记该任务 FAILED
    with ts.WorkSpace(prefix="mv_persist_r5_") as ws:
        class BoomSession(FakeSession):
            def add_torrent(self, atp):
                _raise(RuntimeError("重复任务已存在"))

        t = task_dict()
        deps, ctx = make_deps(ws, ses=BoomSession(), tasks={IH: t})
        ck.check(TaskPersistence(deps).restore_task(t) is False
                 and t["state"] == STATE_FAILED
                 and "重复任务已存在" in t["error"],
                 "restore：add_torrent 失败 → 标记 FAILED（原因透传）")

    # 6) 暂停 / 停止 / 完成态：不自动 resume，且状态原样读回
    for st, why in ((STATE_PAUSED, "暂停"), (STATE_STOPPED, "停止"),
                    (STATE_COMPLETED, "完成")):
        with ts.WorkSpace(prefix="mv_persist_r6_") as ws:
            ses = FakeSession(has_meta=True)
            deps, ctx = make_deps(ws, ses=ses)
            TaskPersistence(deps).restore_task(task_dict(state=st))
            rec = ctx["put"].calls[-1][0][1]
            ck.check(rec.state == st,
                     f"restore：{why}态原样读回，不被改写成 DOWNLOADING")
            ck.check(rec.handle.resumed == 0, f"restore：{why}态不调用 resume()")
            ck.check(rec.handle.paused == 1 and rec.handle.unset == 1,
                     f"restore：{why}态暂停句柄并摘掉 auto_managed")
            ck.check(ctx["activate"].calls == [],
                     f"restore：{why}态不激活下载")

    # 7) 元数据已就绪 → 立即 DOWNLOADING 并激活（完成态除外）
    with ts.WorkSpace(prefix="mv_persist_r7_") as ws:
        deps, ctx = make_deps(ws, ses=FakeSession(has_meta=True))
        TaskPersistence(deps).restore_task(task_dict())
        rec = ctx["put"].calls[-1][0][1]
        ck.check(rec.state == STATE_DOWNLOADING and rec.result == "PARSE_RESULT",
                 "restore：元数据就绪 → DOWNLOADING 且解析结果回填")
        ck.check(len(ctx["activate"].calls) == 1,
                 "restore：元数据就绪 → 调用宿主 activate_download")
        ck.check(ctx["result_from_ti"].calls
                 and ctx["result_from_ti"].calls[0][0][1] == IH,
                 "restore：解析结果按记录的 info_hash 生成")

    # 8) 入表时持锁（与原实现一致的加锁语义）
    with ts.WorkSpace(prefix="mv_persist_r8_") as ws:
        deps, ctx = make_deps(ws, ses=FakeSession())
        held: list = []

        def put_and_probe(*a, **kw):
            held.append(ctx["lock"].locked())
            return None

        deps.put_record = put_and_probe
        TaskPersistence(deps).restore_task(task_dict())
        ck.check(held == [True], "restore：写入注册表时持锁（与原实现一致）")

    # 9) 落盘目录建不出来 → 标记 FAILED 且不抛（不得靠调用方兜底）
    with ts.WorkSpace(prefix="mv_persist_r9_") as ws:
        blocker = os.path.join(ws.cache, "downloads", IH)
        _makedirs(os.path.dirname(blocker))
        with open(blocker, "wb") as f:      # 同名文件占位 → makedirs 必失败
            f.write(b"i am a file, not a dir")
        t = task_dict(save_path=blocker)
        deps, ctx = make_deps(ws, ses=FakeSession(), tasks={IH: t})
        try:
            ok = TaskPersistence(deps).restore_task(t)
        except Exception as e:
            ck.check(False, f"restore：落盘目录冲突导致异常 {type(e).__name__}：{e}")
            ok = None
        if ok is not None:
            ck.check(ok is False and t["state"] == STATE_FAILED,
                     "restore：落盘目录无法创建 → 标记 FAILED 且不抛")
            ck.check("重启恢复失败" in t.get("error", ""),
                     "restore：目录冲突原因写回清单（UI 可见）")


# --------------------------------------------------------------------------
# §F Facade 委托接线
# --------------------------------------------------------------------------

def section_delegation(ck: ts.Checker) -> None:
    ck.section("§F Facade 委托：SessionManager 薄委托真的打到 persist")
    with ts.WorkSpace(prefix="mv_persist_dele_") as ws:
        _makedirs(ws.cache)
        mgr = SessionManager(ws.cache, listen_port=0)
        seen: list = []
        real = mgr._persist

        class Spy:
            def __getattr__(self, name):
                def wrapper(*a, **kw):
                    seen.append(name)
                    return getattr(real, name)(*a, **kw)
                return wrapper

        mgr._persist = Spy()
        try:
            mgr._persist_tasks()
            mgr._read_resume(IH)
            mgr._request_resume(None)
            mgr._write_resume_from_alert(FakeAlert(FakeHandle(IH)))
            mgr._drain_resume_alerts(0.01)
            mgr._restore_task({})
        finally:
            mgr._persist = real
        ck.check(isinstance(real, TaskPersistence),
                 "SessionManager._persist 是 TaskPersistence 实例（非就地实现）")
        want = ["persist_tasks", "read_resume", "request_resume",
                "write_resume_from_alert", "drain_resume_alerts", "restore_task"]
        missing = [n for n in want if n not in seen]
        ck.check(not missing,
                 f"6 个持久化方法全部路由到 persist（缺 {missing or '无'}）")

        # 目录工具：直接走 persist 模块级纯函数（不在 fetcher 里留第二份实现）
        import core.fetcher as fetcher_mod

        origin = (fetcher_mod.persist_is_within, fetcher_mod.persist_task_dir,
                  fetcher_mod.persist_save_subdir_of,
                  fetcher_mod.persist_safe_task_save_path)
        hits: list = []
        fetcher_mod.persist_is_within = lambda *a: hits.append("is_within") or True
        fetcher_mod.persist_task_dir = lambda *a: hits.append("task_dir") or "/t"
        fetcher_mod.persist_save_subdir_of = (lambda *a: hits.append("save_subdir_of")
                                              or "/s")
        fetcher_mod.persist_safe_task_save_path = (
            lambda *a: hits.append("safe_task_save_path") or "/p")
        try:
            mgr._is_within(ws.cache, ws.cache)
            mgr._task_dir(IH)
            mgr._save_subdir_of(ws.cache)
            mgr._safe_task_save_path(IH, ws.cache)
        finally:
            (fetcher_mod.persist_is_within, fetcher_mod.persist_task_dir,
             fetcher_mod.persist_save_subdir_of,
             fetcher_mod.persist_safe_task_save_path) = origin
        miss2 = [n for n in ("is_within", "task_dir", "save_subdir_of",
                             "safe_task_save_path") if n not in hits]
        ck.check(not miss2,
                 f"4 个目录工具全部委托给 persist 纯函数（缺 {miss2 or '无'}）")
        ck.check(persist.is_within(ws.cache, ws.cache),
                 "收尾校验：persist.is_within 仍是真实实现（无测试污染）")


def main() -> int:
    reload(persist)      # 确保拿到磁盘上的最新实现（防止 import 缓存干扰）
    ck = ts.Checker("persist_test（阶段 1 持久化专项）")
    ck.section("core/persist.py 专项验收（假依赖，不启会话/不联网）")
    section_pure_funcs(ck)
    probe_missing_and_pending(ck)
    section_task_list(ck)
    section_resume(ck)
    section_drain(ck)
    section_restore(ck)
    section_delegation(ck)
    return ck.report()


if __name__ == "__main__":
    sys.exit(main())
