"""块可用性视图专项验收（pieceview_test，plan/09 方案 D）。

把「判定成本与种子规模无关」这件事做成可复跑的判据，并锁死
「换了 have 提供者，结果逐值不变」这条语义底线。

覆盖：
  §A have 语义：越界 / 非整数 / 绑定异常 → 一律 False（不可判定按不可用）
  §B 对拍：`contiguous_bytes_of`（增量）≡ `contiguous_bytes`（位图）≥2000 例
  §C 起点切换（seek 语义）正确重锚，且结果与位图一致
  §D 摊销：稳态每轮绑定调用只与**新增**连续块数相关，与总块数无关
  §E 失效自检：`total_done` 下降 / `checking_*` → 自动作废基线
  §F 推进触顶（MAX_ADVANCE）→ 重建基线，绝不把截断当结论
  §G `range_ready_pieces` ≡ `range_available`（区间判定对拍）
  §H `buffered_runs`：小块走位图精确；大块走降采样且**不再物化位图**
  §I 稳态零位图物化（核心判据）
  §J 派发层：无 view 时与纯函数逐值一致（不许悄悄改变旧路径）

用法：.\\.venv\\Scripts\\python.exe tests/pieceview_test.py
退出码：0=通过 1=失败 2=SKIP（依赖缺失，绝不假装通过）
"""
from __future__ import annotations

import os
import random
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import libtorrent as lt  # noqa: E402
    from core.models import (PieceMap, contiguous_bytes,  # noqa: E402
                             contiguous_bytes_of, have_from_bitmap,
                             range_available, range_available_of,
                             ready_until, ready_until_of)
    from core.pieceview import (MAX_ADVANCE, SEG_PROBE_BUDGET,  # noqa: E402
                               PieceView)
    from test_support import Checker  # noqa: E402
except Exception as e:          # 依赖缺失：显式 SKIP，绝不假装通过
    print(f"依赖缺失，无法执行 pieceview 专项：{e}")
    sys.exit(2)


# --------------------------------------------------------------------------
# 替身
# --------------------------------------------------------------------------

class FakeViewHandle:
    """可编程句柄替身：have_piece 与 status().pieces **严格一致**。

    另分别统计「带位图的 status()」与「关位图的 status(flags)」次数——
    §I 的核心判据就是前者在稳态必须为 0。
    """

    def __init__(self, have=(), npieces=64, total_done=0, state=None,
                 flags_ok=True):
        self._have = set(int(p) for p in have)
        self._n = int(npieces)
        self.total_done = int(total_done)
        self.state = state
        self.flags_ok = flags_ok
        self.status_all = 0        # status()：含全量 pieces 位图
        self.status_flag = 0       # status(flags)：flags 关闭 pieces
        self.have_calls = 0

    def have_piece(self, p):
        self.have_calls += 1
        return int(p) in self._have

    def status(self, flags=None):
        if flags is not None:
            if not self.flags_ok:
                raise TypeError("本替身不接受 status flags")
            self.status_flag += 1
            return SimpleNamespace(state=self.state,
                                   total_done=self.total_done, pieces=[])
        self.status_all += 1
        return SimpleNamespace(state=self.state,
                               total_done=self.total_done,
                               pieces=[i in self._have for i in range(self._n)])


def _pm(view, pl, offset, sp, ep, size):
    return PieceMap(piece_length=pl, offset=offset, start_piece=sp,
                    end_piece=ep, size=size, have=view.have, view=view)


# --------------------------------------------------------------------------
# §A have 语义
# --------------------------------------------------------------------------

def section_have(ck: Checker) -> None:
    ck.section("§A have 语义：不可判定一律 False")
    h = FakeViewHandle(have=(0, 1, 3), npieces=8)
    v = PieceView(h, 100, 8)
    ck.check(v.have(0) is True and v.have(3) is True, "已落盘块 → True")
    ck.check(v.have(2) is False, "未落盘块 → False")
    ck.check(v.have(-1) is False, "负块号 → False（不可判定按不可用）")
    ck.check(v.have(8) is False and v.have(9999) is False,
             "越界块号 → False（绝不误判可读）")
    ck.check(v.have("x") is False and v.have(None) is False,
             "非整数 → False（不抛）")

    class Boom(FakeViewHandle):
        def have_piece(self, p):
            raise RuntimeError("句柄失效")
    vb = PieceView(Boom(), 100, 8)
    ck.check(vb.have(0) is False, "绑定异常 → False（不冒泡）")

    ck.check(PieceView(FakeViewHandle(), 0, 8).contiguous_from(0) is None,
             "piece_length<=0 → contiguous_from 返回 None（不可判定）")
    ck.check(PieceView(FakeViewHandle(), 100, 0).contiguous_from(0) is None,
             "npieces<=0 → None（不除零）")


# --------------------------------------------------------------------------
# §B 对拍：增量 ≡ 位图
# --------------------------------------------------------------------------

def section_parity(ck: Checker) -> None:
    ck.section("§B 对拍：contiguous_bytes_of（增量）≡ contiguous_bytes（位图）")
    rnd = random.Random(20260917)
    bad = cases = 0
    for _ in range(2500):
        n = rnd.choice((8, 32, 128, 512, 2048))
        pl = rnd.choice((16, 100, 1024, 4096))
        offset = rnd.choice((0, 0, 0, pl // 2, rnd.randrange(0, pl * 3)))
        sp = rnd.randrange(0, n)
        ep = rnd.randrange(sp, n)
        size = n * pl - offset
        if size <= 0:
            continue
        bits = [rnd.random() < 0.6 for _ in range(n)]
        have = {i for i, b in enumerate(bits) if b}
        view = PieceView(FakeViewHandle(have=have, npieces=n), pl, n)
        pm_bitmap = PieceMap(pl, offset, sp, ep, size, have_from_bitmap(bits))
        pm_view = _pm(view, pl, offset, sp, ep, size)
        got = (contiguous_bytes_of(pm_view),
               contiguous_bytes_of(pm_view, limit=max(1, size // 2)))
        exp = (contiguous_bytes(pm_bitmap),
               contiguous_bytes(pm_bitmap, limit=max(1, size // 2)))
        cases += 1
        if got != exp:
            bad += 1
            if bad <= 3:
                print(f"  [DIFF] n={n} pl={pl} off={offset} [{sp},{ep}] "
                      f"size={size} 增量={got} 位图={exp}")
    ck.check(bad == 0, f"对拍 {cases} 例，不一致 {bad} 例（判据 0）")
    ck.check(cases >= 2000, f"对拍样本量 {cases} ≥ 2000")


# --------------------------------------------------------------------------
# §C 起点切换 / seek
# --------------------------------------------------------------------------

def section_reanchor(ck: Checker) -> None:
    ck.section("§C 起点切换（seek 语义）：重锚且结果与位图一致")
    n, pl = 2000, 100
    # 头部连续 1500 块 + 尾部 100 块（尾部不连续）
    have = set(range(1500)) | set(range(1900, n))
    h = FakeViewHandle(have=have, npieces=n)
    v = PieceView(h, pl, n)
    size = n * pl
    pm_bitmap = PieceMap(pl, 0, 0, n - 1, size, have_from_bitmap(
        [i in have for i in range(n)]))
    pm_view = _pm(v, pl, 0, 0, n - 1, size)

    ck.check(v.contiguous_from(0) == 1500, "从 0 起连续前缀 = 1500 块")
    ck.check(v.contiguous_from(1500) == 1500,
             "从 1500 起（首块即缺）→ 立即停在 1500")
    ck.check(v.contiguous_from(1950) == 2000,
             "从 1950 起（落在尾部连续段内）→ 推进到 2000")
    ck.check(v.contiguous_from(0) == 1500, "切回 0 起点仍正确（多锚点隔离）")
    ck.check(contiguous_bytes_of(pm_view) == contiguous_bytes(pm_bitmap),
             "多锚点来回切换后整体结果仍与位图一致")

    # 下载推进（单调性）
    h._have |= set(range(1500, 1600))
    ck.check(v.contiguous_from(0) == 1600,
             "下载推进后连续前缀跟得上（1500 → 1600）")
    ck.check(v.contiguous_from(0) == 1600, "重复查询不退化（锚点缓存生效）")


# --------------------------------------------------------------------------
# §D 摊销：调用次数只与新增块数相关
# --------------------------------------------------------------------------

def _round_calls(n: int, rounds: int = 30, per_round: int = 2) -> int:
    """模拟 ``rounds`` 轮轮询（每轮新增 ``per_round`` 个连续块）的总绑定调用数。"""
    h = FakeViewHandle(have=set(), npieces=n)
    v = PieceView(h, 16 * 1024, n)
    v.rebuild_baseline()
    h.have_calls = 0
    total = 0
    for i in range(rounds):
        h._have |= set(range(i * per_round, (i + 1) * per_round))
        v.contiguous_from(0)
        total += h.have_calls
        h.have_calls = 0
    return total


def section_amortized(ck: Checker) -> None:
    ck.section("§D 摊销：稳态绑定调用数与**种子规模无关**（plan/09 判据 G1）")
    rounds, per_round = 30, 2
    small, big = 4000, 400000          # 规模 ×100
    c_small = _round_calls(small, rounds, per_round)
    c_big = _round_calls(big, rounds, per_round)
    ck.check(c_small == c_big,
             f"{rounds} 轮累计绑定调用：{small} 块 → {c_small} 次，"
             f"{big} 块 → {c_big} 次（必须相等 = 规模无关）")
    ck.check(c_big <= rounds * (per_round + 1),
             f"上界：每轮 ≤ 新增块数 + 1 次确认（实测 {c_big} ≤ "
             f"{rounds * (per_round + 1)}）")

    # 起点变化（seek 到已下载深处）不应触发 O(N) 绑定调用
    n = 400000
    h2 = FakeViewHandle(have=set(range(300000)), npieces=n)
    v2 = PieceView(h2, 16 * 1024, n)
    v2.rebuild_baseline()
    h2.have_calls = 0
    got = v2.contiguous_from(100000)
    ck.check(got == 300000, f"seek 到 100000（深处已下载）→ {got}（应为 300000）")
    ck.check(h2.have_calls <= 2,
             f"该次查询仅 {h2.have_calls} 次绑定调用（靠基线位图给出下限，"
             f"不做 O(30 万) 绑定调用）")


# --------------------------------------------------------------------------
# §E 失效自检
# --------------------------------------------------------------------------

def section_invalidate(ck: Checker) -> None:
    ck.section("§E 失效自检：total_done 下降 / checking_* → 作废基线")
    n = 1000
    h = FakeViewHandle(have=set(range(500)), npieces=n, total_done=500 * 100)
    v = PieceView(h, 100, n)
    ck.check(v.contiguous_from(0) == 500, "初始连续前缀 500")
    ck.check(v.stats()["baseline_count"] == 1, "基线建立 1 次")

    # 重新校验：total_done 下跌
    h.total_done = 0
    h._have = set()
    ck.check(v.contiguous_from(0) == 0,
             "total_done 下降 → 作废基线并重算（连续前缀 500 → 0）")
    ck.check(v.stats()["baseline_count"] == 2, "自动重建基线（第 2 次）")

    # checking_* 状态：**边沿**触发（进入校验态时作废一次），不是电平触发。
    # 电平触发会让「长期处于校验态」的种子每次查询都重建基线 —— 实测反而比
    # 优化前更糟（D5 首次跑生产接线：单轮 7 次物化、8197 次绑定调用）。
    h2 = FakeViewHandle(have=set(range(10)), npieces=n, total_done=1000,
                        state=lt.torrent_status.downloading)
    v2 = PieceView(h2, 100, n)
    v2.contiguous_from(0)
    c0 = v2.stats()["baseline_count"]
    # 状态不变 → 不得重建（抗抖动）
    for _ in range(5):
        v2.contiguous_from(0)
    ck.check(v2.stats()["baseline_count"] == c0,
             f"状态不变时不重复重建基线（{c0} 次保持不变，抗抖动）")
    # 进入校验态 → 作废一次
    h2.state = lt.torrent_status.checking_files
    v2.contiguous_from(0)
    ck.check(v2.stats()["baseline_count"] == c0 + 1,
             f"状态**进入** checking_files → 作废基线一次"
             f"（{c0} → {v2.stats()['baseline_count']}）")
    # 持续停留在校验态 → 不得反复重建
    c1 = v2.stats()["baseline_count"]
    for _ in range(5):
        v2.contiguous_from(0)
    ck.check(v2.stats()["baseline_count"] == c1,
             f"停留校验态不反复重建（{c1} 次保持不变）")

    # 替身不支持 flags：不得因此报错或误判失效
    h3 = FakeViewHandle(have=set(range(10)), npieces=n, flags_ok=False)
    v3 = PieceView(h3, 100, n)
    ck.check(v3.contiguous_from(0) == 10,
             "替身不支持 status(flags) → 退回无参调用，结果正确")

    # 显式失效
    h4 = FakeViewHandle(have=set(range(10)), npieces=n)
    v4 = PieceView(h4, 100, n)
    v4.contiguous_from(0)
    v4.invalidate()
    ck.check(v4.stats()["anchors"] == 0, "invalidate() 清空全部锚点")


# --------------------------------------------------------------------------
# §F 推进触顶
# --------------------------------------------------------------------------

def section_advance_limit(ck: Checker) -> None:
    ck.section("§F 推进触顶（MAX_ADVANCE）：重建基线，绝不截断当结论")
    n = MAX_ADVANCE * 4
    h = FakeViewHandle(have=set(range(n)), npieces=n)
    v = PieceView(h, 100, n)
    # 构造「基线陈旧」：先让基线只看到 0 块，再让实际 have 拉满
    v._baseline = [False] * n            # 故意植入陈旧基线
    v._anchors[0] = 0
    h.have_calls = 0
    got = v.contiguous_from(0)
    ck.check(got == n,
             f"触顶后重建基线并给出真实结论 {got}（应为 {n}，不得截断到 "
             f"{MAX_ADVANCE}）")
    ck.check(v.stats()["baseline_count"] >= 1,
             f"触顶触发了基线重建（baseline_count="
             f"{v.stats()['baseline_count']}）")


# --------------------------------------------------------------------------
# §G 区间判定对拍
# --------------------------------------------------------------------------

def section_range_parity(ck: Checker) -> None:
    ck.section("§G range_ready_pieces ≡ range_available（区间判定对拍）")
    rnd = random.Random(4242)
    bad = cases = 0
    for _ in range(1500):
        n = rnd.choice((16, 64, 256, 1024))
        pl = rnd.choice((16, 100, 1024))
        offset = rnd.choice((0, 0, pl // 2, rnd.randrange(0, pl * 2)))
        sp = rnd.randrange(0, n)
        ep = rnd.randrange(sp, n)
        size = n * pl - offset
        if size <= 0:
            continue
        have = {i for i in range(n) if rnd.random() < 0.7}
        bits = [i in have for i in range(n)]
        view = PieceView(FakeViewHandle(have=have, npieces=n), pl, n)
        pm_bitmap = PieceMap(pl, offset, sp, ep, size, have_from_bitmap(bits))
        pm_view = _pm(view, pl, offset, sp, ep, size)
        a = rnd.randrange(0, size + 1)
        b = rnd.randrange(0, size + 1)
        lo, hi = min(a, b), max(a, b)
        got = (range_available_of(pm_view, lo, hi),
               ready_until_of(pm_view, lo, hi))
        exp = (range_available(pm_bitmap, lo, hi),
               ready_until(pm_bitmap, lo, hi))
        cases += 1
        if got != exp:
            bad += 1
            if bad <= 3:
                print(f"  [DIFF] n={n} pl={pl} off={offset} [{sp},{ep}] "
                      f"区间[{lo},{hi}) 增量={got} 位图={exp}")
    ck.check(bad == 0, f"区间对拍 {cases} 例，不一致 {bad} 例（判据 0）")

    # 边界条件与纯函数一致
    view = PieceView(FakeViewHandle(have=range(8), npieces=8), 100, 8)
    pm = _pm(view, 100, 0, 0, 7, 800)
    ck.check(range_available_of(pm, 5, 5) is True, "空区间 → True")
    ck.check(range_available_of(pm, 0, 801) is False, "越界末端 → False")
    ck.check(ready_until_of(pm, 10, 5) == 10, "start>=end → 返回 start")
    ck.check(ready_until_of(pm, 0, 900) == 0, "end_excl>size → 返回 start")
    pm0 = _pm(view, 0, 0, 0, 7, 800)
    ck.check(range_available_of(pm0, 0, 10) is False, "piece_length<=0 → False")


# --------------------------------------------------------------------------
# §H 进度条分段
# --------------------------------------------------------------------------

def section_buffered(ck: Checker) -> None:
    ck.section("§H buffered_runs：预算内精确 / 超预算降采样，且**始终实时**")
    # 区间块数 ≤ 预算 → k=1，逐块精确（与原实现同结果），且**不依赖缓存基线**
    n = SEG_PROBE_BUDGET
    have = set(range(100)) | set(range(150, 200))
    h = FakeViewHandle(have=have, npieces=n)
    v = PieceView(h, 100, n)
    v.rebuild_baseline()          # 故意先建一份基线，再改 have 验证实时性
    h._have |= set(range(220, 240))
    runs = v.buffered_runs(0, n - 1)
    ck.check(runs == [(0, 99), (150, 199), (220, 239)],
             f"预算内逐块精确，且反映**开播后**新下载的块（{runs}）")

    # 超预算 → 降采样；仍实时、零位图物化、调用数封顶
    big = 200000
    hb = FakeViewHandle(have=set(range(90000)), npieces=big)
    vb = PieceView(hb, 100, big)
    vb.rebuild_baseline()
    hb.status_all = 0
    hb.have_calls = 0
    runs_b = vb.buffered_runs(0, big - 1)
    ck.check(hb.status_all == 0,
             f"大块分段零位图物化（status() 调用 {hb.status_all} 次）")
    ck.check(hb.have_calls <= SEG_PROBE_BUDGET + 1,
             f"降采样探测 {hb.have_calls} 次 ≤ 预算 {SEG_PROBE_BUDGET}")
    ck.check(bool(runs_b) and runs_b[0][0] == 0,
             f"降采样仍给出首段（{runs_b[:2]}）")
    ck.check(big > SEG_PROBE_BUDGET, "该用例走降采样路径")

    # 实时性：缓存基线故意留成空，也不得影响结果（显示用途不能吃陈旧数据）。
    # 区间取 ≤ 预算以便 k=1（逐块精确），从而能断言精确的段边界。
    hc = FakeViewHandle(have=set(range(100)), npieces=200)
    vc = PieceView(hc, 100, 200)
    vc._baseline = [False] * 200       # 植入陈旧空基线
    ck.check(vc.buffered_runs(0, 199) == [(0, 99)],
             "显示分段不吃陈旧基线（实测踩过：否则进度条永远停在开播那刻）")

    ck.check(v.buffered_runs(10, 5) is None, "sa>sb → None（不抛）")
    ck.check(PieceView(FakeViewHandle(), 0, 8).buffered_runs(0, 7) is None,
             "piece_length<=0 → None")


# --------------------------------------------------------------------------
# §I 稳态零位图物化
# --------------------------------------------------------------------------

def section_no_materialize(ck: Checker) -> None:
    ck.section("§I 稳态零位图物化（核心判据）")
    n = 400000
    h = FakeViewHandle(have=set(range(1000)), npieces=n)
    v = PieceView(h, 16 * 1024, n)
    v.contiguous_from(0)                      # 首次 → 建立基线（允许 1 次）
    first = h.status_all
    ck.check(first == 1, f"首次查询物化位图 {first} 次（一次基线，允许）")
    h.status_all = 0
    h.have_calls = 0
    for i in range(50):                       # 50 轮稳态轮询
        v.contiguous_from(0)
        v.contiguous_from(1000)
    ck.check(h.status_all == 0,
             f"50 轮稳态零位图物化（status() 调用 {h.status_all} 次，判据 0）")
    # 每查询恰 1 次「确认锚点处仍缺失」的绑定调用——**不可省**（必须验证状态
    # 未变，否则连续前缀可能悄悄失效）。真正的判据是它与种子规模**无关**。
    ck.check(h.have_calls == 100,
             f"50 轮 × 2 次查询 = {h.have_calls} 次绑定调用"
             f"（每查询恰 1 次确认调用，无重复扫描）")
    h_small = FakeViewHandle(have=set(range(1000)), npieces=40000)
    v_small = PieceView(h_small, 16 * 1024, 40000)
    v_small.contiguous_from(0)
    v_small.contiguous_from(1000)
    h_small.have_calls = 0
    for _ in range(50):
        v_small.contiguous_from(0)
        v_small.contiguous_from(1000)
    ck.check(h_small.have_calls == h.have_calls,
             f"规模 ×10（4 万 vs 40 万块）绑定调用数完全相同"
             f"（{h_small.have_calls} vs {h.have_calls}）→ 规模无关成立")
    ck.check(h.status_flag >= 50,
             f"失效自检走关位图的 status(flags)（{h.status_flag} 次，恒定成本）")


# --------------------------------------------------------------------------
# §J 派发层向后兼容
# --------------------------------------------------------------------------

def section_dispatch(ck: Checker) -> None:
    ck.section("§J 派发层：无 view 时与纯函数逐值一致")
    rnd = random.Random(777)
    bad = 0
    for _ in range(500):
        n = rnd.choice((16, 64, 256, 1024))
        pl = rnd.choice((16, 100, 1024))
        offset = rnd.choice((0, 0, pl // 2))
        sp = rnd.randrange(0, n)
        ep = rnd.randrange(sp, n)
        size = n * pl - offset
        bits = [rnd.random() < 0.6 for _ in range(n)]
        pm = PieceMap(pl, offset, sp, ep, size, have_from_bitmap(bits))
        a = rnd.randrange(0, size + 1)
        b = rnd.randrange(0, size + 1)
        lo, hi = min(a, b), max(a, b)
        if (contiguous_bytes_of(pm), ready_until_of(pm, lo, hi),
                range_available_of(pm, lo, hi)) != \
           (contiguous_bytes(pm), ready_until(pm, lo, hi),
                range_available(pm, lo, hi)):
            bad += 1
    ck.check(bad == 0, f"无 view 的派发与纯函数逐值一致（不一致 {bad} 例）")
    ck.check(PieceMap(100, 0, 0, 7, 800, lambda p: True).view is None,
             "PieceMap.view 默认 None（旧构造方式不受影响）")


# --------------------------------------------------------------------------

def main() -> int:
    try:                    # 行缓冲：被超时杀掉也不丢已产出的结论（便于定位）
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:
        pass
    ck = Checker("pieceview_test（块可用性视图专项，plan/09 方案 D）")
    print("\n--- 块可用性视图验收 ---")
    section_have(ck)
    section_parity(ck)
    section_reanchor(ck)
    section_amortized(ck)
    section_invalidate(ck)
    section_advance_limit(ck)
    section_range_parity(ck)
    section_buffered(ck)
    section_no_materialize(ck)
    section_dispatch(ck)
    return ck.report()


if __name__ == "__main__":
    sys.exit(main())
