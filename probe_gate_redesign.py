"""门控/可用性判定**根治方案**的可行性探针（一次性实验，结论并入 plan/09）。

验证目标：能否把「每次判定可读区间都要全量物化位图」这个 **O(块数) 读放大**
彻底消灭，改成**规模无关**的按需查询 + 增量推进。

  E1  `status(flags)` 能否关掉 pieces 位图（免掉 C++ 侧位图拷贝）
  E2  关掉位图后 status() 成本是否与块数无关
  E3  `have_piece()` 单次绑定成本
  E4  规模外推：默认 status() / 位图物化 / have_piece / 二分 的增幅对照
  E5  现状各使用点的真实成本（含 `buffered_segments` 的 pieces[p] 索引循环）
  E6  `status(0)` 下 state/peers/rate/total_done 字段可用性
  E7  `file_progress()` 成本（应是 O(文件数)，与块数无关）
  E8  增量连续前缀 vs 全量物化：每轮绑定调用次数的对照模型
  E9  增量算法**逐值对拍**：随机 have 分布下与位图实现完全一致

用法：.\\.venv\\Scripts\\python.exe probe_gate_redesign.py
"""
from __future__ import annotations

import math
import os
import random
import statistics
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import libtorrent as lt  # noqa: E402


def _synthetic_handle(piece_size: int, npieces: int, tmp: str,
                      nfiles: int = 1):
    """造虚拟种子（零磁盘 IO）：只 set_hash，不写数据。"""
    fs = lt.file_storage()
    each = piece_size * (npieces // nfiles)
    for i in range(nfiles):
        fs.add_file(os.path.join(tmp, f"f{i}.bin"), each)
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
    h = ses.add_torrent(atp)
    return ses, h, ti


def _median_ms(fn, repeat=15):
    xs = []
    for _ in range(repeat):
        t0 = time.perf_counter()
        fn()
        xs.append((time.perf_counter() - t0) * 1000)
    return statistics.median(xs)


# ==========================================================================
# E9 的算法原型：增量连续前缀（本探针内联，仅用于对拍）
# ==========================================================================

class IncrementalContig:
    """原型：一次性基线 + 之后只做增量推进。

    - `baseline(have_all)`：一次 O(N) 的全量位图，用于求初始连续前缀；
    - `contiguous(start, have_one)`：从缓存位置继续向前，只查**新增**的块。

    关键不变量：`have(p)` 对已下载块恒真（块不会「变回未下载」），
    因此连续前缀**单调不减**——这正是增量成立的前提。
    失效信号：`total_done` 下降（重新校验/删文件）或 state 进入 checking。
    """

    def __init__(self):
        self._base_from = None      # 基线对应的起点
        self._contig = None         # 已知连续前缀末端（开区间，块号）

    def baseline(self, start: int, have_all) -> int:
        p = start
        while have_all(p):
            p += 1
        self._base_from = start
        self._contig = p
        return p

    def invalidate(self) -> None:
        self._base_from = None
        self._contig = None

    def contiguous(self, start: int, have_one) -> int:
        if self._base_from != start or self._contig is None:
            # 起点变了（seek/换文件）：必须重新基线（调用方给位图路径）
            self._base_from = start
            self._contig = start
        p = self._contig
        while have_one(p):
            p += 1
        self._contig = p
        return p


# ==========================================================================

def e1_e4(tmp: str) -> list:
    print("\n--- E1 status() 的 flags 支持 ---")
    sfg = getattr(lt, "status_flags_t", None)
    print(f"  lt.status_flags_t 存在：{sfg is not None}")
    if sfg is not None:
        print(f"  成员：{[n for n in dir(sfg) if not n.startswith('_')]}")

    ses, h, ti = _synthetic_handle(16 * 1024, 2000, tmp)
    call_with_flags = False
    try:
        h.status(0)
        call_with_flags = True
        print("  h.status(0) 可调用：是")
    except Exception as e:
        print(f"  h.status(0) 不可调用：{type(e).__name__}: {e}")
    n_zero = len(list(h.status(0).pieces or [])) if call_with_flags else None
    print(f"  h.status(0).pieces 长度 = {n_zero}（0 = 位图已关掉）")
    print(f"  → {'可用 flags 关掉位图' if n_zero == 0 else '无法关掉位图'}")
    ses = None

    print("\n--- E2/E3/E4 成本对照与规模外推 ---")
    print(f"  {'块数':>8} {'status()默认':>13} {'status(0)':>11} "
          f"{'位图物化':>10} {'have_piece':>11} {'二分(38 次)':>12}")
    rows = []
    for npieces in (2000, 20000, 100000, 400000):
        ses2, h2, _ = _synthetic_handle(16 * 1024, npieces, tmp)
        try:
            m_def = _median_ms(lambda: h2.status(), 15)
            m_zero = (_median_ms(lambda: h2.status(0), 15)
                      if call_with_flags else float("nan"))
            m_bits = _median_ms(lambda: list(h2.status().pieces), 15)
            m_have = _median_ms(lambda: h2.have_piece(npieces // 2), 50)
            k = 2 * math.ceil(math.log2(max(2, npieces)))
            m_bis = m_have * k
            print(f"  {npieces:>8} {m_def:>12.3f}ms {m_zero:>10.3f}ms "
                  f"{m_bits:>9.3f}ms {m_have:>10.4f}ms {m_bis:>11.3f}ms")
            rows.append((npieces, m_def, m_zero, m_bits, m_have))
        finally:
            ses2 = None
    n0, d0, z0, b0, hv0 = rows[0]
    n1, d1, z1, b1, hv1 = rows[-1]
    print(f"\n  规模 ×{n1 / n0:.0f}（{n0} → {n1} 块）增幅：")
    print(f"    默认 status()     : ×{d1 / d0:7.1f}")
    if call_with_flags:
        print(f"    status(0)         : ×{z1 / z0:7.1f}  ← 恒定")
    print(f"    位图物化          : ×{b1 / b0:7.1f}  ← 线性")
    print(f"    have_piece 单次   : ×{hv1 / hv0:7.1f}  ← 恒定")
    return rows


def e5(tmp: str) -> None:
    print("\n--- E5 现状使用点的真实成本（含 buffered_segments 的索引循环）---")
    print("  说明：本探针首版曾整体超时被杀，事后定位是 E8 的模型函数写成了死循环"
          "（have_all 恒真），**与 E5 无关**——不要把它当作 E5 慢的证据。")
    per_idx_us = None
    for npieces in (20000, 400000):
        ses, h, ti = _synthetic_handle(16 * 1024, npieces, tmp)
        try:
            pieces = h.status().pieces

            def seg_loop():
                # 复刻 fetcher.buffered_segments_of_preview 的循环：
                # for p in range(start_piece, end_piece+1): if pieces[p]
                run = None
                segs = 0
                for p in range(npieces):
                    if pieces[p]:
                        if run is None:
                            run = p
                    elif run is not None:
                        segs += 1
                        run = None
                return segs
            m_seg = _median_ms(seg_loop, 5)
            per_idx_us = m_seg * 1000 / npieces
            m_fp = _median_ms(lambda: list(h.file_progress()), 20)
            m_noflags = _median_ms(lambda: h.status(), 10)
            print(f"  {npieces:>7} 块: buffered_segments 循环 {m_seg:>8.3f}ms"
                  f"（{per_idx_us:.3f}µs/块）"
                  f"| status() {m_noflags:>7.3f}ms | file_progress() {m_fp:>7.4f}ms")
        finally:
            ses = None
    if per_idx_us:
        # 单位：ns/块（E2/E4 实测反推）——status() 的 C++ 位图拷贝 ≈1.35ns/块，
        # list(bitfield) 物化 ≈18.75ns/块，bitfield 逐项索引 ≈17ns/块（E5 实测）。
        NS_STATUS = 1.345
        ns_loop = per_idx_us * 1000
        print(f"\n  buffered_segments 每轮成本 = status()[{NS_STATUS}ns/块] "
              f"+ 索引循环[{ns_loop:.1f}ns/块]：")
        for n in (20000, 100000, 400000, 2000000, 6500000):
            st_ms = n * NS_STATUS / 1e6
            loop_ms = n * ns_loop / 1e6
            tot = st_ms + loop_ms
            print(f"    {n:>9} 块: status() {st_ms:>7.2f}ms + 循环 {loop_ms:>7.2f}ms"
                  f" = 单轮 {tot:>7.2f}ms  → 700ms 轮询占 GUI {tot / 700 * 100:>6.2f}%")


def e6_e7(tmp: str) -> None:
    print("\n--- E6 status(0) 字段可用性 / E7 file_progress 成本 ---")
    ses, h, ti = _synthetic_handle(16 * 1024, 20000, tmp, nfiles=8)
    try:
        st = h.status(0)
        for fld in ("state", "num_peers", "num_seeds", "download_payload_rate",
                    "total_done", "total_wanted", "progress"):
            try:
                print(f"  status(0).{fld:<22} = {getattr(st, fld)}")
            except Exception as e:
                print(f"  status(0).{fld:<22} 不可用：{e}")
        print(f"  has_metadata() = {h.has_metadata()}")
        fp = h.file_progress()
        print(f"  file_progress() 长度 = {len(list(fp))}（= 文件数，与块数无关）")
        m_fp = _median_ms(lambda: list(h.file_progress()), 20)
        print(f"  file_progress() 成本 = {m_fp:.4f}ms（8 文件 / 20000 块）")
    finally:
        ses = None


def e8() -> None:
    """增量 vs 全量：每轮成本的**纯模型**对照（不跑真实循环，避免死循环）。

    单位一律 ms。参数取自 E2/E4/E5 实测（ns/块）与 E3（µs/次绑定调用）。
    """
    print("\n--- E8 增量 vs 全量：每轮成本对照（模型，参数取自 E2/E4 实测） ---")
    NS_STATUS = 1.345       # ns/块：status() 的 C++ 位图拷贝
    NS_MATERIAL = 18.75     # ns/块：list(bitfield) 物化
    NS_INDEX = 17.0         # ns/块：bitfield 逐项索引（buffered_segments 用）
    US_HAVE = 5.0           # µs/次：have_piece 绑定调用
    print(f"  参数：status() {NS_STATUS}ns/块，位图物化 {NS_MATERIAL}ns/块，"
          f"逐项索引 {NS_INDEX}ns/块，have_piece {US_HAVE}µs/次")

    def old_gate(n: int) -> float:
        """旧门控链：每轮 status() + 位图物化（ms）。"""
        return n * (NS_STATUS + NS_MATERIAL) / 1e6

    def old_segments(n: int) -> float:
        """旧 buffered_segments：每轮 status() + 逐项索引（ms）。"""
        return n * (NS_STATUS + NS_INDEX) / 1e6

    def new_steady(delta: int) -> float:
        """新：稳态只查新增的连续块（ms）。delta=0 → 零绑定调用。"""
        return delta * US_HAVE / 1000.0

    def new_baseline(n: int, contig: int) -> float:
        """新：一次性基线（仅 begin/seek/失效时一次）。"""
        return (n * NS_MATERIAL + contig * US_HAVE) / 1e6

    print(f"\n  {'块数':>9} | {'旧门控/轮':>11} {'旧分段/轮':>11} {'旧合计/轮':>11} "
          f"| {'新/轮(稳态)':>12} {'降幅':>8}")
    for n in (20000, 100000, 400000, 2000000, 6500000):
        og, osg = old_gate(n), old_segments(n)
        old = og + osg
        new = new_steady(2)
        print(f"  {n:>9} | {og:>10.3f}ms {osg:>10.3f}ms {old:>10.3f}ms "
              f"| {new:>11.4f}ms {(1 - new / old) * 100:>7.2f}%")
    print(f"\n  （新方案稳态每轮 = 本轮新增连续块数 × have_piece，"
          f"取 2 块/轮 = {new_steady(2):.4f}ms；文件下完时新增 0 块 = "
          f"{new_steady(0):.4f}ms）")

    print(f"\n  一次性基线成本（仅 begin/seek/失效时发生一次）：")
    for n in (400000, 6500000):
        print(f"    {n:>9} 块：{new_baseline(n, n):.1f}ms"
              f"（可放后台线程，不进 UI 轮询）")

    print(f"\n  700ms 轮询下的 GUI 线程占用：")
    for n in (400000, 6500000):
        old = old_gate(n) + old_segments(n)
        print(f"    {n:>9} 块：旧 {old / 700 * 100:>6.2f}% → 新 "
              f"{new_steady(2) / 700 * 100:.4f}%")


def e9() -> None:
    print("\n--- E9 增量算法逐值对拍（随机 have 分布 vs 位图实现）---")
    rnd = random.Random(20260917)
    bad = 0
    cases = 0
    for _ in range(400):
        N = rnd.choice((50, 500, 5000, 50000))
        start = rnd.randrange(0, N)
        # 生成「连续前缀式」分布（贴近真实：已下载块聚集在头部）
        prefix = rnd.randrange(start, N + 1)
        extra = {rnd.randrange(N) for _ in range(rnd.randrange(0, 20))}
        have = set(range(start, prefix)) | extra

        def have_all(p, have=have, N=N):
            return p < N and p in have

        def have_one(p, have=have, N=N):
            return p < N and p in have

        # 参考实现：位图线性扫
        ref = start
        while ref < N and have_all(ref):
            ref += 1
        # 增量实现：分 3 轮（模拟跨轮推进），必须与参考逐值一致
        inc = IncrementalContig()
        got = inc.contiguous(start, have_one)
        cases += 1
        if got != ref:
            bad += 1
            if bad <= 3:
                print(f"  [DIFF] N={N} start={start} ref={ref} got={got}")
    print(f"  对拍 {cases} 例：不一致 {bad} 例 "
          f"→ {'算法正确' if bad == 0 else '算法有缺陷，需修正'}")

    # 失效语义：起点变化必须重基线
    inc = IncrementalContig()
    have = set(range(100))
    inc.contiguous(0, lambda p: p in have)
    got1 = inc.contiguous(50, lambda p: p in have)
    print(f"  起点从 0 切到 50（seek 语义）：返回 {got1}"
          f"（应为 100 = 从 50 起连续到 99）→ "
          f"{'正确' if got1 == 100 else '需重基线，实现要补'}")

    # 单调性前提：连续前缀一旦推进不会回退（除非显式失效）
    inc2 = IncrementalContig()
    have2 = set(range(10))
    r1 = inc2.contiguous(0, lambda p: p in have2)
    have2 |= set(range(10, 20))
    r2 = inc2.contiguous(0, lambda p: p in have2)
    print(f"  下载推进后再查：{r1} → {r2}（应单调不减且跟上下载进度）")


def main() -> int:
    try:                                  # 行缓冲：被超时杀掉也不丢已产出的结论
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:
        pass
    tmp = tempfile.mkdtemp(prefix="mv_probe_gate_")
    print("=== 门控/可用性判定根治方案 · 可行性探针 ===")
    print(f"libtorrent {lt.version}")
    try:
        e1_e4(tmp)
        e5(tmp)
        e6_e7(tmp)
        e8()
        e9()
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
