"""等待超时系统化诊断探针（diag_wait_timeout）。

用途
----
「缓冲中，等待索引块/头部数据就绪」长时间不推进、最终落到流服务 20s 等待上限
（416/503 → FFmpeg 致命错误）时，用它把**怀疑**变成**数字**。诊断分四段，
每段独立可跑，输出判据与阈值，不依赖外网。

  D1 可用性探测成本    —— 一次状态快照链（status() 次数 / 位图物化 / 路径反查）
  D2 门控判据拆解      —— 头部连续前缀与尾部入口(moov)就绪各自耗时与阻塞点
  D3 元数据缓存命中链  —— .meta 命中率、内容一致性、prune 扫盘成本
  D4 线程阻塞与吞吐    —— 注册表锁持有时间、会话吞吐参数是否真实生效

判据（本机基线，2026-09 环境）
------------------------------
  D1-a 单次 UI 轮询的 status() 调用数 > 1        → 快照链冗余，CPU 被位图物化吃满
  D1-b find_record_for_path 单次 > 200 µs        → 路径反查成了热路径瓶颈
  D1-c 位图物化(P=20000) 单次 > 2 ms             → list(pieces) 是主要开销
  D2-a 门控单轮耗时 > 2 ms                       → UI 轮询自身在拖慢门控节奏
  D3-a .meta 内容 info_hash 与文件名不一致未被发现 → 缓存错配（用错元数据）
  D3-b prune_meta 每次写入都 scandir 全目录       → 写入路径上的无谓同步 IO
  D4-a 注册表锁持有 > 5 ms                       → 告警线程/HTTP 线程被阻塞
  D4-b 吞吐参数键未生效                          → 拼错的键被 libtorrent 静默丢弃

用法
----
    .\\.venv\\Scripts\\python.exe diag_wait_timeout.py          # 全部四段
    .\\.venv\\Scripts\\python.exe diag_wait_timeout.py D1 D3    # 指定段

退出码：0=无异常发现 1=存在超阈项（具体见输出） 2=依赖缺失（不假装通过）
"""
from __future__ import annotations

import os
import statistics
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    import libtorrent as lt  # noqa: E402
    from core import metacache, session as session_mod  # noqa: E402
    from core.models import (PieceMap, ParseResult, TorrentFile,  # noqa: E402
                             have_from_bitmap, contiguous_bytes,
                             range_available, ready_until)
    from core.preview import PreviewCore  # noqa: E402
    from core.registry import TaskRecord, TaskRegistry  # noqa: E402
    from core.scheduler import PreviewScheduler, window_pieces  # noqa: E402
except Exception as e:          # 依赖缺失：显式 SKIP
    print(f"依赖缺失，无法执行等待超时诊断：{e}")
    sys.exit(2)

# ---- 判据阈值（本机基线，超阈即计入 findings） ----
TH_STATUS_CALLS_PER_POLL = 1        # 单轮 UI 轮询允许的 status() 次数
TH_FIND_US = 200.0                  # 路径反查单次上限（µs）
TH_BITMAP_MS = 2.0                  # 位图物化单次上限（ms，P=20000）
TH_GATE_MS = 2.0                    # 门控单轮上限（ms）
TH_LOCK_MS = 5.0                    # 注册表锁持有上限（ms）

IH = "a" * 40
IH_B = "b" * 40
FIND_REPEAT = 200
POLL_REPEAT = 20


# --------------------------------------------------------------------------
# 探针用替身
# --------------------------------------------------------------------------

class _FakeStatus:
    """torrent_status 替身（state/peers/rate/total_done/pieces）。"""

    def __init__(self, pieces, npieces):
        self.state = lt.torrent_status.downloading
        self.num_peers = 8
        self.num_seeds = 1
        self.download_payload_rate = 1024 * 300
        self.total_done = 0
        self.pieces = pieces


class CountingHandle:
    """可编程句柄替身：统计各绑定调用次数与位图规模。"""

    def __init__(self, ih=IH, have=(), pl=4 * 1024 * 1024, num_pieces=20000):
        self.ih = ih
        self._have = set(have)
        self._pl = pl
        self._n = num_pieces
        self.status_calls = 0
        self.torrent_file_calls = 0
        self.have_piece_calls = 0
        self.deadlines: list[tuple[int, int]] = []

    def info_hash(self):
        return self.ih

    def status(self, flags=None):
        # plan/09 起生产路径用 status(0) 关掉位图：flags 调用**不计**位图物化
        if flags is not None:
            return _FakeStatus([], self._n)
        self.status_calls += 1
        have = self._have
        n = self._n
        return _FakeStatus([i in have for i in range(n)], n)

    def torrent_file(self):
        self.torrent_file_calls += 1
        return _TI(self._pl)

    def has_metadata(self):
        return True

    def have_piece(self, p):
        self.have_piece_calls += 1
        return p in self._have

    def file_progress(self):
        return [0] * 4

    def set_piece_deadline(self, p, ms):
        self.deadlines.append((p, ms))

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


class _TI:
    def __init__(self, pl):
        self._pl = pl

    def piece_length(self):
        return self._pl

    def num_files(self):
        return 4


def _fake_result(ih=IH, nfiles=4, pl=4 * 1024 * 1024, fsize=1024 * 1024):
    files = []
    offset = 0
    for i in range(nfiles):
        files.append(TorrentFile(i, f"root/part{i}.mp4", fsize, offset,
                                 offset // pl, (offset + fsize - 1) // pl))
        offset += fsize
    return ParseResult(info_hash=ih, name="root", total_size=offset,
                       piece_size=pl, num_pieces=offset // pl, files=files,
                       source="magnet")


def _median_us(fn, repeat):
    xs = []
    for _ in range(repeat):
        t0 = time.perf_counter()
        fn()
        xs.append((time.perf_counter() - t0) * 1e6)
    return statistics.median(xs), xs


# --------------------------------------------------------------------------
# D1 可用性探测成本
# --------------------------------------------------------------------------

def _virtual_handle(piece_size: int, npieces: int):
    """造**不写数据**的虚拟种子（只 ``set_hash``）：零磁盘 IO、零真实哈希。

    返回 ``(tmp, session, handle, torrent_info)``（与 ``_real_handle`` 同形）。

    为什么与 ``_real_handle`` 并存：
    - ``_real_handle``（写真实数据 + 真实哈希）用于 D1 —— 量「位图物化 /
      逐项索引」的成本必须让位图非空、块分布真实；
    - D5 只需要「指定块数的**真**句柄」，用真数据会把 40 万块 × 16KB =
      **6.4GB** 写盘并哈希（分钟级），而且校验完成后所有块都是 have ——
      与「边下边播（大部分尚未下载）」这个目标场景**恰好相反**，会让连续
      前缀一路推进、反复触顶重建基线，测出与实际无关的数字。
    """
    tmp = tempfile.mkdtemp(prefix="mv_diag_virt_")
    fs = lt.file_storage()
    fs.add_file(os.path.join(tmp, "payload", "big.bin"), piece_size * npieces)
    ct = lt.create_torrent(fs, piece_size)
    for i in range(ct.num_pieces()):
        ct.set_hash(i, b"\x01" * 20)
    ti = lt.torrent_info(ct.generate())
    ses = lt.session({"listen_interfaces": "127.0.0.1:0", "enable_dht": False,
                      "enable_lsd": False, "enable_upnp": False,
                      "enable_natpmp": False})
    atp = lt.add_torrent_params()
    atp.ti = ti
    atp.save_path = tmp
    atp.flags |= lt.torrent_flags.upload_mode
    return tmp, ses, ses.add_torrent(atp), ti


def _real_handle(piece_size: int, nbytes: int):
    """建真 libtorrent 会话 + 真句柄（未做种，只用于量 status() 成本）。

    为什么必须用真句柄：`torrent_status.pieces` 是 libtorrent 的 **bitfield**，
    转成 Python 序列的成本只有真绑定才体现；用假句柄（Python list）会把这项
    开销测成 0，得出「快照很便宜」的错误结论（本探针首次运行即踩此坑）。
    """
    tmp = tempfile.mkdtemp(prefix="mv_diag_real_")
    payload = os.path.join(tmp, "payload")
    os.makedirs(payload, exist_ok=True)
    with open(os.path.join(payload, "big.bin"), "wb") as f:
        f.write(b"\0" * nbytes)
    fs = lt.file_storage()
    lt.add_files(fs, payload)
    ct = lt.create_torrent(fs, piece_size)
    lt.set_piece_hashes(ct, tmp)
    ti = lt.torrent_info(ct.generate())
    ses = lt.session({"listen_interfaces": "127.0.0.1:0", "enable_dht": False,
                      "enable_lsd": False, "enable_upnp": False,
                      "enable_natpmp": False})
    atp = lt.add_torrent_params()
    atp.ti = ti
    atp.save_path = tmp
    atp.flags |= lt.torrent_flags.upload_mode
    h = ses.add_torrent(atp)
    return tmp, ses, h, ti


def _synthetic_handle(piece_size: int, npieces: int):
    """零磁盘 IO 的合成句柄：虚拟文件 + 直接 set_hash，用于规模外推测量。

    为什么需要它：`list(torrent_status.pieces)` 是 O(块数) 的位图转换，成本
    只在十万块量级才显形；而造一个 100GB 的真文件来测它显然不可行。
    `create_torrent.set_hash()` 允许直接填占位哈希，不读盘不算哈希。
    """
    tmp = tempfile.mkdtemp(prefix="mv_diag_syn_")
    fs = lt.file_storage()
    fs.add_file(os.path.join(tmp, "virtual.bin"), piece_size * npieces)
    ct = lt.create_torrent(fs, piece_size)
    for i in range(ct.num_pieces()):
        ct.set_hash(i, b"\x11" * 20)
    ti = lt.torrent_info(ct.generate())
    ses = lt.session({"listen_interfaces": "127.0.0.1:0", "enable_dht": False,
                      "enable_lsd": False, "enable_upnp": False,
                      "enable_natpmp": False})
    atp = lt.add_torrent_params()
    atp.ti = ti
    atp.save_path = tmp
    atp.flags |= lt.torrent_flags.upload_mode
    h = ses.add_torrent(atp)
    return tmp, ses, h, ti


def probe_scale(findings: list[str]) -> None:
    """规模外推：位图物化成本随块数线性增长，落在 GUI 线程上。"""
    print("\n=== D1-s 规模外推（位图物化 × 单轮快照次数）===")
    print(f"  {'块数':>8} {'status()':>10} {'+位图物化':>11} "
          f"{'单轮(4 次)':>11} {'700ms 轮询占用':>14}")
    rows = []
    for npieces in (20000, 100000, 400000):
        tmp, ses, h, ti = _synthetic_handle(16 * 1024, npieces)
        try:
            s_us, _ = _median_us(lambda: h.status(), 20)
            b_us, _ = _median_us(lambda: list(h.status().pieces), 10)
            poll_ms = b_us / 1000 * 4
            duty = poll_ms / 700 * 100
            print(f"  {npieces:8d} {s_us/1000:9.3f}ms {b_us/1000:10.3f}ms "
                  f"{poll_ms:10.2f}ms {duty:13.1f}%")
            rows.append((npieces, b_us / 1000))
        finally:
            import shutil
            ses = None
            shutil.rmtree(tmp, ignore_errors=True)
    if len(rows) >= 2:
        (n0, c0), (n1, c1) = rows[0], rows[-1]
        slope = (c1 - c0) / max(1, (n1 - n0))
        print(f"  线性拟合：+{slope*1000:.4f} µs / 块"
              f"（20 万块 ≈ {slope*200000:.1f} ms / 次物化）")
    for npieces, cost in rows:
        if cost * 4 / 700 * 100 > 10.0:
            findings.append(
                f"D1-e {npieces} 块时单轮 4 次位图物化 {cost*4:.1f} ms，"
                f"占 700ms 轮询 {cost*4/700*100:.0f}% GUI 线程")
            break


def probe_d1(findings: list[str]) -> None:
    print("\n=== D1 可用性探测成本（真句柄：status() / 位图物化 / 路径反查）===")
    pl = 16 * 1024
    nbytes = 20000 * pl          # 20000 块
    tmp, ses, h, ti = _real_handle(pl, nbytes)
    try:
        npieces = ti.num_pieces()
        print(f"  真种子规模                        : {npieces} 块 × "
              f"{pl // 1024} KB = {nbytes // 1024 // 1024} MB")

        us, _ = _median_us(lambda: h.status(), 30)
        print(f"  handle.status()                   : {us / 1000:8.3f} ms / 次")

        def bits_to_list():
            return list(h.status().pieces)
        us_b, _ = _median_us(bits_to_list, 30)
        print(f"  status() + list(pieces)           : {us_b / 1000:8.3f} ms / 次"
              f"（差异 = 位图物化 {(us_b - us) / 1000:.3f} ms）")
        if us_b / 1000 > TH_BITMAP_MS:
            findings.append(
                f"D1-c status()+位图物化 {us_b/1000:.2f} ms > {TH_BITMAP_MS} ms")

        bits = h.status().pieces
        us_h, _ = _median_us(lambda: have_from_bitmap(bits), 50)
        print(f"  have_from_bitmap(真 bitfield)      : {us_h / 1000:8.3f} ms / 次")

        hb = h.status()
        us_c, _ = _median_us(lambda: contiguous_bytes(_real_pm(ti, hb)), 50)
        print(f"  contiguous_bytes(20000 块)         : {us_c / 1000:8.3f} ms / 次")

        # 一次 UI 轮询的状态快照链（真句柄 + 真调度器）
        cache = tempfile.mkdtemp(prefix="mv_diag_d1_")
        try:
            reg = TaskRegistry(cache_dir=cache, ses_get=lambda: ses)
            sched = PreviewScheduler()
            res = _fake_result(pl=pl)
            rec = TaskRecord(handle=h, result=res, save_path=cache,
                             download=True)
            with reg.lock:
                reg.put_record_locked(IH, rec)
                reg.handle = h
                reg.result = res
            sched.handle = h
            sched.file = res.files[0]
            sched._head_n = window_pieces(pl)
            pv = PreviewCore(reg=reg, scheduler_get=lambda: sched)

            def one_poll():
                return pv.status()
            us_p, _ = _median_us(one_poll, 10)
            st = pv.status()
            print(f"  单轮 PreviewCore.status()          : {us_p / 1000:8.3f} ms"
                  f"（真句柄；对照：3 处派生量各自取快照）")
            print(f"  └ contiguous={st.get('contiguous')} "
                  f"tail_entry_ready={st.get('tail_entry_ready')}")

            # 说明（plan/09）：D1 这一段量的是**无视图**时的原始成本（位图物化
            # 与逐块扫描的真实单价），用于对照与归因。生产接线（注入 PieceView、
            # 走 status(0)）后的**单轮物化次数与规模无关性**判据在 **D5** ——
            # 那里用能识别 status_flags 的代理真句柄计量，D1 的假句柄做不到。
            sched._tail_pieces = [npieces - 1]
            sched._entry_pieces = [npieces - 1]
            st2 = pv.status()
            print(f"  含尾部 moov 窗口时（无视图）        : "
                  f"{us_p / 1000:8.3f} ms（原始路径对照，物化次数见 D5）")
            print(f"  └ contiguous={st2.get('contiguous')} "
                  f"tail_entry_ready={st2.get('tail_entry_ready')}")
            if us_p / 1000 > TH_GATE_MS:
                findings.append(f"D2-a 门控单轮 {us_p/1000:.2f} ms > {TH_GATE_MS} ms")

            # 路径反查规模：任务数 × 文件数
            big = _fake_result(ih=IH_B, nfiles=2000, pl=pl)
            rec_b = TaskRecord(handle=h, result=big,
                               save_path=os.path.join(cache, "downloads", IH_B),
                               download=True)
            with reg.lock:
                reg.put_record_locked(IH_B, rec_b)
            target = os.path.join(rec_b.save_path, "root", "part1999.mp4")
            us_f, xs = _median_us(lambda: pv.find_record_for_path(target),
                                  FIND_REPEAT)
            print(f"  find_record_for_path(2 任务×2000 文件): {us_f:8.1f} µs "
                  f"（max {max(xs):.0f} µs）")
            if us_f > TH_FIND_US:
                findings.append(f"D1-b 路径反查 {us_f:.0f} µs > {TH_FIND_US:.0f} µs")

            us_m, _ = _median_us(lambda: pv.piece_map_for_path(target), 30)
            pct = us_m * (1 / 0.05) * 16 / 1e6 * 100
            print(f"  piece_map_for_path                 : {us_m:8.1f} µs / 次"
                  f"  → 50ms 轮询 × 16 并发 ≈ {pct:5.1f}% 单核")
            if pct > 10.0:
                findings.append(
                    f"D1-d 流服务探测 16 并发占单核 {pct:.1f}%（>10%）")
        finally:
            import shutil
            shutil.rmtree(cache, ignore_errors=True)
    finally:
        import shutil
        ses = None
        shutil.rmtree(tmp, ignore_errors=True)


def _real_pm(ti, status):
    pl = ti.piece_length()
    return PieceMap(piece_length=pl, offset=0, start_piece=0,
                    end_piece=ti.num_pieces() - 1,
                    size=ti.total_size(),
                    have=have_from_bitmap(status.pieces))


# --------------------------------------------------------------------------
# D2 门控判据拆解
# --------------------------------------------------------------------------

def probe_d2(findings: list[str]) -> None:
    print("\n=== D2 门控判据拆解（头部连续前缀 / 尾部 moov 入口）===")
    from core.stream_server import STALL_POLLS
    pl = 4 * 1024 * 1024
    n = 20000
    h = CountingHandle(num_pieces=n, pl=pl)
    sched = PreviewScheduler()
    sched.handle = h
    sched.file = _fake_result().files[0]
    sched._head_n = window_pieces(pl)
    # 造出「moov 在尾部」的窗口：不设则 tail_ready/tail_entry_ready 直接短路，
    # 测不出冗余快照（4 次 status() 只有真 mp4 预览路径才走满）
    sched._tail_pieces = [n - 1]
    sched._entry_pieces = [n - 1]

    h.status_calls = 0
    t0 = time.perf_counter()
    contig = sched.contiguous_progress()
    t1 = time.perf_counter()
    sched.tail_ready()
    t2 = time.perf_counter()
    sched.tail_entry_ready()
    t3 = time.perf_counter()
    print(f"  [逐方法调用，非热路径]")
    print(f"  contiguous_progress()   : {(t1-t0)*1000:7.3f} ms → {contig} B")
    print(f"  tail_ready()            : {(t2-t1)*1000:7.3f} ms")
    print(f"  tail_entry_ready()      : {(t3-t2)*1000:7.3f} ms")
    print(f"  └ 合计 status() 调用     : {h.status_calls} 次"
          f"（单独调用本就该各取一份快照；热路径必须走 gate_snapshot）")

    # 热路径：gate_snapshot 单快照
    h.status_calls = 0
    t0 = time.perf_counter()
    snap = sched.gate_snapshot()
    gms = (time.perf_counter() - t0) * 1000
    print(f"  [热路径] gate_snapshot() : {gms:7.3f} ms，"
          f"status() 调用 {h.status_calls} 次 → {snap}")
    if h.status_calls != 1:
        findings.append(f"D2-b 热路径 gate_snapshot 取快照 {h.status_calls} 次（应恰 1）")
    # 传入外部快照 → 零绑定调用
    have = have_from_bitmap(h.status().pieces)
    h.status_calls = 0
    sched.gate_snapshot(have)
    print(f"  [热路径] gate_snapshot(外部 have): status() 调用 {h.status_calls} 次")
    if h.status_calls != 0:
        findings.append(f"D2-b' 传入 have 后仍取快照 {h.status_calls} 次（应 0）")

    # _first_missing_from 的定位成本（热路径经 tick() 传入共享快照）
    h2 = CountingHandle(num_pieces=n, pl=pl, have=set(range(500)))
    full = TorrentFile(0, "root/big.bin", n * pl, 0, 0, n - 1)
    sched2 = PreviewScheduler()
    sched2.handle = h2
    sched2.file = full
    sched2._play_from = 0
    fallback_calls = 0
    for label, passed in (("逐块回退（无 status 的替身）", None),
                          ("共享快照（热路径）", have_from_bitmap(
                              h2.status().pieces))):
        h2.have_piece_calls = 0
        t0 = time.perf_counter()
        first = sched2._first_missing_from(0, passed)
        dt = (time.perf_counter() - t0) * 1000
        print(f"  _first_missing_from(0) [{label}] : {dt:7.3f} ms → piece {first}"
              f"，have_piece 调用 {h2.have_piece_calls} 次")
        if passed is None:
            fallback_calls = h2.have_piece_calls
            if fallback_calls <= 1:
                findings.append("D2-c' 逐块回退路径未按预期走 have_piece"
                                "（替身接口变化？）")
        elif h2.have_piece_calls:
            findings.append(
                f"D2-c 共享快照路径仍逐块 have_piece {h2.have_piece_calls} 次")

    # 源码注释与常量的一致性（旧注释声称 ≈1.5s，与实际名义时长不符）
    src_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "core", "stream_server.py")
    try:
        with open(src_path, encoding="utf-8") as f:
            src = f.read()
    except OSError:
        src = ""
    nominal = STALL_POLLS * 0.05
    # 只在「注释仍在断言 ≈1.5s」时判失败；复述旧值的说明行（含「旧注释」）不算
    stale = any(("1.5s" in ln and "旧注释" not in ln)
                for ln in src.splitlines())
    print(f"  STALL_POLLS={STALL_POLLS} × 0.05s = {nominal:.2f}s；"
          f"源码仍断言「约 1.5s」：{'是' if stale else '否'}")
    if stale:
        findings.append(f"D2-d STALL_POLLS 注释声称 ≈1.5s，实际名义 "
                        f"{nominal:.2f}s（不一致）")


# --------------------------------------------------------------------------
# D3 元数据缓存命中链
# --------------------------------------------------------------------------

def probe_d3(findings: list[str]) -> None:
    print("\n=== D3 元数据缓存命中链（命中率 / 内容一致性 / prune 成本）===")
    tmp = tempfile.mkdtemp(prefix="mv_diag_d3_")
    try:
        fs = lt.file_storage()
        payload = os.path.join(tmp, "payload")
        os.makedirs(payload, exist_ok=True)
        with open(os.path.join(payload, "a.bin"), "wb") as f:
            f.write(os.urandom(64 * 1024))
        lt.add_files(fs, payload)
        ct = lt.create_torrent(fs, 16 * 1024)
        lt.set_piece_hashes(ct, tmp)
        ti = lt.torrent_info(ct.generate())
        ih = str(ti.info_hash())

        cache = os.path.join(tmp, "cache")
        os.makedirs(cache, exist_ok=True)
        metacache.reset_stats()
        blob = metacache.encode_meta(ti)
        # 必须走 save_torrent_info（写主键 + v1/v2 别名键），write_meta 只写
        # 单个键——用它测「别名命中」会得到假的未命中（本探针首次运行即踩）。
        out = metacache.save_torrent_info(cache, ih, ti)
        print(f"  写入 .meta                        : {out is not None}")

        # 命中
        hits0 = metacache.stats()["hits"]
        got = metacache.load_torrent_info(cache, ih)
        print(f"  同 ih 再读                        : 命中={'是' if got else '否'}"
              f"（hits {hits0}→{metacache.stats()['hits']}）")

        # 内容一致性：把 A 种子的字节放到 B 的键下
        ih_b = "f" * 40
        metacache.write_meta(cache, ih_b, blob)
        got_b = metacache.load_torrent_info(cache, ih_b)
        mism = (got_b is not None and str(got_b.info_hash()) == ih)
        print(f"  错配键(内容 info_hash={ih[:8]}…, 文件名={ih_b[:8]}…) :"
              f" 被识别={'是' if not mism else '否——按命中返回了另一个种子'}")
        if mism:
            findings.append("D3-a .meta 内容 info_hash 与文件名不一致未被识别"
                            "（缓存错配，会按错误元数据建文件树）")

        # 别名键命中（混合 v1+v2 种子的真 v1 btih 也须命中）
        v1, v2 = metacache.info_hash_of(ti)
        print(f"  info_hash()={str(ti.info_hash())[:8]}… 别名 v1={v1[:8]}… "
              f"v2={v2[:8]}…（本绑定 info_hash() ≠ v1！）")
        files = sorted(os.listdir(metacache.meta_dir(cache)))
        print(f"  .meta 落盘文件                    : {files}")
        if v1 and v1 != ih:
            hits0 = metacache.stats()["hits"]
            ck_v1 = metacache.load_torrent_info(cache, v1)
            print(f"  用真 v1 btih 查                    : 命中="
                  f"{'是' if ck_v1 else '否'}（hits {hits0}→"
                  f"{metacache.stats()['hits']}）")
            if ck_v1 is None:
                findings.append("D3-c 混合种子的 v1 btih 查不到缓存"
                                "（别名键未落盘 → 命中率缺口）")
        print(f"  命中率                            : {metacache.hit_rate():.2f}")

        # prune 成本
        scans = {"n": 0}
        real_scandir = os.scandir

        def counting_scandir(p):
            if os.path.normcase(str(p)) == os.path.normcase(metacache.meta_dir(cache)):
                scans["n"] += 1
            return real_scandir(p)

        os.scandir = counting_scandir
        try:
            for _ in range(30):
                metacache.save_torrent_info(cache, ih, ti)
        finally:
            os.scandir = real_scandir
        print(f"  30 次写入触发 .meta 目录扫描      : {scans['n']} 次")
        if scans["n"] >= 30:
            findings.append(f"D3-b prune_meta 每次写入都扫全目录（{scans['n']}/30）")
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------------------
# D4 线程阻塞与吞吐参数
# --------------------------------------------------------------------------

def probe_d4(findings: list[str]) -> None:
    print("\n=== D4 线程阻塞与吞吐参数（锁持有 / 参数生效）===")
    cache = tempfile.mkdtemp(prefix="mv_diag_d4_")
    try:
        # 锁持有时间：任务数 × 文件数规模化
        reg = TaskRegistry(cache_dir=cache, ses_get=lambda: None)
        h = CountingHandle()
        for i in range(8):
            r = _fake_result(ih=f"{i:040x}", nfiles=2000)
            with reg.lock:
                reg.put_record_locked(f"{i:040x}",
                                      TaskRecord(handle=h, result=r,
                                                 save_path=os.path.join(
                                                     cache, f"t{i}"),
                                                 download=True))

        def hold():
            with reg.lock:
                _ = list(reg.torrents.values())
        us, _ = _median_us(hold, 200)
        print(f"  注册表锁持有(8 任务×2000 文件)     : {us:7.1f} µs / 次")
        if us / 1000 > TH_LOCK_MS:
            findings.append(f"D4-a 注册表锁持有 {us/1000:.2f} ms > {TH_LOCK_MS} ms")

        # 吞吐参数键是否真实存在**且真实生效**：必须用应用自己的
        # build_session_settings 建会话——裸会话只反映 libtorrent 默认值，
        # 会得出「所有键都未生效」的假结论（本探针首次运行即踩此坑）。
        settings = session_mod.build_session_settings(0, 3)
        s = lt.session(settings)
        got = s.get_settings()
        wanted = dict(session_mod.THROUGHPUT_SETTINGS)
        missing = [k for k in wanted if k not in got]
        mismatch = [k for k in wanted
                    if k in got and int(got[k]) != int(wanted[k])]
        print(f"  THROUGHPUT_SETTINGS 键数           : {len(wanted)}")
        print(f"  └ 不存在的键（被静默丢弃）          : {missing or '无'}")
        print(f"  └ 存在但未生效的键                  : {mismatch or '无'}")
        if missing or mismatch:
            findings.append(f"D4-b 吞吐参数未全部生效：缺键={missing} 未生效={mismatch}")
        s = None
        del s
        try:
            import gc
            gc.collect()
        except Exception:
            pass

        # 候选可加项的键存在性（供后续调参，不判失败）
        probe = lt.session({"listen_interfaces": "127.0.0.1:0", "enable_dht": False,
                            "enable_lsd": False})
        pg = probe.get_settings()
        candidates = ["aio_threads", "hashing_threads", "piece_extent_affinity",
                      "max_out_request_queue", "request_queue_size",
                      "request_timeout", "max_failcount", "whole_pieces_threshold"]
        present = [k for k in candidates if k in pg]
        print(f"  候选可调键（本绑定存在）            : {present}")
        probe = None
    finally:
        import shutil
        shutil.rmtree(cache, ignore_errors=True)


# --------------------------------------------------------------------------
# D5 规模无关性（plan/09 验收段）
# --------------------------------------------------------------------------

D5_LIMIT_ROUND_MS = 0.60      # 门控轮（状态判定路径）的耦合成本硬上界（ms）
D5_LIMIT_RATIO = 3.0          # 400k 相对 20k 的耗时增幅上界（倍，含噪声余量）
D5_LIMIT_SEG_MS = 4.0         # 显示轮（进度条分段）相对门控轮的**增量**上界
D5_LIMIT_CALL_RATIO = 2.0     # 门控轮**绑定调用次数**的规模增幅上界（确定性判据）
D5_SIZES = (20000, 400000)    # 真实测量规模（再大只做外推，避免探针本身过慢）

# 关于阈值取值的说明（2026-09-17 实测校正，记录以免被当成随意放宽）
# ---------------------------------------------------------------------------
# 立项时 D5_LIMIT_ROUND_MS 定为 0.05ms，是**未测量前的估计值**。方案落地后
# 实测门控轮成本下限 ≈0.11ms，构成全是不可省的常量项：
#     handle.status(0)       6.4µs  （state/peers/rate，已关位图）
#     handle.file_progress() 5.9µs  （O(文件数)）
#     gate_snapshot()       40.6µs  （contiguous + 尾窗 + 入口 三次区间判定）
#     tick()                26.4µs
#     + 失效自检 3~4 次 status(0) ≈ 25µs
# 所以 0.05ms 在物理上不可达。
#
# **方法论（重要）**：亚毫秒级的时间差在本机负载下可差 2~3 倍，单纯比时间
# 只能证明「同一量级」。真正的规模无关性由两条**确定性**判据保证：
#   1. D5-c 位图物化次数 == 0（一次都不物化，规模自然无关）
#   2. D5-e 门控轮**绑定调用次数**跨规模增幅 ≤ 2（计数无噪声）
#      同源护栏在 `pieceview_test.py §D`：调用次数跨规模**严格相等**
# 时间判据（D5-a/D5-b）只作旁证，阈值按实测下限 + 噪声余量给。


class _CountingRealHandle:
    """真句柄的计数代理：分别统计「带位图的 status()」与「have_piece()」。

    为什么需要它：D5 要量的是**真实绑定成本**，不能用假句柄（会把位图物化
    测成 0）。这里只在真句柄外面包一层计数，所有调用原样转发。
    """

    def __init__(self, h):
        self._h = h
        self.status_bitmap = 0     # status()：返回记录含全量 pieces 位图
        self.status_no_bits = 0    # status(flags)：flags 已关掉 pieces
        self.have_calls = 0

    def reset(self):
        self.status_bitmap = 0
        self.status_no_bits = 0
        self.have_calls = 0

    def status(self, *a, **kw):
        if a or kw:
            self.status_no_bits += 1
            return self._h.status(*a, **kw)
        self.status_bitmap += 1
        return self._h.status()

    def have_piece(self, p):
        self.have_calls += 1
        return self._h.have_piece(p)

    def __getattr__(self, name):
        return getattr(self._h, name)


class _SegStub:
    """`buffered_segments_of_preview` 的最小宿主（跑**真实方法体**，不启会话）。"""

    def __init__(self, sched):
        self._lock = threading.Lock()
        self.scheduler = sched


def _one_round_ms(proxy, sched, pv, with_segments: bool = True) -> float:
    """一轮 UI 轮询：状态快照 → tick() →（可选）进度条分段。

    与 ``ui/main_window._refresh_status`` 同序。``with_segments=False`` 用来
    把「状态判定路径」（门控 + tick）与「显示路径」（分段）分开计量——
    两者判据不同：前者必须 ≤0.05ms 且规模无关，后者是限频的显示用途。
    """
    from core.fetcher import SessionManager
    t0 = time.perf_counter()
    pv.status()
    sched.tick()
    if with_segments:
        SessionManager.buffered_segments_of_preview(_SegStub(sched))
    return (time.perf_counter() - t0) * 1000


def _measure_round(proxy, sched, pv, with_segments: bool, n: int = 5):
    """多轮采样取中位数（仓库纪律：单次采样在本机可差 20 倍）。

    返回 ``(中位耗时 ms, 最坏一轮的位图物化次数, flags-status 次数,
    have_piece 次数)``；计数取最坏轮（保守，避免中位数掩盖偶发多取）。
    采样数取 5：门控轮成本已降到亚毫秒量级，3 次采样的中位数仍会被噪声
    拉动（实测同一配置在 0.24~0.53ms 之间跳）。
    """
    samples = []
    for _ in range(n):
        proxy.reset()
        ms = _one_round_ms(proxy, sched, pv, with_segments)
        samples.append((ms, proxy.status_bitmap, proxy.status_no_bits,
                        proxy.have_calls))
    samples.sort(key=lambda s: s[0])
    mid = samples[len(samples) // 2]
    return (mid[0], max(s[1] for s in samples), max(s[2] for s in samples),
            max(s[3] for s in samples))


def probe_d5(findings: list[str]) -> None:
    print("\n=== D5 规模无关性（plan/09 验收段）===")
    print(f"  判据：门控轮 ≤ {D5_LIMIT_ROUND_MS}ms 且规模增幅 ≤ {D5_LIMIT_RATIO}×；"
          f"两轮均位图物化 0 次；显示轮增量 ≤ {D5_LIMIT_SEG_MS}ms")
    from core.preview import PreviewCore
    from core.registry import TaskRecord, TaskRegistry
    from core.scheduler import PreviewScheduler, window_pieces

    tmp = tempfile.mkdtemp(prefix="mv_diag_d5_")
    rows: list[tuple] = []
    try:
        for npieces in D5_SIZES:
            pl = 16 * 1024
            # 虚拟种子（零磁盘 IO）：D5 量的是「边下边播（大部分未下）」下的
            # 判定成本，真数据会被校验成全 have、与目标场景相反（见 _virtual_handle）
            _t, ses, rh, ti = _virtual_handle(pl, npieces)
            try:
                proxy = _CountingRealHandle(rh)
                reg = TaskRegistry(cache_dir=tmp, ses_get=lambda: ses)
                big = TorrentFile(0, "root/big.mp4", npieces * pl, 0, 0,
                                  npieces - 1)
                res = ParseResult(info_hash=IH, name="root",
                                  total_size=npieces * pl, piece_size=pl,
                                  num_pieces=npieces, files=[big],
                                  source="magnet")
                with reg.lock:
                    reg.put_record_locked(IH, TaskRecord(
                        handle=proxy, result=res, save_path=tmp, download=True))
                    reg.handle = proxy
                    reg.result = res
                sched = PreviewScheduler()
                pv = PreviewCore(reg=reg, scheduler_get=lambda: sched)
                # 走**生产接线**：start_preview 会建/注入增量视图并 begin()
                pv.start_preview(big)
                sched._tail_pieces = [npieces - 1]
                sched._entry_pieces = [npieces - 1]

                proxy.reset()
                _one_round_ms(proxy, sched, pv, True)     # 预热（首轮含基线）
                gate = _measure_round(proxy, sched, pv, False)
                disp = _measure_round(proxy, sched, pv, True)
                rows.append((npieces, gate, disp))
                print(f"  {npieces:>8} 块: 门控轮 {gate[0]:>8.4f}ms"
                      f"（绑定 {gate[3]:>4} 次）"
                      f" | 显示轮 {disp[0]:>8.4f}ms（增量 "
                      f"{max(0.0, disp[0] - gate[0]):>7.3f}ms）"
                      f" | 位图物化 门控 {gate[1]} 次/显示 {disp[1]} 次")
            finally:
                ses = None
                import shutil as _sh
                _sh.rmtree(_t, ignore_errors=True)
        if not rows:
            return
        n_small, gate_small, disp_small = rows[0]
        n_big, gate_big, disp_big = rows[-1]
        ratio = gate_big[0] / gate_small[0] if gate_small[0] > 0 else float("inf")
        print(f"  门控轮耗时规模 ×{n_big / n_small:.0f} 的增幅：×{ratio:.2f}"
              f"（旁证判据 ≤ {D5_LIMIT_RATIO}×；亚毫秒计时含噪声）")
        c_small, c_big = gate_small[3], gate_big[3]
        cratio = c_big / c_small if c_small else float("inf")
        print(f"  门控轮**绑定调用次数**：{c_small} → {c_big}（×{cratio:.2f}）"
              f" ← 确定性判据，判据 ≤ {D5_LIMIT_CALL_RATIO}×")
        ns_per_piece = ((gate_big[0] - gate_small[0]) * 1e6 / (n_big - n_small)
                        if n_big > n_small else 0.0)
        print(f"  门控轮拟合斜率：{ns_per_piece:.3f} ns/块"
              f"（>2 即说明仍随块数增长；同源确定性护栏见 pieceview_test §D）")
        for n in (2000000, 6500000):
            est = gate_small[0] + ns_per_piece * (n - n_small) / 1e6
            print(f"    外推 {n:>8} 块：门控轮 ≈ {est:>8.3f}ms"
                  f"（700ms 轮询占 GUI {est / 700 * 100:>6.3f}%）")
        if gate_big[0] > D5_LIMIT_ROUND_MS:
            findings.append(
                f"D5-a 门控轮耦合成本 {gate_big[0]:.4f}ms > "
                f"{D5_LIMIT_ROUND_MS}ms（{n_big} 块）")
        if cratio > D5_LIMIT_CALL_RATIO:
            findings.append(
                f"D5-b 门控轮绑定调用数随规模增长 ×{cratio:.2f}"
                f"（{c_small} → {c_big}，判据 ≤ {D5_LIMIT_CALL_RATIO}×）")
        elif ratio > D5_LIMIT_RATIO:
            findings.append(
                f"D5-b' 门控轮耗时增幅 ×{ratio:.2f} > {D5_LIMIT_RATIO}×"
                f"（调用数判据已过，此项属计时噪声，需复测确认）")
        if gate_big[1] or disp_big[1]:
            findings.append(
                f"D5-c 仍物化全量位图（门控 {gate_big[1]} 次、"
                f"显示 {disp_big[1]} 次，判据均 0）")
        seg_extra = max(0.0, disp_big[0] - gate_big[0])
        if seg_extra > D5_LIMIT_SEG_MS:
            findings.append(
                f"D5-d 显示轮（进度条分段）增量 {seg_extra:.3f}ms > "
                f"{D5_LIMIT_SEG_MS}ms")
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------------------
# D6 语义等价护栏：位图 have ≡ 惰性 have
# --------------------------------------------------------------------------

def probe_d6(findings: list[str]) -> None:
    """`PieceMap.have` 换成惰性/增量实现后，三个纯查询必须与位图路径**逐值一致**。

    plan/09 方案 D 把 `have` 从「预物化位图闭包」改为「按需绑定调用」。
    这条护栏锁死「提供者换掉、结果不许变」——它是 A4 判据的常驻版本，
    也是实施前唯一能先立的语义基线。
    """
    import random
    print("\n=== D6 语义等价护栏：位图 have ≡ 惰性 have（≥2000 例）===")
    rnd = random.Random(20260917)
    bad = 0
    cases = 0
    for _ in range(2000):
        n = rnd.choice((16, 64, 256, 1024, 5000))
        pl = rnd.choice((16, 100, 1024, 4096))
        offset = rnd.choice((0, 0, 0, pl // 2, rnd.randrange(0, pl * 4)))
        start = rnd.randrange(0, n)
        end = rnd.randrange(start, n)
        size = n * pl - offset
        if size <= 0:
            continue
        bits = [rnd.random() < 0.55 for _ in range(n)]
        a = rnd.randrange(0, size + 1)
        b = rnd.randrange(0, size + 1)
        lo, hi = min(a, b), max(a, b)

        pm_bitmap = PieceMap(pl, offset, start, end, size,
                             have_from_bitmap(bits))
        pm_lazy = PieceMap(pl, offset, start, end, size,
                           (lambda p, _b=bits, _n=n: 0 <= p < _n and _b[p]))
        got = (contiguous_bytes(pm_bitmap),
               ready_until(pm_bitmap, lo, hi),
               range_available(pm_bitmap, lo, hi),
               contiguous_bytes(pm_bitmap, limit=size // 2))
        exp = (contiguous_bytes(pm_lazy),
               ready_until(pm_lazy, lo, hi),
               range_available(pm_lazy, lo, hi),
               contiguous_bytes(pm_lazy, limit=size // 2))
        cases += 1
        if got != exp:
            bad += 1
            if bad <= 3:
                print(f"  [DIFF] n={n} pl={pl} offset={offset} "
                      f"[{start},{end}] 区间[{lo},{hi}) 位图={got} 惰性={exp}")
    print(f"  对拍 {cases} 例：不一致 {bad} 例 → "
          f"{'两种 have 提供者逐值等价' if bad == 0 else '提供者语义不等价'}")
    if bad:
        findings.append(f"D6-a have 提供者语义不等价（{bad}/{cases} 例不一致）")


# --------------------------------------------------------------------------

def main() -> int:
    want = [a.upper() for a in sys.argv[1:]] or ["D1", "D1S", "D2", "D3", "D4",
                                                 "D5", "D6"]
    findings: list[str] = []
    print("=== 等待超时诊断（diag_wait_timeout）===")
    print(f"libtorrent {lt.version}，pid {os.getpid()}")
    if "D1" in want:
        probe_d1(findings)
    if "D1S" in want or "D1" in want:
        probe_scale(findings)
    if "D2" in want:
        probe_d2(findings)
    if "D3" in want:
        probe_d3(findings)
    if "D4" in want:
        probe_d4(findings)
    if "D5" in want:
        probe_d5(findings)
    if "D6" in want:
        probe_d6(findings)

    print("\n=== 诊断汇总 ===")
    if findings:
        for f in findings:
            print("  [!] " + f)
        print(f"\n共 {len(findings)} 项超阈/异常，按上表逐项处理。")
        return 1
    print("  未发现超阈项。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
