"""core/resolver.py 专项验收（fetcher 重构阶段 5）。

假依赖：FakeSession / FakeHandle / 真 TaskRegistry+TaskOps（内存 cache_dir）
+ CountingPersist——不启真会话、不联网、秒级。

段：
- §A resolve 换代 begin_resolve 四分支（下载保留 / review 就绪 detach /
     review 解析中移除 / 游离句柄兜底）+ gen 递增 + 别名复位
- §B _focus_existing_download：命中切焦点+重发 on_metadata；review/未命中 None
- §C _resolve_torrent_file：解析失败仅当代次发射（gen 自弃）、cache_dir/
     save_subdir 注入（P3-2 防回归）、add 竞态兜底 remove(handle,1)
- §D _resolve_magnet：坏链发射、已是下载任务切焦点、无 trackers 注入
     bootstrap、元数据已就绪走快路径（重复解析不发迟告警）
- §E connect_peer：task_id 定位 / 当前别名等待 / 超时放弃 / 句柄异常吞
- §F on_metadata_received：review→READY+emit / 下载→清单 upsert+activate /
     暂停态保持 PAUSED / 幂等重复告警 / 元数据处理失败→FAILED 分支
- §G on_download_finished：seed 续做种 vs 完成 pause+撤 auto_managed、
     清单 finished_at、落盘+请求 resume
- §H result_from_torrent_info：单文件不套前缀（P0-1 防回归）、多文件剥根、
     恶意路径净化、total_size 排除 .pad
- §J result_from_torrent_info 真种子路径（F1）：单文件**套在目录里**的种子
     path 与 parser 口径对齐（num_files()==1 但 file_path(0) 含分隔符）、
     真单文件 / 多文件行为逐字不变——真 libtorrent 构种，秒级
- §I Facade 接线：SessionManager 公开面打到 ResolverCore；session 的
     on_metadata_received/on_download_finished 注入线经委托到 resolver

退出码：0=通过，1=失败，2=SKIP（依赖缺失，绝不假装通过）。
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    import libtorrent as lt

    import test_support as ts
    from core.models import ParseResult, TorrentFile, file_disk_path
    from core.parser import parse_torrent_file
    from core.persist import TaskPersistence
    from core.registry import TaskRecord, TaskRegistry
    from core.resolver import ResolverCore, result_from_torrent_info
    from core.states import (STATE_COMPLETED, STATE_DOWNLOADING, STATE_FAILED,
                             STATE_META_FETCH, STATE_PAUSED, STATE_READY,
                             STATE_SEEDING)
    from core.taskops import TaskOps
except ImportError as e:    # 依赖缺失（模块/导出不存在）：显式 SKIP；
                            # 语法错误/逻辑错误不是 ImportError，会正常冒泡为失败
    print(f"依赖缺失，无法执行 resolver 专项验收：{e}")
    sys.exit(2)

IH = "a" * 40
MAG = f"magnet:?xt=urn:btih:{IH}"


class FakeHandle:
    def __init__(self, ih=IH, meta=None):
        self.ih = ih
        self.meta = meta          # torrent_file() 返回值
        self.paused = 0
        self.resumed = 0
        self.unset: list = []
        self.set: list = []
        self.connected: list = []

    def info_hash(self):
        return self.ih

    def pause(self):
        self.paused += 1

    def resume(self):
        self.resumed += 1

    def set_flags(self, f):
        self.set.append(f)

    def unset_flags(self, f):
        self.unset.append(f)

    def torrent_file(self):
        return self.meta

    def connect_peer(self, addr):
        self.connected.append(addr)

    def prioritize_files(self, prio):
        self.prioritized = prio

    def torrent_priority(self, p):
        self.tprio = p


class FakeTI:
    """torrent_info 替身（供 _resolve_torrent_file 的 atp.ti 赋值检查）。"""


class FakeSession:
    def __init__(self, add_result=None, add_raises=False):
        self.added: list = []
        self.removed: list = []
        self.add_result = add_result or FakeHandle()
        self.add_raises = add_raises

    def add_torrent(self, atp):
        if self.add_raises:
            raise RuntimeError("会话已满")
        self.added.append(atp)
        return self.add_result

    def remove_torrent(self, h, options=0):
        self.removed.append((h, options))


class FakeSched:
    def __init__(self):
        self.stops = 0

    def stop(self):
        self.stops += 1


class CountingPersist:
    def __init__(self):
        self.persist_calls = 0
        self.resume_requests: list = []

    def persist_tasks(self):
        self.persist_calls += 1

    def request_resume(self, rec):
        self.resume_requests.append(rec)


def mk_env(ses=None):
    ws = tempfile.mkdtemp(prefix="mv_resolver_")
    cache = os.path.join(ws, "cache")
    dl = os.path.join(cache, "downloads")
    os.makedirs(dl, exist_ok=True)
    ses = ses if ses is not None else FakeSession()
    reg = TaskRegistry(cache_dir=cache, ses_get=lambda: ses)
    sched = FakeSched()
    persist = CountingPersist()
    ops = TaskOps(reg=reg, persist=persist, ses_get=lambda: ses,
                  scheduler_get=lambda: sched, download_dir=dl)
    emet, eerr = [], []
    rs = ResolverCore(reg=reg, ops=ops, cache_dir=cache,
                      ses_get=lambda: ses, scheduler_get=lambda: sched,
                      persist_tasks=persist.persist_tasks,
                      request_resume=persist.request_resume,
                      emit_metadata=lambda r: emet.append(r),
                      emit_error=lambda m: eerr.append(m))
    return ws, reg, ses, sched, persist, ops, rs, emet, eerr


# --------------------------------------------------------------------------

def section_begin_resolve(ck):
    ck.section("§A begin_resolve 换代四分支")
    ws, reg, ses, sched, persist, ops, rs, emet, eerr = mk_env()
    try:
        h_dl = FakeHandle(IH)
        with reg.lock:
            reg.put_record_locked(IH, TaskRecord(handle=h_dl, download=True,
                                                 result="R"),
                                  make_current=True)
        g0 = reg.gen
        rs.begin_resolve()
        ck.check(sched.stops == 1, "第一步停预览调度")
        ck.check(IH in reg.torrents, "下载任务保留注册表（继续跑）")
        ck.check(h_dl.paused == 0, "下载任务不被 pause")
        ck.check(reg.resolving and reg.resolve_started > 0
                 and reg.current_ih is None and reg.gen == g0 + 1,
                 "换代：resolving=True / 别名清空 / gen+1")

        # review 就绪 → detach（pause+upload_mode，留在表）
        ws2, reg2, ses2, sched2, _, _, rs2, _, _ = mk_env()
        h_rv = FakeHandle(IH)
        with reg2.lock:
            reg2.put_record_locked(IH, TaskRecord(handle=h_rv, result="R"),
                                   make_current=True)
        rs2.begin_resolve()
        ck.check(h_rv.paused == 1
                 and lt.torrent_flags.upload_mode
                 in h_rv.set,
                 "就绪 review：detach（pause+upload_mode）保留在 map")
        ck.check(ses2.removed == [], "detach 不移除句柄")

        # review 解析中 → 移除句柄 + 弹出记录（未成任务不算删除）
        ws3, reg3, ses3, _, _, _, rs3, _, _ = mk_env()
        h_mf = FakeHandle("b" * 40)
        with reg3.lock:
            reg3.put_record_locked("b" * 40,
                                   TaskRecord(handle=h_mf, result=None),
                                   make_current=True)
        rs3.begin_resolve()
        # 原实现即双 remove（rec.handle + 游离句柄兜底同物再摘一次，
        # libtorrent 对已摘句柄幂等）——忠实照搬，不改行为
        ck.check(ses3.removed == [(h_mf, 1), (h_mf, 1)],
                 "解析中 review：remove(handle,1)（含兜底二摘，与原语义一致）")
        ck.check("b" * 40 not in reg3.torrents, "记录弹出注册表")

        # 游离句柄兜底：current_ih=None 但 reg.handle 残留
        ws4, reg4, ses4, _, _, _, rs4, _, _ = mk_env()
        h_orphan = FakeHandle("c" * 40)
        with reg4.lock:
            reg4.handle = h_orphan
        rs4.begin_resolve()
        ck.check(ses4.removed == [(h_orphan, 1)], "游离句柄兜底移除")
        for w in (ws2, ws3, ws4):
            shutil.rmtree(w, ignore_errors=True)
    finally:
        shutil.rmtree(ws, ignore_errors=True)


def section_focus(ck):
    ck.section("§B _focus_existing_download")
    ws, reg, ses, sched, persist, ops, rs, emet, eerr = mk_env()
    try:
        h = FakeHandle(IH)
        rec = TaskRecord(handle=h, result="R", download=True)
        with reg.lock:
            reg.put_record_locked(IH, rec)
        g0 = reg.gen
        got = rs._focus_existing_download(IH)
        ck.check(got is rec and reg.current_ih == IH, "命中：焦点切换")
        ck.check(reg.gen == g0 + 1, "焦点切换即换代")
        ck.check(emet == ["R"], "有结果 → 重发 on_metadata（旧 UI 语义）")
        ck.check(rs._focus_existing_download("f" * 40) is None, "未命中 None")
        rv = TaskRecord(handle=FakeHandle("e" * 40), result=None)
        with reg.lock:
            reg.put_record_locked("e" * 40, rv)
        ck.check(rs._focus_existing_download("e" * 40) is None,
                 "review（非 download）记录不切焦点")
    finally:
        shutil.rmtree(ws, ignore_errors=True)


def make_result():
    return ParseResult(info_hash=IH, name="T", total_size=10, piece_size=100,
                       num_pieces=1,
                       files=[TorrentFile(0, "T.bin", 10, 0, 0, 0)],
                       source="torrent")


def section_torrent_file(ck):
    ck.section("§C _resolve_torrent_file")
    # 旧代次失败：不发射（gen 自弃）
    ws, reg, ses, sched, persist, ops, rs, emet, eerr = mk_env()
    try:
        rs._resolve_torrent_file("Z:\\不存在.torrent", reg.gen - 1)
        ck.check(eerr == [], "旧代次解析失败：不打扰新会话（0 发射）")
        rs._resolve_torrent_file("Z:\\不存在.torrent", reg.gen)
        ck.check(len(eerr) == 1 and "解析失败" in eerr[0],
                 "当代次解析失败：发射含原因")
    finally:
        shutil.rmtree(ws, ignore_errors=True)

    # 正常路径：cache_dir/save_subdir 注入 + add+pause + register + emit
    import core.resolver as resmod
    ws, reg, ses, sched, persist, ops, rs, emet, eerr = mk_env()
    real_parse = resmod.parse_torrent_file

    class _ATP:
        def __init__(self):
            self.ti = None
            self.save_path = ""
            self.flags = 0

    real_atp = resmod.lt.add_torrent_params
    real_ti = resmod.lt.torrent_info
    try:
        resmod.parse_torrent_file = lambda p: make_result()
        # 真 add_torrent_params.ti 的 setter 拒收替身对象——连 atp 一起替身
        resmod.lt.add_torrent_params = lambda: _ATP()
        resmod.lt.torrent_info = lambda p: FakeTI()   # 假路径不读盘
        handle = FakeHandle(IH)
        ses.add_result = handle
        g = reg.gen
        rs._resolve_torrent_file("C:\\demo.torrent", g)
        ck.check(emet and emet[0].cache_dir == reg.cache_dir,
                 "P3-2 防回归：result.cache_dir 注入（主窗口映射键的前提）")
        ck.check(emet[0].save_subdir.startswith(".preview"),
                 "save_subdir 指向 .preview/<ih>")
        atp = ses.added[0]
        ck.check(atp.flags & lt.torrent_flags.upload_mode
                 and atp.save_path.endswith(IH),
                 "atp：upload_mode（只查单不下载）+ .preview 落盘")
        ck.check(handle.paused == 1 and reg.current_ih == IH,
                 "add 后 pause + 登记为当前任务")
        # 期间换代 → 结果自弃
        emet.clear()
        rs._resolve_torrent_file("C:\\demo.torrent", reg.gen - 100)
        ck.check(emet == [], "gen 不匹配：结果自弃不发射")
    finally:
        resmod.parse_torrent_file = real_parse
        resmod.lt.add_torrent_params = real_atp
        resmod.lt.torrent_info = real_ti
        shutil.rmtree(ws, ignore_errors=True)


def section_magnet(ck):
    ck.section("§D _resolve_magnet")
    ws, reg, ses, sched, persist, ops, rs, emet, eerr = mk_env()
    try:
        rs._resolve_magnet("magnet:?xt=无")
        ck.check(len(eerr) == 1 and "磁力链接无效" in eerr[0],
                 "坏磁力链：发射含原因")
        emet.clear(); eerr.clear()
        h = FakeHandle(IH)
        ses.add_result = h
        rs._resolve_magnet(MAG)
        atp = ses.added[0]
        ck.check(atp.flags & lt.torrent_flags.upload_mode,
                 "只取元数据：upload_mode")
        ck.check(len(atp.trackers) == 5, "无 tracker 注入 bootstrap（5 条）")
        ck.check(h in [r.handle for r in reg.torrents.values()]
                 and reg.resolving, "登记当前任务：META_FETCH 等待态")
        ck.check(eerr == [] and emet == [], "元数据未就绪：不发射任何回调")
        # 快路径：重复解析，libtorrent 返回既有句柄且元数据已就绪
        ti = object()
        h.meta = ti
        import core.resolver as resmod
        real = resmod.result_from_torrent_info
        resmod.result_from_torrent_info = (
            lambda x, y: make_result())
        try:
            rs._resolve_magnet(MAG)
            ck.check(emet and emet[0].name == "T",
                     "快路径：既有元数据直接走就绪链（不发迟告警）")
        finally:
            resmod.result_from_torrent_info = real
    finally:
        shutil.rmtree(ws, ignore_errors=True)


def section_connect(ck):
    ck.section("§E connect_peer")
    ws, reg, ses, sched, persist, ops, rs, emet, eerr = mk_env()
    try:
        h = FakeHandle(IH)
        with reg.lock:
            reg.put_record_locked(IH, TaskRecord(handle=h, download=True))
        rs.connect_peer("127.0.0.1", 6885, wait_handle=0.2, task_id=IH)
        ck.check(h.connected == [("127.0.0.1", 6885)], "task_id 定位直连")
        t0 = time.time()
        rs.connect_peer("127.0.0.1", 1, wait_handle=0.2,
                        task_id="f" * 40)
        ck.check(0.15 <= time.time() - t0 < 1.5,
                 "未知 task_id：等满超时放弃（不抛）")

        class BoomH(FakeHandle):
            def connect_peer(self, addr):
                raise RuntimeError("网络炸了")
        with reg.lock:
            reg.put_record_locked("c" * 40, TaskRecord(handle=BoomH("c" * 40)))
        try:
            rs.connect_peer("127.0.0.1", 2, task_id="c" * 40)
            ck.check(True, "句柄 connect_peer 抛：吞掉记日志")
        except Exception as e:
            ck.check(False, f"不得冒泡：{e}")
    finally:
        shutil.rmtree(ws, ignore_errors=True)


def make_ti(num=1, pad=False):
    """torrent_info 替身：驱动真 result_from_torrent_info。"""
    # 真 libtorrent 语义：单文件种子 file_path(0)==根名；.pad 在
    # root/.pad/ 目录内（is_pad 判定要求两侧都有斜杠）
    total = num + (1 if pad else 0)

    class FS:
        def num_files(self):
            return total

        def file_size(self, i):
            return 50 if i < num else 4096

        def file_path(self, i):
            if pad and i == num:
                return "root/.pad/padfile"
            return "root" if total == 1 else f"root/f{i}.bin"

    class TI:
        def files(self):
            return FS()

        def num_files(self):
            return total

        def piece_length(self):
            return 100

        def name(self):
            return "root"

        def num_pieces(self):
            return 3

        def trackers(self):
            return []

        def comment(self):
            return "c"

        def creator(self):
            return "cr"
    return TI()


def section_metadata(ck):
    ck.section("§F on_metadata_received")
    # review 当前任务：READY + 发射
    ws, reg, ses, sched, persist, ops, rs, emet, eerr = mk_env()
    try:
        h = FakeHandle(IH)
        h.meta = make_ti(num=2)
        rec = TaskRecord(handle=h, resolving=True, download=False)
        with reg.lock:
            reg.put_record_locked(IH, rec, make_current=True)
            reg.resolving = True
        rs.on_metadata_received(rec)
        ck.check(rec.state == STATE_READY and rec.result is not None,
                 "review 就绪：STATE_READY + result 挂上")
        ck.check(emet and emet[0].cache_dir == reg.cache_dir,
                 "当前 review：发射 on_metadata（cache_dir 已注入）")
        ck.check(reg.resolving is False, "当前别名 resolving 归零")
        # 幂等：重复告警直接跳过
        n = len(emet)
        rs.on_metadata_received(rec)
        ck.check(len(emet) == n, "重复告警幂等跳过")
    finally:
        shutil.rmtree(ws, ignore_errors=True)

    # 下载任务：清单 upsert + activate + 请求 resume
    ws, reg, ses, sched, persist, ops, rs, emet, eerr = mk_env()
    try:
        h = FakeHandle(IH)
        h.meta = make_ti(num=1)
        rec = TaskRecord(handle=h, resolving=True, download=True,
                         save_path=reg.cache_dir)
        with reg.lock:
            reg.put_record_locked(IH, rec)
            reg.tasks[IH] = {"info_hash": IH, "state": STATE_META_FETCH}
        rs.on_metadata_received(rec)
        ck.check(rec.state == STATE_DOWNLOADING, "下载任务 → DOWNLOADING")
        t = reg.tasks[IH]
        ck.check(t["state"] == STATE_DOWNLOADING and t["selected"]
                 and t["total_size"] > 0,
                 "清单补全 name/total_size/selected + 状态")
        ck.check(h.unset and lt.torrent_flags.upload_mode in h.unset
                 and h.resumed == 1, "activate：解 upload_mode + resume")
        ck.check(persist.resume_requests == [rec], "就绪后请求 fastresume")
        ck.check(persist.persist_calls >= 1, "落盘清单")
        ck.check(emet == [], "非当前任务不发全局 on_metadata")
    finally:
        shutil.rmtree(ws, ignore_errors=True)

    # 暂停态保持暂停（用户暂停元数据获取是合法动作）
    ws, reg, ses, sched, persist, ops, rs, emet, eerr = mk_env()
    try:
        h = FakeHandle(IH)
        h.meta = make_ti(num=1)
        rec = TaskRecord(handle=h, download=True, save_path=reg.cache_dir)
        with reg.lock:
            reg.put_record_locked(IH, rec)
            reg.tasks[IH] = {"info_hash": IH, "state": STATE_PAUSED}
        rs.on_metadata_received(rec)
        ck.check(rec.state == STATE_PAUSED and reg.tasks[IH]["state"]
                 == STATE_PAUSED, "暂停态元数据到达：保持 PAUSED 不自动开下")
    finally:
        shutil.rmtree(ws, ignore_errors=True)

    # torrent_file 抛 → FAILED（下载任务写清单+error；预览发射 on_error）
    ws, reg, ses, sched, persist, ops, rs, emet, eerr = mk_env()
    try:
        class BadH(FakeHandle):
            def torrent_file(self):
                raise RuntimeError("元数据缺失")
        rec = TaskRecord(handle=BadH(IH), download=True)
        with reg.lock:
            reg.put_record_locked(IH, rec)
            reg.tasks[IH] = {"info_hash": IH, "state": STATE_META_FETCH}
        rs.on_metadata_received(rec)
        ck.check(rec.state == STATE_FAILED
                 and reg.tasks[IH]["state"] == STATE_FAILED
                 and "元数据处理失败" in reg.tasks[IH]["error"],
                 "处理失败：FAILED + 清单写原因（下载任务不弹预览错）")
    finally:
        shutil.rmtree(ws, ignore_errors=True)


def section_finished(ck):
    ck.section("§G on_download_finished（D3）")
    ws, reg, ses, sched, persist, ops, rs, emet, eerr = mk_env()
    try:
        h = FakeHandle(IH)
        rec = TaskRecord(handle=h, download=True)
        with reg.lock:
            reg.put_record_locked(IH, rec)
            reg.tasks[IH] = {"info_hash": IH}
        rs.on_download_finished(rec)
        ck.check(rec.state == STATE_COMPLETED
                 and reg.tasks[IH]["state"] == STATE_COMPLETED
                 and reg.tasks[IH].get("finished_at"),
                 "默认完成即停：COMPLETED + finished_at 落清单")
        ck.check(h.paused == 1 and lt.torrent_flags.auto_managed in h.unset,
                 "完成：pause + 撤 auto_managed（防队列自动续传）")
        ck.check(persist.resume_requests == [rec]
                 and persist.persist_calls >= 1, "完成落盘 + 请求 resume")
        h2 = FakeHandle(IH2 := "b" * 40)
        rec2 = TaskRecord(handle=h2, download=True, seed=True)
        with reg.lock:
            reg.put_record_locked(IH2, rec2)
            reg.tasks[IH2] = {"info_hash": IH2}
        rs.on_download_finished(rec2)
        ck.check(rec2.state == STATE_SEEDING and h2.resumed == 1,
                 "seed=True → SEEDING + resume 做种")
    finally:
        shutil.rmtree(ws, ignore_errors=True)


def section_pure_ti(ck):
    ck.section("§H result_from_torrent_info（纯函数）")
    # 单文件：path==种子名本身，不套 "name/name" 双层（P0-1 防回归）
    r = result_from_torrent_info(make_ti(num=1), IH)
    ck.check(r.files[0].path == "root",
             f"单文件 path 不套根目录层（实得 {r.files[0].path!r}）")
    ck.check(r.name == "root" and r.info_hash == IH, "name/info_hash 透传")
    # 多文件：剥根
    r2 = result_from_torrent_info(make_ti(num=2), IH)
    ck.check([f.path for f in r2.files] == ["root/f0.bin", "root/f1.bin"],
             "多文件剥根但保留 root 前缀（落盘层级一致）")
    # .pad 不计入 total_size（BEP-47）
    r3 = result_from_torrent_info(make_ti(num=1, pad=True), IH)
    ck.check(r3.total_size == 50,
             f"total_size 排除 .pad（实得 {r3.total_size}，应=50）")


def section_real_paths(ck):
    ck.section("§J result_from_torrent_info 真种子路径（F1 单文件套目录对齐 parser）")
    import warnings as _w
    ws = tempfile.mkdtemp(prefix="mv_resolver_realti_")
    try:
        def _build(fs_paths, parent):
            """真 libtorrent 构种：返回 (torrent_info, .torrent 字节)。"""
            with _w.catch_warnings():
                _w.simplefilter("ignore", DeprecationWarning)
                fs = lt.file_storage()
                for p in fs_paths:
                    lt.add_files(fs, p)
                ct = lt.create_torrent(fs, 16 * 1024)
                lt.set_piece_hashes(ct, parent)
                raw = ct.generate()
            return lt.torrent_info(raw), lt.bencode(raw)

        # ① 单文件但套在目录里：info 含 files 键、num_files()==1，
        #    磁盘布局 payload/movie/demo.mp4 —— F1 修复现场
        sub = os.path.join(ws, "sub")
        demo = os.path.join(sub, "payload", "movie", "demo.mp4")
        os.makedirs(os.path.dirname(demo), exist_ok=True)
        with open(demo, "wb") as fh:
            fh.write(os.urandom(400 * 1024))
        tp = os.path.join(ws, "nested.torrent")
        ti, raw = _build([os.path.join(sub, "payload")], sub)
        with open(tp, "wb") as fh:
            fh.write(raw)
        ck.check(ti.files().num_files() == 1,
                 "构造：单文件套目录 num_files()==1（判别退化条件已就位）")
        r = result_from_torrent_info(ti, str(ti.info_hash()))
        pr = parse_torrent_file(tp)
        ck.check(r.files[0].path == "payload/movie/demo.mp4",
                 f"resolver path 未被截成目录（实得 {r.files[0].path!r}）")
        ck.check(r.files[0].path == pr.files[0].path,
                 f"两入口同资源 path 判别一致（parser={pr.files[0].path!r}）")
        ck.check(os.path.isfile(file_disk_path(sub, r.files[0])),
                 "file_disk_path 拼接指向实际落盘文件（isfile=True）")

        # ② 真单文件种子（无子目录）：行为逐字不变
        sf = os.path.join(ws, "single.bin")
        with open(sf, "wb") as fh:
            fh.write(os.urandom(300 * 1024))
        ti2, _ = _build([sf], ws)
        ck.check(ti2.files().num_files() == 1
                 and "\\" not in ti2.files().file_path(0)
                 and "/" not in ti2.files().file_path(0),
                 "构造：真单文件 file_path(0) 不含分隔符")
        r2 = result_from_torrent_info(ti2, str(ti2.info_hash()))
        ck.check(r2.files[0].path == "single.bin",
                 f"真单文件 path 不套前缀（实得 {r2.files[0].path!r}）")
        ck.check(os.path.isfile(file_disk_path(ws, r2.files[0])),
                 "真单文件磁盘路径不变（isfile=True）")

        # ③ 多文件（>1）行为不变：剥根但保留根前缀 + .pad 仍排除
        pay = ts.build_payload(os.path.join(ws, "multi"))
        ti3 = ts.make_torrent(pay)
        r3 = result_from_torrent_info(ti3, str(ti3.info_hash()))
        paths3 = [f.path for f in r3.files]
        ck.check(all(p.startswith("multi/") for p in paths3),
                 f"多文件保留根前缀（实得 {paths3[:3]}…）")
        ck.check(all(os.path.isfile(file_disk_path(os.path.dirname(pay), f))
                     for f in r3.view_files),
                 "多文件非 .pad 磁盘路径全部命中（isfile=True）")
        ck.check(r3.total_size == sum(f.size for f in r3.files if not f.is_pad),
                 "多文件 total_size 排除 .pad（行为不变）")
    finally:
        shutil.rmtree(ws, ignore_errors=True)


def section_facade(ck):
    ck.section("§I Facade 接线")
    ws = tempfile.mkdtemp(prefix="mv_resolver_facade_")
    try:
        from core.fetcher import SessionManager
        mgr = SessionManager(os.path.join(ws, "cache"))
        ck.check(isinstance(mgr._resolver, ResolverCore),
                 "SessionManager 构造出 ResolverCore")
        ck.check(mgr._resolver.reg is mgr._registry
                 and mgr._resolver.ops is mgr._taskops,
                 "Resolver 复用同一 registry/taskops 实例（单源）")
        # 委托路由证据：spy 换绑 resolver，mgr._on_* 调用必须打过去
        real_r = mgr._resolver
        seen = []

        class Spy:
            def __getattr__(self, name):
                def w(*a, **k):
                    seen.append(name)
                    return getattr(real_r, name)(*a, **k)
                return w
        mgr._resolver = Spy()
        try:
            mgr._on_metadata_received(TaskRecord(handle=None))
            mgr._on_download_finished(TaskRecord(handle=None))
            mgr.resolve.__self__   # 属性可达即可
        finally:
            mgr._resolver = real_r
        ck.check(seen == ["on_metadata_received", "on_download_finished"],
                 f"mgr 的 alert 双回调经委托打到 ResolverCore（实收 {seen}）")
        import core.fetcher as fm
        import core as pkg
        ck.check(hasattr(fm.SessionManager, "_result_from_torrent_info")
                 and pkg.resolver.result_from_torrent_info
                 is fm.resolver_result_from_ti,
                 "_result_from_torrent_info 委托到 resolver 模块级纯函数（单源）")
        ck.check(fm.STATE_NAMES is pkg.preview.STATE_NAMES,
                 "STATE_NAMES 再导出同一对象（本体在 preview）")
    finally:
        shutil.rmtree(ws, ignore_errors=True)


def main() -> int:
    ck = ts.Checker("resolver_test（阶段 5 解析与元数据编排专项）")
    ck.section("core/resolver.py 专项验收（假会话+真注册表，不联网）")
    section_begin_resolve(ck)
    section_focus(ck)
    section_torrent_file(ck)
    section_magnet(ck)
    section_connect(ck)
    section_metadata(ck)
    section_finished(ck)
    section_pure_ti(ck)
    section_real_paths(ck)
    section_facade(ck)
    return ck.report()


if __name__ == "__main__":
    sys.exit(main())
