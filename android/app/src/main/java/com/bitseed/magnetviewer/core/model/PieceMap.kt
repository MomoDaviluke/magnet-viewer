package com.bitseed.magnetviewer.core.model

/**
 * 文件 → 种子分块的映射视图（桌面 `core/models.py:PieceMap` 的逐字移植）。
 *
 * [have] 返回该种子分块是否已完整落盘：libtorrent 只有整块落盘才算 have，
 * 未下载区间是稀疏零数据、不可读取。
 */
class PieceMap(
  val pieceLength: Int,
  val offset: Long,
  val startPiece: Int,
  val endPiece: Int,
  val size: Long,
  val have: (Int) -> Boolean,
)

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
