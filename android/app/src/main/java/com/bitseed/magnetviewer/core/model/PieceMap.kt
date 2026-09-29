package com.bitseed.magnetviewer.core.model

/**
 * 文件 → 种子分块的映射视图（桌面 `core/models.py:PieceMap` 的逐字移植）。
 *
 * [have] 返回该种子分块是否已完整落盘：libtorrent 只有整块落盘才算 have，
 * 未下载区间是稀疏零数据、不可读取。
 *
 * [view]（S6，2026-09-27）：**可选**的增量可用性视图（`engine.PieceView`，
 * 桌面 `core/pieceview.py` 同构）。有它时 `contiguousBytesOf` / `readyUntilOf` /
 * `rangeAvailableOf` 走锚点增量快路径（摊销 O(1)）；无它时退回下面的纯函数
 * （逐字等价）。本字段刻意做成不透明 `Any?`——`core.model` 不依赖 engine
 * 也不依赖 libtorrent（对齐桌面 `models.py` 的分层纪律）。
 */
class PieceMap(
  val pieceLength: Int,
  val offset: Long,
  val startPiece: Int,
  val endPiece: Int,
  val size: Long,
  val have: (Int) -> Boolean,
) {
  /** 可选：增量视图（构造后注入；null = 走纯函数路径）。 */
  var view: IncrementalAvailability? = null
}

/**
 * 增量可用性视图的**最小消费接口**（S6；`engine.PieceView` 实现它）。
 *
 * 为什么要切这一刀：`*_of` 派发层在 `core.model`，而视图实现在 `engine`
 * ——分层纪律要求 core.model 不依赖 engine（也不依赖 libtorrent），
 * 故由本包定义接口、engine 侧实现（依赖方向 core.model ← engine）。
 * 语义与桌面 `core/pieceview.py` 逐条对齐：返回 null = 不可判定，
 * 调用方（本文件 `*_of`）退回纯函数，**绝不把不可判定当成「已就绪」**。
 */
interface IncrementalAvailability {
  /** 从 `start`（块号）起连续已落盘的块末端；null = 不可判定。 */
  fun contiguousFrom(start: Int, limit: Int): Int?

  /** 块号闭区间 `[p0, p1]` 是否全部已落盘；null = 不可判定。 */
  fun rangeReadyPieces(p0: Int, p1: Int): Boolean?
}

/** 块号 → 文件内字节位置（**共用换算公式**：位图路径与增量路径必须逐值一致）。 */
private fun bytesUpToPiece(piece: Int, pieceLength: Int, offset: Long, limit: Long): Long =
  maxOf(0L, minOf(limit, piece.toLong() * pieceLength - offset))

/** [contiguousBytes] 的增量版本（无视图 / 不可判定时逐字退回纯函数）。 */
fun contiguousBytesOf(pm: PieceMap, limit: Long? = null): Long {
  val size = if (limit == null) pm.size else minOf(pm.size, limit)
  if (size <= 0 || pm.pieceLength <= 0) return 0
  val v = pm.view
  if (v != null) {
    val end = v.contiguousFrom(pm.startPiece, pm.endPiece + 1)
    if (end != null) return bytesUpToPiece(end, pm.pieceLength, pm.offset, size)
  }
  return contiguousBytes(pm, limit)
}

/** [readyUntil] 的增量版本（无视图 / 不可判定时逐字退回纯函数）。 */
fun readyUntilOf(pm: PieceMap, start: Long, endExcl: Long): Long {
  if (start >= endExcl || pm.pieceLength <= 0 || endExcl > pm.size) return start
  val v = pm.view
  if (v != null) {
    val p0 = ((pm.offset + start) / pm.pieceLength).toInt()
    val p1 = ((pm.offset + endExcl - 1) / pm.pieceLength).toInt()
    val p = v.contiguousFrom(p0, p1 + 1)
    if (p != null) {
      val until = p.toLong() * pm.pieceLength - pm.offset
      return maxOf(start, minOf(endExcl, until))
    }
  }
  return readyUntil(pm, start, endExcl)
}

/** [rangeAvailable] 的增量版本（无视图 / 不可判定时逐字退回纯函数）。 */
fun rangeAvailableOf(pm: PieceMap, start: Long, endExcl: Long): Boolean {
  if (start >= endExcl || endExcl <= 0) return true
  if (endExcl > pm.size || pm.pieceLength <= 0) return false
  val v = pm.view
  if (v != null) {
    val p0 = ((pm.offset + start) / pm.pieceLength).toInt()
    val p1 = ((pm.offset + endExcl - 1) / pm.pieceLength).toInt()
    val ok = v.rangeReadyPieces(p0, p1)
    if (ok != null) return ok
  }
  return rangeAvailable(pm, start, endExcl)
}

/**
 * 文件头部连续可读字节数（从 0 开始、中途无缺失块的连续区间），
 * 对齐桌面 `core/models.py:contiguous_bytes`。
 */
fun contiguousBytes(pm: PieceMap, limit: Long? = null): Long {
  val size = if (limit == null) pm.size else minOf(pm.size, limit)
  if (size <= 0 || pm.pieceLength <= 0) return 0
  var p = pm.startPiece
  while (p <= pm.endPiece && pm.have(p)) p++
  val contig = p.toLong() * pm.pieceLength.toLong() - pm.offset
  return maxOf(0L, minOf(size, contig))
}

/**
 * 从 [start] 起向后延伸，返回第一个**未就绪**字节的位置（开区间末端）；
 * 返回 [start] 表示该处一个就绪字节都没有。对齐桌面 `core/models.py:ready_until`。
 */
fun readyUntil(pm: PieceMap, start: Long, endExcl: Long): Long {
  if (start >= endExcl) return start
  if (pm.pieceLength <= 0 || endExcl > pm.size) return start
  val p0 = (pm.offset + start) / pm.pieceLength
  val p1 = (pm.offset + endExcl - 1) / pm.pieceLength
  var p = p0
  while (p <= p1 && pm.have(p.toInt())) p++
  val until = p * pm.pieceLength.toLong() - pm.offset
  return maxOf(start, minOf(endExcl, until))
}

/**
 * [start], [endExcl) 区间内的每个字节是否都属于已落盘分块。
 * 对齐桌面 `core/models.py:range_available`（不可判定一律按不可用处理）。
 */
fun rangeAvailable(pm: PieceMap, start: Long, endExcl: Long): Boolean {
  if (start >= endExcl || endExcl <= 0) return true
  if (endExcl > pm.size) return false
  if (pm.pieceLength <= 0) return false
  val p0 = (pm.offset + start) / pm.pieceLength
  val p1 = (pm.offset + endExcl - 1) / pm.pieceLength
  var p = p0
  while (p <= p1) {
    if (!pm.have(p.toInt())) return false
    p++
  }
  return true
}
