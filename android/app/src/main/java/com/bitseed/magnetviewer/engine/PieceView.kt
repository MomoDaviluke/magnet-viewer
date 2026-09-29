package com.bitseed.magnetviewer.engine

import com.bitseed.magnetviewer.core.model.IncrementalAvailability
import com.bitseed.magnetviewer.data.AppLog

/**
 * 块可用性**增量视图**（S6；桌面 `core/pieceview.py` plan/09 方案 D 的逐条移植）。
 *
 * 解决的根因：安卓等待轮询（50ms/轮）与门控每轮都走 `status(QUERY_PIECES)`
 * 重建快照——libtorrent C++ 层每次拷贝**全量**位图（O(总块数)），再叠加
 * 「从起点扫连续前缀」的 O(前缀) 逐块 JNI。桌面实测该成本 27.1ms/轮
 * （40 万块），本视图把它降到**摊销 O(1)**、与种子规模无关。
 *
 * 依赖的唯一不变量：**块一旦落盘就恒为 have**（块不会变回未下载），
 * 因此「从 start 起的连续前缀」单调不减——这是锚点增量成立的全部前提。
 * 破坏它的三种场景（删除文件 / 重新校验 / 句柄换代）各有 O(1) 观测信号：
 *
 * - 失效**电平**：`totalDone` 比上次观测**下降** → 有块退回 not-have；
 * - 失效**边沿**：状态**进入** checking（CHECKING_FILES / CHECKING_RESUME_DATA）
 *   → 即将大规模变动 have。边沿触发是刻意设计：电平触发下「长期停在
 *   checking_resume_data」的种子会在每轮都重建基线，比优化前更糟
 *   （桌面探针 D5 首跑生产接线即暴露过，单轮 7 次物化）；校验期间真正
 *   丢块的情况由电平判据兜住。
 *
 * 结构（与桌面逐条对齐，保证跨端行为可对拍）：
 * - **一次性基线**：物化一次位图（BooleanArray），只作「新起点的免费下限」，
 *   之后**再也不物化**（失效重建除外）；基线陈旧只多走几次实时探测，
 *   不影响正确性（`contiguousFrom` 的结论永远实时）。
 * - **锚点缓存**：`anchors[start] = 末端`。新起点先用基线拿到已知下限，
 *   再用实时单查推进（首缺即停）；老锚点从上次末端继续。稳态每轮新增 0~3 块。
 * - **推进触顶**：单次推进超过 [MAX_ADVANCE] 块 = 基线已陈旧（句柄被换过 /
 *   未显式失效）→ 强制重建基线并直接给结论，**绝不把截断当成「就绪到此
 *   为止」**（那会把可读误判为不可读，比多一次基线更糟）。
 *
 * 失败策略：任何探针异常都不向上抛（真实现已吞成 false/null）；
 * `contiguousFrom` 返回 null = **不可判定**，调用方必须保守处理，
 * 绝不当成「已就绪」。流服务/门控侧在视图不可用时**退回旧路径**
 * （缓存只许加速，不许致败）。
 *
 * 并发：等待轮询的多个 HTTP 线程可能并发进入；全部状态变更收进
 * [lock]（`*_locked` 纪律与桌面同款——锁内绝不调自持锁的公开方法）。
 * 锁内会执行探针的 JNI 单查（~100ns/次，推进循环最坏 [MAX_ADVANCE] 次
 * ≈ 0.4ms）与基线物化（一次性 O(块数)）；探针真实现绝不回调本视图，
 * 锁序单向（scheduler.lock → PieceView.lock）无环。
 */
class PieceView(private val probe: PieceProbe, val npieces: Int) : IncrementalAvailability {

  /** 引擎探针窄接口：JVM 单测用纯 Kotlin 替身实现（swig 类型测试里造不出来）。 */
  interface PieceProbe {
    /** 视图换代身份：句柄被替换后 identity 变化 → 调用方必须重建视图（防串台）。 */
    val identity: Any

    /** 单块真查（对应 `have_piece`；实现方必须带 isValid 守卫，异常按 false）。 */
    fun have(piece: Int): Boolean

    /** 失效自检标量；null = 不可判定（本轮跳过自检，绝不误判为失效）。 */
    fun scalars(): Scalars?

    /** 一次性基线位图快照（`status(QUERY_PIECES)`）；null = 物化失败 → 全 False 保守。 */
    fun baseline(): Baseline?
  }

  /** 失效自检标量（[checking] 由真实现按 State ∈ {CHECKING_FILES, CHECKING_RESUME_DATA} 映射）。 */
  class Scalars(val totalDone: Long, val checking: Boolean)

  /** 基线位图（真实现 = TorrentPieceSnapshot 同源快照）。 */
  interface Baseline {
    val size: Int
    fun have(piece: Int): Boolean
  }

  companion object {
    /**
     * 单次「用实时单查推进连续前缀」的块数上限（桌面同款 4096）。
     * 稳态下每轮只推进 0~3 块；触顶说明基线异常，作废锚点并强制重建基线。
     */
    const val MAX_ADVANCE = 4096
  }

  private val lock = Any()

  /** 一次性基线位图；null = 尚未建立 / 已失效待重建。 */
  private var baseline: BooleanArray? = null

  /** 诊断：基线重建次数（供测试断言「失效只重建一次」）。 */
  var baselineCount = 0
    private set

  /** 各起点的连续前缀末端缓存（块号，开区间末端）；键 = 起点块号。 */
  private val anchors = HashMap<Int, Int>()

  private var lastTotalDone = -1L

  /** 上次观测是否处于 checking（边沿判据用；首次观测前视为「没有上次」）。 */
  private var seenState = false
  private var lastChecking = false

  /** 视图换代身份（透传探针）。 */
  val identity: Any get() = probe.identity

  /** 是否就是该探针的视图（句柄被替换后调用方必须重建，防串台）。 */
  fun matches(p: PieceProbe): Boolean = p.identity == identity

  // ---------- 基线与失效 ----------

  /** 作废基线与全部锚点（begin / seek / 句柄换代后由调用方显式触发）。 */
  fun invalidate() {
    synchronized(lock) {
      baseline = null
      anchors.clear()
      lastTotalDone = -1L
      seenState = false
      lastChecking = false
    }
  }

  /**
   * O(1) 失效自检（**须持锁**）。标量不可判定时跳过——宁可依赖显式
   * [invalidate]，也绝不把「查不到状态」误判成「必须失效」。
   */
  private fun checkValidLocked() {
    val sc = probe.scalars() ?: return
    var bad = false
    if (lastTotalDone >= 0 && sc.totalDone < lastTotalDone) {
      // 电平判据：total_done 下降 = 有块从 have 退回 not-have
      bad = true
      AppLog.warn("pieceview.total_done_shrank: $lastTotalDone -> ${sc.totalDone}，作废基线")
    }
    lastTotalDone = sc.totalDone
    // 边沿判据：上次状态可见 && 上次非 checking && 现在进入 checking。
    // 「长期停在 checking」不会反复触发（lastChecking 已置真），校验期间
    // 真正的丢块由上面的电平判据兜住。
    if (seenState && !lastChecking && sc.checking) bad = true
    seenState = true
    lastChecking = sc.checking
    if (bad) {
      baseline = null
      anchors.clear()
    }
  }

  /** 基线物化（**须持锁**）。已有有效基线时是幂等空操作。 */
  private fun rebuildBaselineLocked(): Boolean {
    if (baseline != null) return false
    val b = probe.baseline()
    baseline =
      if (b != null && b.size >= npieces) {
        BooleanArray(npieces) { i ->
          try {
            b.have(i)
          } catch (e: Throwable) {
            false
          }
        }
      } else {
        BooleanArray(npieces) // 全 False 保守（位图不可用 = 不可判定按不可用处理）
      }
    baselineCount++
    anchors.clear()
    checkValidLocked()
    return true
  }

  // ---------- 查询 ----------

  /**
   * 单块是否已完整落盘：**透传真查**（对齐桌面 `have()` 语义——实时值，
   * 不做视图内缓存；锚点是唯一的复用机制）。越界 / 异常由探针按 false 处理。
   */
  fun have(piece: Int): Boolean = probe.have(piece)

  /** 用基线位图求 `[start, end)` 内的连续前缀末端（免费下限，内存查表）。 */
  private fun baselineRunLocked(start: Int, end: Int): Int {
    val bits = baseline ?: return start
    var p = start
    while (p < end && p < bits.size && bits[p]) p++
    return p
  }

  /**
   * 从 `start` 起**连续**已落盘的块末端（第一个未 have 的块号，开区间）；
   * 返回 null = 不可判定（分块非法 / 推进触顶后重建失败）。调用方必须保守处理。
   */
  override fun contiguousFrom(start: Int, limit: Int): Int? {
    if (npieces <= 0) return null
    val s = maxOf(0, start)
    val end = minOf(npieces, limit)
    synchronized(lock) {
      checkValidLocked()
      if (baseline == null) rebuildBaselineLocked()
      var anchor = anchors[s]
      if (anchor == null) {
        // 新起点：先用基线位图拿「已知下限」（基线陈旧只会多走几次实时探测，
        // 绝不影响正确性——结论永远来自实时单查），再增量推进。
        anchor = baselineRunLocked(s, end)
        anchors[s] = anchor
      }
      var p = maxOf(s, minOf(anchor, end))
      var steps = 0
      while (p < end) {
        if (!probe.have(p)) break
        p++
        steps++
        if (steps > MAX_ADVANCE) {
          // 触顶 = 基线已陈旧：强制重建并直接给结论，绝不把截断当「就绪到此为止」
          AppLog.warn("pieceview.advance_limited: 连续前缀推进超过 $MAX_ADVANCE 块，重建基线")
          baseline = null
          rebuildBaselineLocked()
          val a2 = baselineRunLocked(s, end)
          anchors[s] = a2
          return a2
        }
      }
      anchors[s] = p
      return p
    }
  }

  /**
   * 块号闭区间 `[p0, p1]` 是否全部 have。返回 null = 不可判定（保守处理）。
   * 经 `contiguousFrom` 首缺即停 + 锚点缓存 → 跨调用摊销 O(1)。
   */
  override fun rangeReadyPieces(p0: Int, p1: Int): Boolean? {
    if (p1 < p0) return true
    val contig = contiguousFrom(p0, p1 + 1) ?: return null
    return contig > p1
  }
}
