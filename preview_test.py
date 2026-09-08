"""core/preview.py 专项验收（fetcher 重构阶段 5）。

假依赖：FakeHandle（可编程 have/status/deadline）+ 真 TaskRegistry——
不启会话、不联网、秒级。流服务本身的行为由 smoke_test [2b]/[3] 与
moov_stream_test 端到端覆盖，此处专攻「磁盘路径 → 任务 → 分块」这条桥。

段：
- §A find_record_for_path：前缀命中（正/反斜杠归一）、无句柄/无结果记录
     跳过、根外路径 None（调用方不得降级全量）
- §B piece_map_for_path：命中构造 PieceMap（offset/区间透传）、
     未命中 None、torrent_file 抛 → None（绝不整文件可用）
- §C demand_for_path：字节区间 → 分块 deadline 集合（首/尾钳制到文件
     区间）、pl<=0 → False、未命中 False、句柄炸 False 不抛
- §D have_piece / piece_length：无当前句柄 False/None、has_metadata=False
     → None、句柄抛 → 降级值
- §E start/stop_preview：无句柄或无结果 → RuntimeError("请先解析种子")；
     有 → scheduler.begin 收到 (handle, file)；stop 转发
- §F status：句柄 None → None；status() 抛 → None；正常字段集与
     同源一次扫描（contiguous==st["contiguous"]、buffer 归一）、
     resolving/elapsed 走一致快照、file_progress 抛 → []
- §G task_result / current_result：锁读透传
- §H Facade：SessionManager 公开面（piece_map_for_path 等 7 个 + 2 属性）
     路由到 PreviewCore

退出码：0=通过，1=失败，2=SKIP（依赖缺失，绝不假装通过）。
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    import libtorrent as lt

    import test_support as ts
    from core.models import ParseResult, TorrentFile
    from core.preview import PreviewCore
    from core.registry import TaskRecord, TaskRegistry
except Exception as e:      # 依赖缺失：显式 SKIP，绝不假装通过
    print(f"依赖缺失，无法执行 preview 专项验收：{e}")
    sys.exit(2)

IH = "a" * 40


class FakeHandle:
    def __init__(self, ih=IH, have=(), pl=100, file=None, status_ok=True):
        self.ih = ih
        self._have = set(have)
        self._pl = pl
        self.deadlines: list = []
        self.status_ok = status_ok
        self.file = file          # torrent_file() 替身
        self.begun = []

    def info_hash(self):
        return self.ih

    def have_piece(self, p):
        return p in self._have

    def set_piece_deadline(self, p, ms):
        self.deadlines.append((p, ms))

    def torrent_file(self):
        if self.file is None:
            raise RuntimeError("无元数据")
        return self.file

    def has_metadata(self):
        return self.file is not None

    def status(self):
        if not self.status_ok:
            raise RuntimeError("句柄失效")

        class S:
            # 用真枚举值作 state（STATE_NAMES 键即这些属性值，裸 int 不保证相等）
            state = lt.torrent_status.downloading
            num_peers = 3
            num_seeds = 1
            download_payload_rate = 50
            total_done = 250
        return S()

    def file_progress(self):
        return [1, 2]


class FakeTI:
    def __init__(self, pl=100):
        self._pl = pl

    def piece_length(self):
        return self._pl


class FakeSched:
    def __init__(self, active=False, file=None):
        self.active = active
        self.file = file
        self.stops = 0
        self.begins: list = []

    def begin(self, h, f):
        self.begins.append((h, f))

    def stop(self):
        self.stops += 1

    def contiguous_progress(self):
        return 150

    def tail_ready(self):
        return True


def mk_env():
    ws = tempfile.mkdtemp(prefix="mv_preview_")
    cache = os.path.join(ws, "cache")
    os.makedirs(cache, exist_ok=True)
    reg = TaskRegistry(cache_dir=cache, ses_get=lambda: None)
    sched = FakeSched()
    pv = PreviewCore(reg=reg, scheduler_get=lambda: sched)
    return ws, cache, reg, sched, pv


def add_task_rec(reg, save_path, nfiles=2, fsize=200, ih=IH):
    """入表一条：result.files 两条 [0,200)/[200,400)，piece 0~3。"""
    files = [TorrentFile(i, f"root/f{i}.bin", fsize, i * fsize,
                         (i * fsize) // 100, ((i + 1) * fsize - 1) // 100)
             for i in range(nfiles)]
    result = ParseResult(info_hash=ih, name="root", total_size=nfiles * fsize,
                         piece_size=100, num_pieces=4, files=files,
                         source="magnet")
    h = FakeHandle(ih, file=FakeTI())
    rec = TaskRecord(handle=h, result=result, save_path=save_path,
                     download=True)
    with reg.lock:
        reg.put_record_locked(ih, rec)
    return rec, result, h


# --------------------------------------------------------------------------

def section_find(ck):
    ck.section("§A find_record_for_path")
    ws, cache, reg, sched, pv = mk_env()
    try:
        task_dir = os.path.join(cache, "downloads", IH)
        os.makedirs(task_dir, exist_ok=True)
        rec, result, h = add_task_rec(reg, task_dir)
        hit = pv.find_record_for_path(os.path.join(task_dir, "root", "f1.bin"))
        ck.check(hit is not None and hit[1].path == "root/f1.bin",
                 "正斜杠声明 × 反斜杠磁盘 → normpath 归一命中")
        hit2 = pv.find_record_for_path(task_dir + os.sep + "root/f0.bin")
        ck.check(hit2 is not None and hit2[1].path == "root/f0.bin",
                 "混合分隔符路径同样命中")
        ck.check(pv.find_record_for_path(os.path.join(cache, "elsewhere"))
                 is None, "任务目录外 → None（不得降级为整文件可用）")
        # 无句柄记录跳过
        bad = TaskRecord(handle=None, result=result, save_path=task_dir)
        with reg.lock:
            reg.put_record_locked("b" * 40, bad)
        ck.check(pv.find_record_for_path(
            os.path.join(task_dir, "root", "f0.bin"))[0] is rec,
            "无句柄记录不参与（命中仍是有效记录）")
    finally:
        shutil.rmtree(ws, ignore_errors=True)


def section_piece_map(ck):
    ck.section("§B piece_map_for_path")
    ws, cache, reg, sched, pv = mk_env()
    try:
        task_dir = os.path.join(cache, "downloads", IH)
        rec, result, h = add_task_rec(reg, task_dir)
        h._have = {2}   # 模拟 piece2 已完整落盘
        pm = pv.piece_map_for_path(os.path.join(task_dir, "root", "f1.bin"))
        ck.check(pm is not None and pm.offset == 200
                 and pm.start_piece == 2 and pm.end_piece == 3
                 and pm.piece_length == 100 and pm.size == 200,
                 "PieceMap 透传 f1 的 offset/分块区间/大小")
        ck.check(bool(pm.have(2)) is True and bool(pm.have(1)) is False,
                 "have 回调 = 句柄 have_piece（已下 piece2 可读）")
        ck.check(pv.piece_map_for_path(os.path.join(cache, "x")) is None,
                 "未命中 → None（pieces_cb 语义：不可判定≠可用）")
        h.file = None    # torrent_file() 抛
        ck.check(pv.piece_map_for_path(
            os.path.join(task_dir, "root", "f1.bin")) is None,
            "元数据抛异常 → None，绝不整文件可用（P1-8 语义）")
    finally:
        shutil.rmtree(ws, ignore_errors=True)


def section_demand(ck):
    ck.section("§C demand_for_path")
    ws, cache, reg, sched, pv = mk_env()
    try:
        task_dir = os.path.join(cache, "downloads", IH)
        rec, result, h = add_task_rec(reg, task_dir)
        # start_byte 语义 = 文件相对字节（与 scheduler.request_range 一致）；
        # f1 占分块 2~3，相对 [50,150) → piece {2,3}
        ck.check(pv.demand_for_path(
            os.path.join(task_dir, "root", "f1.bin"), 50, 150) is True,
            "命中返回 True")
        ck.check(h.deadlines == [(2, 0), (3, 0)],
                 "相对字节→分块 {2,3}，deadline 即时（0ms）")
        h.deadlines.clear()
        pv.demand_for_path(os.path.join(task_dir, "root", "f1.bin"),
                           0, 9999)   # 越界钳制到文件区间
        ck.check(h.deadlines == [(2, 0), (3, 0)],
                 "区间钳制：不越界补拉其它文件的分块")
        ck.check(pv.demand_for_path(os.path.join(cache, "no"), 0, 1) is False,
                 "未命中 → False（不抛）")
        h._pl_bad = True
        h.file = type("T", (), {"piece_length": staticmethod(lambda: 0)})()
        ck.check(pv.demand_for_path(
            os.path.join(task_dir, "root", "f1.bin"), 0, 100) is False,
            "piece_length<=0 → False（除零防线）")
        h.file = None
        ck.check(pv.demand_for_path(
            os.path.join(task_dir, "root", "f1.bin"), 0, 100) is False,
            "句柄抛 → False 不冒泡")
    finally:
        shutil.rmtree(ws, ignore_errors=True)


def section_current(ck):
    ck.section("§D have_piece / piece_length / start/stop_preview")
    ws, cache, reg, sched, pv = mk_env()
    try:
        ck.check(pv.have_piece(0) is False, "无当前句柄 → False")
        ck.check(pv.piece_length() is None, "无当前句柄 → None")
        rec, result, h = add_task_rec(reg, cache)
        with reg.lock:
            reg.put_record_locked(IH, rec, make_current=True)
        h._have = {1}
        ck.check(pv.have_piece(1) is True and pv.have_piece(99) is False,
                 "当前句柄 have_piece 透传")
        ck.check(pv.piece_length() == 100, "piece_length 经 has_metadata 门禁")
        h.file = None
        ck.check(pv.piece_length() is None, "元数据未就绪 → None")
        # start_preview：先解析门禁
        pv2 = PreviewCore(reg=TaskRegistry(cache_dir=cache,
                                           ses_get=lambda: None),
                          scheduler_get=lambda: sched)
        try:
            pv2.start_preview(result.files[0])
            ck.check(False, "无当前任务应 RuntimeError")
        except RuntimeError as e:
            ck.check("请先解析" in str(e), f"门禁文案（{e}）")
        with reg.lock:
            reg.result = result
        pv.start_preview(result.files[0])
        ck.check(sched.begins == [(h, result.files[0])],
                 "begin 收到 (当前句柄, 文件)")
        pv.stop_preview()
        ck.check(sched.stops == 1, "stop 转发 scheduler")
    finally:
        shutil.rmtree(ws, ignore_errors=True)


def section_status(ck):
    ck.section("§F status 快照")
    ws, cache, reg, sched, pv = mk_env()
    try:
        ck.check(pv.status() is None, "无句柄 → None（UI 静默跳过）")
        rec, result, h = add_task_rec(reg, cache)
        with reg.lock:
            reg.put_record_locked(IH, rec, make_current=True)
        bad = FakeHandle("c" * 40)
        bad.status_ok = False
        with reg.lock:
            reg.handle = bad
        ck.check(pv.status() is None, "handle.status() 抛 → None 不崩 UI")
        with reg.lock:
            reg.handle = h
        sched.active = True
        sched.file = result.files[0]
        reg.set_resolving(True, time.time() - 2)
        st = pv.status()
        ck.check(st["state"] == "downloading" and st["num_peers"] == 3
                 and st["download_rate"] == 50, "基础字段映射（STATE_NAMES）")
        ck.check(st["contiguous"] == 150 and st["buffer"] == 0.75,
                 "contiguous/buffer 同源一次扫描（150/200=0.75）")
        ck.check(st["resolving"] is True and 1.5 < st["elapsed"] < 3.0,
                 "resolving/elapsed 走 registry 一致快照（R-1 家族）")
        ck.check(st["file_progress"] == [1, 2] and st["tail_ready"] is True
                 and st["preview_file"] is result.files[0],
                 "file_progress/tail_ready/preview_file 装配")

        class BadFP(FakeHandle):
            def file_progress(self):
                raise RuntimeError("没了")
        with reg.lock:
            reg.handle = BadFP(IH, file=FakeTI())
        st2 = pv.status()
        ck.check(st2["file_progress"] == [],
                 "file_progress 抛 → 空列表（不毁整个快照）")
    finally:
        shutil.rmtree(ws, ignore_errors=True)


def section_task_result(ck):
    ck.section("§G task_result / current_result")
    ws, cache, reg, sched, pv = mk_env()
    try:
        rec, result, h = add_task_rec(reg, cache)
        with reg.lock:
            reg.put_record_locked(IH, rec)
        ck.check(pv.task_result(IH) is result, "task_result 锁读透传")
        ck.check(pv.task_result("f" * 40) is None, "不存在 → None")
        with reg.lock:
            reg.result = result
        ck.check(pv.current_result() is result, "current_result 锁读")
    finally:
        shutil.rmtree(ws, ignore_errors=True)


def section_facade(ck):
    ck.section("§H Facade 接线")
    ws = tempfile.mkdtemp(prefix="mv_preview_facade_")
    try:
        from core.fetcher import SessionManager
        mgr = SessionManager(os.path.join(ws, "cache"))
        ck.check(isinstance(mgr._preview, PreviewCore)
                 and mgr._preview.reg is mgr._registry,
                 "SessionManager 构造出 PreviewCore（复用同一注册表）")
        real = mgr._preview
        seen = []

        class Spy:
            def __getattr__(self, name):
                def w(*a, **k):
                    seen.append(name)
                    return getattr(real, name)(*a, **k)
                return w
        mgr._preview = Spy()
        try:
            mgr.stop_preview()
            mgr.piece_map_for_path("x")
            mgr.demand_for_path("x", 0, 1)
            mgr.task_result("0" * 40)
            mgr.have_piece(0)
            mgr.piece_length()
            mgr.status()
            mgr.current_result
        finally:
            mgr._preview = real
        want = ["stop_preview", "piece_map_for_path", "demand_for_path",
                "task_result", "have_piece", "piece_length", "status",
                "current_result"]
        missing = [n for n in want if n not in seen]
        ck.check(not missing,
                 f"预览/状态 8 入口全部路由到 PreviewCore（缺 {missing or '无'}）")
    finally:
        shutil.rmtree(ws, ignore_errors=True)


def main() -> int:
    ck = ts.Checker("preview_test（阶段 5 预览桥与状态专项）")
    ck.section("core/preview.py 专项验收（假句柄+真注册表，不联网）")
    section_find(ck)
    section_piece_map(ck)
    section_demand(ck)
    section_current(ck)
    section_status(ck)
    section_task_result(ck)
    section_facade(ck)
    return ck.report()


if __name__ == "__main__":
    sys.exit(main())
