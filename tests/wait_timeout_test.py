"""等待超时专项验收（wait_timeout_test）：把「索引块/头部数据等待超时」的
根因修复固化成回归护栏。

背景与根因（详见 diag_wait_timeout.py 与代码内注释）
----------------------------------------------------
症状：播放前门控（头部连续前缀 ≥1MB **且** 尾部 moov 入口就绪）长时间不满足、
流服务 20s 等待上限被打满（416/503 → FFmpeg 致命错误），表现为「缓冲中，等待
索引块就绪」卡住不出画面。排查确认的**真实**成因不是网络下行慢，而是三类
自身开销与判据缺口：

  D1-a 一轮 UI 轮询（700ms）**4 次** `handle.status()` + 4 次位图物化
       （O(块数)，实测 ≈20ns/块；40 万块 = 4 × 7.9ms = 31.5ms，全在 GUI 线程）
  D1-b 流服务可用性探测每 50ms 反查一次落盘目录，旧实现 O(任务数 × 文件数)
       且每次抓任务锁（16 并发 320 次/秒，实测 8% 单核 + 与 GUI/告警线程争锁）
  D2-c `tick()` 里 `_first_missing_from` 逐块 `have_piece()` 绑定调用（P2-1 漏网）
  D2-d `STALL_POLLS` 注释与实际名义时长不符（注释算进了轮询自身开销）
  D3-a `.meta` 缓存**不校验内容 info_hash 与文件名是否一致** → 错配被当命中
  D3-b 每次元数据写入都全目录 `scandir + stat` 剪枝
  D3-c 混合 v1+v2 种子的 `info_hash()` ≠ 真 v1 → v1 btih 查不到缓存（命中率缺口）
  D4   重试容错为线性退避且第 4 次后**放弃**，失败时不主动催数据

本套件逐条断言修复后的**行为**（不是实现细节），并对同一量做改动前后对照。

用法：.\\.venv\\Scripts\\python.exe tests/wait_timeout_test.py
退出码：0=通过 1=失败 2=SKIP（依赖缺失，绝不假装通过）
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import libtorrent as lt  # noqa: E402
    from core import metacache, session as session_mod  # noqa: E402
    from core.models import ParseResult, TorrentFile, have_from_bitmap  # noqa: E402
    from core.preview import PreviewCore  # noqa: E402
    from core.registry import TaskRecord, TaskRegistry  # noqa: E402
    from core.scheduler import (DEADLINE_STEP_MS, PreviewScheduler,  # noqa: E402
                                window_pieces)
    from test_support import Checker  # noqa: E402
except Exception as e:          # 依赖缺失：显式 SKIP，绝不假装通过
    print(f"依赖缺失，无法执行等待超时专项：{e}")
    sys.exit(2)

IH = "a" * 40
IH_B = "b" * 40
PL = 4 * 1024 * 1024


# --------------------------------------------------------------------------
# 替身
# --------------------------------------------------------------------------

class FakeTI:
    def __init__(self, pl=PL, nfiles=1):
        self._pl = pl
        self._n = nfiles

    def piece_length(self):
        return self._pl

    def num_files(self):
        return self._n


class _FakeStatus:
    """torrent_status 替身。"""

    def __init__(self, pieces, npieces):
        self.state = lt.torrent_status.downloading
        self.num_peers = 4
        self.num_seeds = 1
        self.download_payload_rate = 1024
        self.total_done = 0
        self.pieces = pieces


class CountingHandle:
    """记录了 status() / have_piece() 调用次数的句柄替身。

    位图内容与 have_piece 严格一致（协议契约），因此「位图路径」与「逐块路径」
    的结果可比对——这是本套件的核心断言方式。
    """

    def __init__(self, ih=IH, have=(), pl=PL, npieces=400):
        self.ih = ih
        self._have = set(have)
        self._pl = pl
        self._n = npieces
        self.status_calls = 0
        self.have_piece_calls = 0
        self.torrent_file_calls = 0
        self.deadlines: list[tuple[int, int]] = []

    def info_hash(self):
        return self.ih

    def status(self, flags=None):
        # plan/09：生产路径用 status(0) 关掉位图 → flags 调用**不计**位图物化
        if flags is not None:
            return _FakeStatus([], self._n)
        self.status_calls += 1
        have, n = self._have, self._n
        return _FakeStatus([i in have for i in range(n)], n)

    def torrent_file(self):
        self.torrent_file_calls += 1
        return FakeTI(self._pl)

    def has_metadata(self):
        return True

    def have_piece(self, p):
        self.have_piece_calls += 1
        return p in self._have

    def file_progress(self):
        return [0]

    def set_piece_deadline(self, p, ms):
        self.deadlines.append((int(p), int(ms)))

    def clear_piece_deadlines(self):
        self.deadlines.clear()

    def prioritize_files(self, _p):
        pass

    def pause(self):
        pass

    def resume(self):
        pass

    def set_flags(self, _f):
        pass

    def unset_flags(self, _f):
        pass


def _result(ih=IH, nfiles=1, fsize=400 * PL, pl=PL):
    files = [TorrentFile(i, f"root/part{i}.mp4", fsize, i * fsize,
                         (i * fsize) // pl, ((i + 1) * fsize - 1) // pl)
             for i in range(nfiles)]
    return ParseResult(info_hash=ih, name="root",
                       total_size=fsize * nfiles, piece_size=pl,
                       num_pieces=fsize * nfiles // pl, files=files,
                       source="magnet")


def _sched_with(handle, file, tail=True, pl=None):
    s = PreviewScheduler()
    s.handle = handle
    s.file = file
    s._head_n = window_pieces(pl if pl is not None else getattr(handle, "_pl", PL))
    if tail:
        s._tail_pieces = [file.end_piece]
        s._entry_pieces = [file.end_piece]
    return s


# --------------------------------------------------------------------------
# §A 单轮轮询只取一次位图（D1-a / D2-b）
# --------------------------------------------------------------------------

def section_single_snapshot(ck: Checker) -> None:
    ck.section("§A 单轮 UI 轮询的位图快照次数（D1-a / D2-b）")
    cache = tempfile.mkdtemp(prefix="mv_wait_snap_")
    try:
        reg = TaskRegistry(cache_dir=cache, ses_get=lambda: None)
        res = _result()
        h = CountingHandle(npieces=res.num_pieces + 10)
        rec = TaskRecord(handle=h, result=res, save_path=cache, download=True)
        with reg.lock:
            reg.put_record_locked(IH, rec)
            reg.handle = h
            reg.result = res
        sched = _sched_with(h, res.files[0])
        pv = PreviewCore(reg=reg, scheduler_get=lambda: sched)
        # 生产接线（plan/09）：start_preview 会建并注入增量视图
        pv.start_preview(res.files[0])
        ck.check(sched.piece_view is not None,
                 "start_preview 注入了增量可用性视图（规模无关快路径的入口）")

        # 基线的单轮形态：state 快照 + 三个派生量各自 status()（mp4 尾窗路径
        # 实测 4 次）。这里用「逐方法调用」显式复现基线量作为对照。
        # 注意：注入视图后三个派生量各自只做区间查询，不再 status()。
        h.status_calls = 0
        sched.contiguous_progress()
        sched.tail_ready()
        sched.tail_entry_ready()
        legacy = h.status_calls
        ck.check(legacy == 0,
                 f"对照：注入视图后三个派生量各自 status() 次数 = {legacy}"
                 f"（基线无视图时 mp4 路径共 4 次）")

        h.status_calls = 0
        m = sched.gate_snapshot()
        ck.check(h.status_calls == 0,
                 f"gate_snapshot() 零位图物化：status() {h.status_calls} 次（应 0）")
        ck.check(set(m) == {"contiguous", "tail_ready", "tail_entry_ready"},
                 f"gate_snapshot 返回三判据（{sorted(m)}）")

        # 端到端：PreviewCore.status() 一轮的真实物化次数（生产判据）
        h.status_calls = 0
        st = pv.status()
        ck.check(h.status_calls == 0,
                 f"PreviewCore.status() 单轮零位图物化（status() "
                 f"{h.status_calls} 次；基线 4 次 → 现 0 次）")
        ck.check(st is not None and "tail_entry_ready" in st
                 and "tail_ready" in st and "contiguous" in st,
                 "状态快照三判据齐全（门控字段未丢）")

        # 传入外部快照 → 零额外绑定调用
        h.status_calls = 0
        sched.gate_snapshot(have_from_bitmap(h.status().pieces))
        ck.check(h.status_calls == 1,
                 f"gate_snapshot(外部 have) 不再自取快照"
                 f"（总 status() {h.status_calls} 次，含构造 have 的 1 次）")

        # 三判据取值必须与逐方法调用**逐值一致**（行为不变式）
        h._have = set(range(3))
        h.status_calls = 0
        m2 = sched.gate_snapshot()
        exp_contig, exp_tail, exp_entry = (
            sched.contiguous_progress(), sched.tail_ready(),
            sched.tail_entry_ready())
        ck.check((m2["contiguous"], m2["tail_ready"], m2["tail_entry_ready"])
                 == (exp_contig, exp_tail, exp_entry),
                 f"单快照取值与逐方法调用逐值一致"
                 f"（{(m2['contiguous'], m2['tail_ready'], m2['tail_entry_ready'])}"
                 f" vs {(exp_contig, exp_tail, exp_entry)}）")

        # 未激活时的缺省与基线逐值一致
        idle = PreviewScheduler()
        ck.check(idle.gate_snapshot() == {"contiguous": 0, "tail_ready": True,
                                          "tail_entry_ready": True},
                 "未激活 → 缺省 (0, True, True)（与基线逐值一致）")

        # 快照取用失败：句柄失效不得被当成「就绪」放行
        class Boom(CountingHandle):
            def status(self):
                raise RuntimeError("句柄失效")
        hb = Boom()
        sb = _sched_with(hb, res.files[0])
        mb = sb.gate_snapshot()
        ck.check(mb["contiguous"] == 0 and mb["tail_ready"] is False
                 and mb["tail_entry_ready"] is False,
                 f"快照失败 → 门控不放行（{mb}）")
    finally:
        shutil.rmtree(cache, ignore_errors=True)


# --------------------------------------------------------------------------
# §B _first_missing_from 走位图（D2-c，P2-1 对称）
# --------------------------------------------------------------------------

def section_first_missing(ck: Checker) -> None:
    ck.section("§B _first_missing_from：位图路径 + 无 status 替身回退（D2-c）")
    n = 400
    big = TorrentFile(0, "root/big.mp4", n * PL, 0, 0, n - 1)

    h = CountingHandle(have=set(range(250)), npieces=n)
    s = _sched_with(h, big, tail=False)
    s._play_from = 0
    h.have_piece_calls = 0
    got = s._first_missing_from(0, have_from_bitmap(h.status().pieces))
    ck.check(got == 250 and h.have_piece_calls == 0,
             f"共享快照：定位到 piece {got}，have_piece 调用 "
             f"{h.have_piece_calls} 次（应 0）")

    # 无 status() 的替身（smoke_test._FakeHandle 同型）必须仍可工作
    class NoStatus:
        def __init__(self, have):
            self._h = set(have)

        def have_piece(self, p):
            return p in self._h
    ns = NoStatus(range(7))
    s2 = _sched_with(ns, big, tail=False)
    s2._play_from = 0
    ck.check(s2._first_missing_from(0) == 7,
             "无 status() 替身：逐块回退仍给出正确结果（7）")

    # tick() 热路径：整轮只取一次快照
    h3 = CountingHandle(have=set(range(10)), npieces=n)
    s3 = _sched_with(h3, big, tail=True)
    s3._play_from = 0
    h3.status_calls = 0
    s3.tick()
    ck.check(h3.status_calls == 1,
             f"tick() 整轮 status() {h3.status_calls} 次（应 1）")
    ck.check(h3.have_piece_calls == 0,
             f"tick() 不走逐块 have_piece（{h3.have_piece_calls} 次，应 0）")
    ck.check(bool(h3.deadlines), "tick() 仍正常滚动预约窗口（deadline 非空）")


# --------------------------------------------------------------------------
# §C 落盘目录索引（D1-b：O(任务×文件) → O(路径深度)）
# --------------------------------------------------------------------------

def section_path_index(ck: Checker) -> None:
    ck.section("§C 落盘目录反查索引（D1-b）")
    cache = tempfile.mkdtemp(prefix="mv_wait_idx_")
    try:
        reg = TaskRegistry(cache_dir=cache, ses_get=lambda: None)
        pv = PreviewCore(reg=reg, scheduler_get=lambda: PreviewScheduler())

        def add(ih, save_path, nfiles=8, handle=True):
            res = _result(ih=ih, nfiles=nfiles,
                          fsize=nfiles * PL)
            h = CountingHandle(ih=ih) if handle else None
            with reg.lock:
                reg.put_record_locked(
                    ih, TaskRecord(handle=h, result=res, save_path=save_path,
                                   download=True))
            return res, h

        d1 = os.path.join(cache, "downloads", IH)
        add(IH, d1)
        hit = pv.find_record_for_path(os.path.join(d1, "root", "part3.mp4"))
        ck.check(hit is not None and hit[1].path == "root/part3.mp4",
                 "索引命中（相对路径精确匹配）")
        ck.check(pv.find_record_for_path(os.path.join(cache, "nope")) is None,
                 "非受管路径 → None（不得降级为整文件可用）")

        # 新增任务后必须立即可见（结构代次失效）
        d2 = os.path.join(cache, "downloads", IH_B)
        add(IH_B, d2)
        ck.check(pv.find_record_for_path(
            os.path.join(d2, "root", "part1.mp4")) is not None,
            "新入表任务立即出现在索引（结构代次失效生效）")

        # 无句柄/无结果的记录不参与，但不得影响其它记录命中
        add("c" * 40, d1, handle=False)
        ck.check(pv.find_record_for_path(
            os.path.join(d1, "root", "part3.mp4")) is not None,
            "同目录无句柄记录不遮蔽有效记录")

        # 嵌套目录：内层任务目录优先（旧实现按入表序，易被外层抢先）
        inner = os.path.join(d1, "inner")
        add("d" * 40, inner)
        hit2 = pv.find_record_for_path(os.path.join(inner, "root", "part0.mp4"))
        ck.check(hit2 is not None and hit2[0].save_path == inner,
                 "嵌套落盘目录按最内层命中")

        # 移除记录后索引失效（drop_record_locked 触发结构代次）
        with reg.lock:
            reg.drop_record_locked(IH_B)
        ck.check(pv.find_record_for_path(
            os.path.join(d2, "root", "part1.mp4")) is None,
            "记录移除后索引失效（drop_record_locked 生效）")

        # 索引未变时不应抓任务锁（热路径无锁）
        locked = {"n": 0}
        real_lock = reg.lock

        class SpyLock:
            def __enter__(self):
                locked["n"] += 1
                return real_lock.__enter__()

            def __exit__(self, *a):
                return real_lock.__exit__(*a)
        reg.lock = SpyLock()
        try:
            for _ in range(20):
                pv.find_record_for_path(os.path.join(d1, "root", "part3.mp4"))
        finally:
            reg.lock = real_lock
        ck.check(locked["n"] == 0,
                 f"索引命中路径零任务锁（20 次反查抓锁 {locked['n']} 次）")
    finally:
        shutil.rmtree(cache, ignore_errors=True)


# --------------------------------------------------------------------------
# §D 元数据缓存：内容一致性 + 别名命中 + 剪枝节流（D3-a/b/c）
# --------------------------------------------------------------------------

def _make_ti(tmp: str):
    payload = os.path.join(tmp, "payload")
    os.makedirs(payload, exist_ok=True)
    with open(os.path.join(payload, "a.bin"), "wb") as f:
        f.write(os.urandom(64 * 1024))
    fs = lt.file_storage()
    lt.add_files(fs, payload)
    ct = lt.create_torrent(fs, 16 * 1024)
    lt.set_piece_hashes(ct, tmp)
    return lt.torrent_info(ct.generate())


def section_metacache(ck: Checker) -> None:
    ck.section("§D 元数据缓存：一致性校验 / 别名命中 / 剪枝节流（D3-a/b/c）")
    tmp = tempfile.mkdtemp(prefix="mv_wait_meta_")
    try:
        cache = os.path.join(tmp, "cache")
        os.makedirs(cache, exist_ok=True)
        ti = _make_ti(tmp)
        ih = str(ti.info_hash())
        v1, v2 = metacache.info_hash_of(ti)
        ck.check(bool(v1) and bool(v2) and v1 != ih,
                 f"本环境混合种子：info_hash()={ih[:8]}… ≠ v1={v1[:8]}…"
                 f"（这正是旧实现丢命中的原因）")
        ck.check(ih in metacache.alias_keys(ti)
                 and v1 in metacache.alias_keys(ti)
                 and v2 in metacache.alias_keys(ti),
                 f"alias_keys 覆盖 {len(metacache.alias_keys(ti))} 个可查键")

        metacache.reset_stats()
        ck.check(metacache.save_torrent_info(cache, ih, ti) is not None,
                 "save_torrent_info 落盘成功（含别名键）")
        metacache.reset_stats()
        ck.check(metacache.load_torrent_info(cache, ih) is not None,
                 "主键（info_hash()）命中")
        ck.check(metacache.load_torrent_info(cache, v1) is not None,
                 "真 v1 btih 别名命中（D3-c：混合种子命中率缺口已补）")
        ck.check(metacache.load_torrent_info(cache, v2) is not None,
                 "完整 v2 别名命中")
        ck.check(metacache.stats()["hits"] == 3,
                 f"三次查找全部计 hit（{metacache.stats()['hits']}）")
        ck.check(metacache.hit_rate() == 1.0,
                 f"命中率 100%（{metacache.hit_rate():.2f}）")

        # D3-a 内容错配：把 A 的字节放到 B 的键下 → 必须被识别并删除
        ih_b = "f" * 40
        p_b = metacache.meta_path(cache, ih_b)
        metacache.write_meta(cache, ih_b, metacache.encode_meta(ti))
        before = metacache.stats()["mismatch"]
        ck.check(metacache.load_torrent_info(cache, ih_b) is None,
                 "内容 info_hash 与文件名不符 → 按未命中（绝不当命中返回）")
        ck.check(metacache.stats()["mismatch"] == before + 1,
                 "错配计一次 mismatch 统计")
        ck.check(not os.path.isfile(p_b), "错配条目已删除（自愈 + 防投毒）")
        ck.check(metacache.load_torrent_info(cache, ih) is not None,
                 "错配删除不影响同名合法条目")

        # 显式失效
        ck.check(metacache.invalidate_meta(cache, v1) is True
                 and not metacache.has_meta(cache, v1),
                 "invalidate_meta 显式失效单条（幂等第二手）")
        ck.check(metacache.invalidate_meta(cache, "../evil") is False,
                 "非法键显式失效 → False（不拼路径）")

        # D3-b 剪枝节流：写入路径不再每次全目录扫描
        scans = {"n": 0}
        real_scandir = os.scandir
        meta_d = os.path.normcase(metacache.meta_dir(cache))

        def counting_scandir(p):
            if os.path.normcase(str(p)) == meta_d:
                scans["n"] += 1
            return real_scandir(p)
        metacache.reset_stats()
        os.scandir = counting_scandir
        try:
            for _ in range(30):
                metacache.save_torrent_info(cache, ih, ti, aliases=False)
        finally:
            os.scandir = real_scandir
        ck.check(scans["n"] <= 2,
                 f"30 次写入仅扫目录 {scans['n']} 次（基线 30 次）")

        # 超限仍必须真剪（节流不得把 LRU 弄失效）
        for i in range(6):
            metacache.write_meta(cache, "a" * 39 + str(i), b"x" * 8)
            time.sleep(0.01)
        n = metacache.prune_meta(cache, max_files=2)
        ck.check(n == 6, f"显式 prune 仍删到上限（实删 {n}，期望 6）")
        left = sorted(os.listdir(metacache.meta_dir(cache)))
        ck.check(len(left) == 2, f"剪枝后目录内仅剩 2 条（{left}）")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------------------
# §E 重试容错：指数退避不放弃 + 失败时催数据（D4）
# --------------------------------------------------------------------------

def section_retry(ck: Checker) -> None:
    ck.section("§E 重试容错：指数退避 / 不硬性放弃 / 失败即催数据（D4）")
    # Wave 2 拆分：退避/门控常量与执行体移居 ui/stream_host.py（协作者）
    from ui.stream_host import (StreamHost, gate_wait_timeout_reason,
                                stream_failure_message)
    base = StreamHost.STREAM_RETRY_BASE_MS
    cap = StreamHost.STREAM_RETRY_MAX_MS
    seq = [min(cap, base * (2 ** min(6, n - 1))) for n in range(1, 8)]
    ck.check(seq == sorted(seq) and all(d >= base for d in seq),
             f"退避序列单调不减且起点 {base}ms：{seq}")
    ck.check(seq[0] < seq[-1] and max(seq) <= cap,
             f"指数增长并封顶 {cap}ms（末值 {seq[-1]}）")
    ck.check(StreamHost.STREAM_RETRY_SOFT_LIMIT >= 2,
             f"软上限 {StreamHost.STREAM_RETRY_SOFT_LIMIT}：只提示不终止重试")

    # ---- C3：开播门控必须有等待上限，且超时文案可归因 ----
    gate_ms = StreamHost.GATE_WAIT_TIMEOUT_MS
    ck.check(0 < gate_ms <= 120_000,
             f"门控等待上限存在且在合理区间（{gate_ms}ms）")

    class _F:
        size = 8 * 1024 * 1024

    st = {"contiguous": 256 * 1024, "tail_entry_ready": False,
          "num_peers": 0, "num_seeds": 0, "download_rate": 0,
          "state": "DOWNLOADING"}
    reason = gate_wait_timeout_reason(st, _F(), 61.0)
    ck.check("已等 61 秒" in reason,
             f"超时文案含已等时长：{reason[:48]}…")
    # contig=256KB → 0.25MB；target=min(1MB, 8MB)=1MB → 1.00MB
    ck.check("0.25MB" in reason and "需 1.00MB" in reason,
             f"文案给出「已连续 / 需要」的实际进度（实际：{reason[20:70]}）")
    ck.check("索引块未就绪" in reason, "文案区分是缺数据还是缺索引块")
    ck.check("peer 0" in reason and "种子 0" in reason,
             "文案给出 peer / 种子事实数字（不加结论）")
    ck.check("再次点击播放" in reason, "文案给出可操作的补救（重试）")
    ck.check("就是资源没种" not in reason and "无做种" in reason,
             "用「多半是」的表述陈述可能性，不武断定性")
    hint = gate_wait_timeout_reason(st, _F(), 61.0, "；另：已配置代理 x")
    ck.check(hint.endswith("已配置代理 x"), "代理提示被原样附加在末尾")

    base_msg = stream_failure_message()
    ck.check(base_msg.startswith("播放器多次打开失败"), "无应答摘要时保持原文案")
    with_note = stream_failure_message("流服务最近应答：503，就绪 0 字节")
    ck.check(with_note.startswith(base_msg) and "503" in with_note,
             "有应答摘要时附加在原文案之后（C4：失败可归因）")

    # ---- 接线验证：on_gate_timeout / last_stream_note 的**行为**
    # 用鸭子类型宿主直接调 StreamHost 未绑定方法（Wave 2 拆分：门控超时/
    # 应答摘要的执行体移居 ui/stream_host.py，宿主经 win 晚绑定访问）：
    # 既能验行为，又不必搭整个 GUI。
    class _V:
        def __init__(self):
            self.err = None

        def show_error(self, m):
            self.err = m

    class _S:
        def __init__(self):
            self.state = None

        def set_state(self, m):
            self.state = m

    class _Win:
        """StreamHost 的鸭子宿主：只喂它用到的成员，**不建 GUI**。"""

        def __init__(self):
            self.preview = type("P", (), {"video": _V()})()
            self.status_panel = _S()
            self.cfg = type("C", (), {
                "proxy": staticmethod(
                    lambda: {"type": "none", "host": ""})})()

    win = _Win()
    host = StreamHost.__new__(StreamHost)      # 绕过 __init__（不建流服务）
    host._win = win
    host.pending_video = ("pair",)
    StreamHost.on_gate_timeout(host, _F(), st, 61.0)
    ck.check(host.pending_video is None,
             "门控超时后清掉待播状态（停止空转，用户可再点播放重试）")
    ck.check(bool(win.preview.video.err)
             and "等待开播数据超时" in win.preview.video.err,
             f"超时结论上屏到播放页（{str(win.preview.video.err)[:32]}…）")
    ck.check(win.status_panel.state == win.preview.video.err,
             "状态栏与播放页文案一致（不看播放页也能看到）")

    class _SrvOK:
        def last_response_note(self):
            return "流服务最近应答：503，就绪 0 字节，等待 20000ms"

    class _SrvBad:
        def last_response_note(self):
            raise RuntimeError("boom")

    host.server = _SrvOK()
    ck.check(StreamHost.last_stream_note(host).startswith("流服务最近应答"),
             "C4：从流服务取到最近应答摘要")
    host.server = _SrvBad()
    ck.check(StreamHost.last_stream_note(host) == "",
             "C4：取摘要失败时回落空串（绝不因归因失败而抛）")

    cache = tempfile.mkdtemp(prefix="mv_wait_retry_")
    try:
        reg = TaskRegistry(cache_dir=cache, ses_get=lambda: None)
        res = _result()
        h = CountingHandle(have=set(range(2)))
        with reg.lock:
            reg.put_record_locked(
                IH, TaskRecord(handle=h, result=res, save_path=cache,
                               download=True))
        s = _sched_with(h, res.files[0])
        s._play_from = 0
        s._scheduled_to = res.files[0].end_piece      # 模拟「窗口看起来很充裕」
        h.deadlines.clear()
        s.tick()
        entry = res.files[0].end_piece
        ck.check({p for p, _ in h.deadlines} == {entry},
                 f"对照：tick() 有防抖——头部窗口充裕时不重复预约，只补尾部"
                 f"入口（实际 {sorted({p for p, _ in h.deadlines})}）")
        s.rearm()
        ck.check(bool(h.deadlines) and any(p == entry for p, _ in h.deadlines),
                 f"rearm() 无条件重刷（{len(h.deadlines)} 条，含尾部入口 "
                 f"piece {entry}）")
        ck.check(min(ms for _, ms in h.deadlines) == 0,
                 "rearm() 首块 deadline = 0（立即插队）")
        n_head = window_pieces(PL)
        ck.check(len({p for p, _ in h.deadlines}) <= n_head + 1,
                 f"rearm() 只覆盖播放窗口 + 尾部入口（"
                 f"{len({p for p, _ in h.deadlines})} 块 ≤ {n_head + 1}），不刷全文件")
        idle = PreviewScheduler()
        idle.rearm()      # 未激活：必须静默返回
        ck.check(True, "rearm() 未激活时不抛异常（幂等安全）")
    finally:
        shutil.rmtree(cache, ignore_errors=True)


# --------------------------------------------------------------------------
# §F 吞吐参数：键真实存在且真实生效（D4）
# --------------------------------------------------------------------------

def section_throughput(ck: Checker) -> None:
    ck.section("§F 吞吐/容错参数真实生效（D4）")
    settings = session_mod.build_session_settings(0, 3)
    # 本表是**批次承诺值**（刻意与实现不同源，否则失去拦截作用）：
    # aio_threads 8→12、hashing_threads 2→4、max_out_request_queue 1000→1500
    # 随「千兆吞吐专项」（plan/10_gigabit_throughput_plan.md）上调，
    # 改参数必须同步改这里，否则本套件应当报红。
    new_keys = {"aio_threads": 12, "hashing_threads": 4,
                "piece_extent_affinity": True, "max_out_request_queue": 1500,
                "max_failcount": 5}
    for k, v in new_keys.items():
        ck.check(settings.get(k) == v,
                 f"构造值 {k} = {settings.get(k)}（期望 {v}）")
    ses = lt.session(settings)
    got = ses.get_settings()
    missing = [k for k in session_mod.THROUGHPUT_SETTINGS if k not in got]
    mismatch = [k for k in session_mod.THROUGHPUT_SETTINGS
                if k in got and int(got[k]) != int(session_mod.THROUGHPUT_SETTINGS[k])]
    ck.check(not missing,
             f"全部 {len(session_mod.THROUGHPUT_SETTINGS)} 个键真实存在"
             f"（缺失 {missing or '无'}）")
    ck.check(not mismatch,
             f"真会话回读全部生效（未生效 {mismatch or '无'}）")
    for k in new_keys:
        ck.check(int(got[k]) == int(new_keys[k]),
                 f"真会话回读 {k} = {got[k]}")
    ses = None


# --------------------------------------------------------------------------

def main() -> int:
    ck = Checker("wait_timeout_test（等待超时专项：根因护栏）")
    print("\n--- 等待超时根因护栏（2026-09-17） ---")
    section_single_snapshot(ck)
    section_first_missing(ck)
    section_path_index(ck)
    section_metacache(ck)
    section_retry(ck)
    section_throughput(ck)
    return ck.report()


if __name__ == "__main__":
    sys.exit(main())
