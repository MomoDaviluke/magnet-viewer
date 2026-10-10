package com.bitseed.magnetviewer.core.sched

import com.bitseed.magnetviewer.core.model.TorrentFile

/**
 * 预览调度纯函数层（桌面 `core/scheduler.py` 的逐条对齐，参数一个不改）。
 *
 * 本文件只做数学与排序，不碰句柄、不碰 IO，因此能在 JVM 单测里直接跑。
 */

/** 一次向前预约的分块数量**上限**（块数口径，契约冻结）。 */
const val LOOKAHEAD_PIECES: Int = 60

/** 播放窗口的**字节**预算。 */
const val LOOKAHEAD_BYTES: Long = 16L * 1024 * 1024

/** 窗口内相邻分块的 deadline 步长（递增保序）。 */
const val DEADLINE_STEP_MS: Int = 400

/** 尾部窗口下限。 */
const val TAIL_BYTES_MIN: Long = 2L * 1024 * 1024

/** 尾部窗口上限。 */
const val TAIL_BYTES_MAX: Long = 16L * 1024 * 1024

/** 大文件按体积 0.25% 放大尾部窗口。 */
const val TAIL_RATIO: Double = 0.0025

/** 开播门控的「尾部入口」= 文件最后 2MB。 */
const val TAIL_ENTRY_BYTES: Long = 2L * 1024 * 1024

/** 一次预约的尾部块数上限。 */
const val TAIL_MAX_PIECES: Int = 128

/** moov 索引块在尾部的容器家族（FFmpeg mov 解封装器适用）。 */
val TAIL_FIRST_EXTS: Set<String> = setOf(".mp4", ".mov", ".m4v", ".3gp", ".3g2", ".mj2")

/** 向上取整的整除（a >= 0, b > 0）。 */
internal fun ceilDiv(a: Long, b: Long): Long = if (b <= 0) 0L else (a + b - 1) / b

/**
 * 播放窗口块数：按**字节预算**换算，再夹到 `[4, LOOKAHEAD_PIECES]`；
 * 非法块长（<= 0）兜底返回上限常量，不除零。
 */
fun windowPieces(pieceLength: Int): Int {
  if (pieceLength <= 0) return LOOKAHEAD_PIECES
  val byBytes = ceilDiv(LOOKAHEAD_BYTES, pieceLength.toLong())
  return maxOf(4, minOf(LOOKAHEAD_PIECES.toLong(), byBytes).toInt())
}

/**
 * 尾部索引窗口的**字节**预算：`min(size, max(2MB, 0.25%·size), 16MB)`。
 *
 * `int(size * 0.0025)` 用 Double 复现 Python 的截断（`toLong()` 同为向零截断），
 * 边界行为与桌面逐位一致（800MB±1、6.25GB±1 等已由 parity.json 固化）。
 */
fun tailWindowBytes(size: Long): Long {
  if (size <= 0) return 0
  val scaled = (size.toDouble() * TAIL_RATIO).toLong()
  return minOf(size, maxOf(TAIL_BYTES_MIN, scaled), TAIL_BYTES_MAX)
}

/**
 * 「尾部入口」= 文件最后 `min(2MB, size)` 字节覆盖的块（闭区间，升序）。
 *
 * 入口块数量与文件扩展名无关（只有尾窗本身看扩展名）；非法输入返回空列表。
 */
fun tailEntryPieces(file: TorrentFile?, pieceLength: Int): List<Int> {
  if (file == null || file.size <= 0 || pieceLength <= 0) return emptyList()
  val span = minOf(TAIL_ENTRY_BYTES, file.size)
  var n = maxOf(1L, ceilDiv(span, pieceLength.toLong())).toInt()
  n = minOf(n, file.endPiece - file.startPiece + 1)
  if (n <= 0) return emptyList()
  return (file.endPiece - n + 1..file.endPiece).toList()
}

/**
 * 需要优先补拉的尾部窗口（闭区间 piece 列表，升序）；仅对 moov 在尾部的容器家族生效。
 */
fun tailPieceWindow(file: TorrentFile, pieceLength: Int): List<Int> {
  if (file.ext !in TAIL_FIRST_EXTS || file.size <= 0 || pieceLength <= 0) return emptyList()
  val tailBytes = tailWindowBytes(file.size)
  var n = maxOf(1L, ceilDiv(tailBytes, pieceLength.toLong())).toInt()
  n = minOf(n, TAIL_MAX_PIECES, file.endPiece - file.startPiece + 1)
  if (n <= 0) return emptyList()
  return (file.endPiece - n + 1..file.endPiece).toList()
}

/** 单个分块的 ASAP 预约（[deadlineMs] 为相对 deadline，单位毫秒）。 */
data class PieceDeadline(val piece: Int, val deadlineMs: Int)

/**
 * 尾部索引窗口的预约计划：**入口块提前**（与头窗第 2 块同级 = [DEADLINE_STEP_MS]），
 * 其余尾块排在头窗之后（`headPieces*step + 1000 + j*step`）。
 */
fun tailPlan(
  headPieces: Int,
  tailPieces: List<Int>,
  entryPieces: Set<Int>,
  stepMs: Int = DEADLINE_STEP_MS,
): List<PieceDeadline> {
  if (tailPieces.isEmpty()) return emptyList()
  val base = headPieces * stepMs + 1000
  return tailPieces.mapIndexed { j, p ->
    PieceDeadline(p, if (p in entryPieces) stepMs else base + j * stepMs)
  }
}

/**
 * 顺序窗口预约计划：从 [anchor] 起为 `[firstPiece, lastPieceInclusive]` 逐块递增 deadline。
 * 对应桌面 `seek_to_byte` / `tick` 的 `(p - piece) * DEADLINE_STEP_MS`。
 */
fun ascendingPlan(
  firstPiece: Int,
  lastPieceInclusive: Int,
  anchor: Int = firstPiece,
  stepMs: Int = DEADLINE_STEP_MS,
): List<PieceDeadline> {
  if (lastPieceInclusive < firstPiece) return emptyList()
  return (firstPiece..lastPieceInclusive).map { p -> PieceDeadline(p, (p - anchor) * stepMs) }
}

/**
 * 按字节区间即时点播（文件内相对偏移，[endByteExcl] 为开区间）；
 * 单次点播上限与顺序窗口**同源**（16MB 字节预算换算块数，夹 [4,60]，
 * 对齐桌面 core/scheduler.py:528-531），且不移动顺序锚点。
 *
 * C21 修复：旧实现用 [LOOKAHEAD_PIECES]=60 块的**块数口径**封顶——4MB 块下
 * 一次拖动点播 = 240MB 全 ASAP 洪泛（正是桌面 plan/07 阶段 1 实证修掉的
 * 「连续前缀凑不出、缓冲恒 0.0%」成因）。改为 [windowPieces] 字节口径。
 */
fun rangePlan(
  file: TorrentFile,
  pieceLength: Int,
  startByte: Long,
  endByteExcl: Long,
  stepMs: Int = DEADLINE_STEP_MS,
): List<PieceDeadline> {
  if (pieceLength <= 0) return emptyList()
  val pl = pieceLength.toLong()
  val first = maxOf(file.startPiece.toLong(), file.startPiece.toLong() + maxOf(0L, startByte) / pl)
  var last = minOf(file.endPiece.toLong(), file.startPiece.toLong() + maxOf(0L, endByteExcl - 1) / pl)
  // 与顺序窗口同源：16MB 字节预算换算的块数（夹 [4,60]）。
  // 4MB 块 → 4 块（16MB）；16KB 块 → 60 块（仍是旧行为的 960KB）。
  val n = maxOf(1, windowPieces(pieceLength))
  last = minOf(last, first + n.toLong() - 1)
  if (last < first) return emptyList()
  return (first..last).map { p -> PieceDeadline(p.toInt(), ((p - first) * stepMs).toInt()) }
}
