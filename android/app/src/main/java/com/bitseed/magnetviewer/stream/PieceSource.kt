package com.bitseed.magnetviewer.stream

import com.bitseed.magnetviewer.core.model.PieceMap

/**
 * 流服务向宿主索要「分块可用性」与「按需点播」的接口。
 *
 * 实现方（engine 包）只在预览中的那个文件上给出 [PieceMap]；
 * 找不到对应任务/句柄时**必须返回 null**（= 不可判定），流服务会按「不可用」
 * 处理（等待 → 504），绝不会降级为整文件可用。
 */
interface PieceSource {
  /** 磁盘绝对路径 → 该文件的分块可用性视图；不认识该路径时返回 null。 */
  fun pieceMap(diskPath: String): PieceMap?

  /** 通知调度器：这段字节现在就要（[endExcl] 为开区间）。 */
  fun onDemand(diskPath: String, start: Long, endExcl: Long)
}
