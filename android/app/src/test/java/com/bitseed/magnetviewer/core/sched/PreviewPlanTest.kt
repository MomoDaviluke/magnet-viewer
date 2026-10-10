package com.bitseed.magnetviewer.core.sched

import com.bitseed.magnetviewer.core.model.TorrentFile
import org.junit.Assert.assertEquals
import org.junit.Test

/**
 * 调度纯函数的边界单测（与桌面 `core/scheduler.py` 的取值逐条对齐）。
 * 每个函数至少 3 个边界用例，含 4MB 块、0、负值。
 */
class PreviewPlanTest {

  private val MB = 1024L * 1024L

  private fun file(size: Long, pieceLength: Int, ext: String, offset: Long = 0L): TorrentFile {
    val start = if (pieceLength <= 0) 0 else (offset / pieceLength).toInt()
    val n = if (pieceLength <= 0 || size <= 0) 0 else ((size + pieceLength - 1) / pieceLength).toInt()
    val end = start + maxOf(0, n - 1)
    return TorrentFile(index = 0, path = "root/movie$ext", size = size, offset = offset, startPiece = start, endPiece = end)
  }

  // ------------------------------------------------------------ window_pieces

  @Test
  fun windowPieces_boundaries() {
    assertEquals(60, windowPieces(16 * 1024)) // 小块 → 上限 60（旧行为逐字一致）
    assertEquals(60, windowPieces(256 * 1024))
    assertEquals(16, windowPieces(1024 * 1024)) // 16MB / 1MB
    assertEquals(8, windowPieces(2 * 1024 * 1024))
    assertEquals(4, windowPieces(4 * 1024 * 1024)) // 4MB 块 → 16MB/4MB = 4
    assertEquals(4, windowPieces(8 * 1024 * 1024)) // ceil(16/8)=2 → 夹到下限 4
    assertEquals(4, windowPieces(16 * 1024 * 1024))
    assertEquals(60, windowPieces(0)) // 非法块长 → 上限常量，不除零
    assertEquals(60, windowPieces(-1))
    assertEquals(60, windowPieces(Int.MIN_VALUE))
  }

  // ------------------------------------------------------------ tail_window_bytes

  @Test
  fun tailWindowBytes_boundaries() {
    assertEquals(0L, tailWindowBytes(0))
    assertEquals(0L, tailWindowBytes(-1))
    assertEquals(1L, tailWindowBytes(1)) // 小文件不超自身
    assertEquals(102400L, tailWindowBytes(102400))
    assertEquals(2097152L, tailWindowBytes(500L * MB)) // 2MB 下限
    assertEquals(2097152L, tailWindowBytes(800L * MB)) // 0.25% == 2MB 的临界
    // 临界以下 0.25% 不足 2MB → 由 2MB 下限接管（与桌面 parity 夹具一致）
    assertEquals(2097152L, tailWindowBytes(800L * MB - 1))
    assertEquals(2097152L, tailWindowBytes(16 * MB - 1)) // 0.25% 仍远低于 2MB → 下限接管
    assertEquals(2097152L, tailWindowBytes(16 * MB))
    assertEquals(2097152L, tailWindowBytes(16 * MB + 1))
    assertEquals(2500000L, tailWindowBytes(1_000_000_000L)) // int(size*0.0025) 截断
    assertEquals(2684354L, tailWindowBytes(1024L * MB))
    assertEquals(10250000L, tailWindowBytes(4_100_000_000L)) // 真机 4.1GB 样本
    assertEquals(10249999L, tailWindowBytes(4_099_999_999L))
    assertEquals(10250000L, tailWindowBytes(4_100_000_001L))
    assertEquals(16777215L, tailWindowBytes(6_710_886_399L)) // 撞 16MB 上限的临界
    assertEquals(16777216L, tailWindowBytes(6_710_886_400L))
    assertEquals(16777216L, tailWindowBytes(6_710_886_401L))
    assertEquals(16777216L, tailWindowBytes(1000L * 1024 * MB)) // 上限夹取
  }

  // ------------------------------------------------------------ tail_entry_pieces

  @Test
  fun tailEntryPieces_boundaries() {
    // 5MB / 256KB 块：最后 2MB = 8 块
    assertEquals((12..19).toList(), tailEntryPieces(file(5 * MB, 262144, ".mp4"), 262144))
    // 5MB / 4MB 块：最后 2MB 落在最末一块
    assertEquals(listOf(1), tailEntryPieces(file(5 * MB, 4194304, ".mp4"), 4194304))
    // 4.1GB / 4MB 块：同样只需最末一块（门控只等这一块）
    assertEquals(listOf(977), tailEntryPieces(file(4_100_000_000L, 4194304, ".mp4"), 4194304))
    // 入口块与扩展名无关（尾窗才看扩展名）
    assertEquals(listOf(1), tailEntryPieces(file(5 * MB, 4194304, ".mkv"), 4194304))
    // 比块还小的文件
    assertEquals(listOf(0), tailEntryPieces(file(100, 262144, ".mp4"), 262144))
    // 非法输入
    assertEquals(emptyList<Int>(), tailEntryPieces(null, 262144))
    assertEquals(emptyList<Int>(), tailEntryPieces(file(5 * MB, 262144, ".mp4"), 0))
    assertEquals(emptyList<Int>(), tailEntryPieces(file(5 * MB, 262144, ".mp4"), -1))
    assertEquals(emptyList<Int>(), tailEntryPieces(file(0, 262144, ".mp4"), 262144))
    // 非零 offset 几何
    assertEquals(listOf(1), tailEntryPieces(file(5 * MB, 4194304, ".mp4", offset = 1000), 4194304))
  }

  // ------------------------------------------------------------ tail_piece_window

  @Test
  fun tailPieceWindow_onlyForMoovTailFamilies() {
    assertEquals((12..19).toList(), tailPieceWindow(file(5 * MB, 262144, ".mp4"), 262144))
    assertEquals(emptyList<Int>(), tailPieceWindow(file(5 * MB, 262144, ".mkv"), 262144))
    assertEquals(emptyList<Int>(), tailPieceWindow(file(5 * MB, 262144, ".webm"), 262144))
    val large = tailPieceWindow(file(4_100_000_000L, 262144, ".mp4"), 262144)
    assertEquals(40, large.size) // 10.25MB / 256KB
    assertEquals(15640, large.last())
    assertEquals(listOf(975, 976, 977), tailPieceWindow(file(4_100_000_000L, 4194304, ".mp4"), 4194304))
    assertEquals(emptyList<Int>(), tailPieceWindow(file(0, 262144, ".mp4"), 262144))
    assertEquals(emptyList<Int>(), tailPieceWindow(file(5 * MB, 0, ".mp4"), 0))
    assertEquals(emptyList<Int>(), tailPieceWindow(file(5 * MB, 262144, ".mp4"), -1))
    // TAIL_MAX_PIECES=128 夹取
    assertEquals(128, tailPieceWindow(file(4_100_000_000L, 65536, ".mp4"), 65536).size)
  }

  // ------------------------------------------------------------ 预约计划

  @Test
  fun tailPlan_entryPieceGetsHeadLevelDeadline() {
    val plan = tailPlan(headPieces = 4, tailPieces = (12..19).toList(), entryPieces = setOf(19))
    assertEquals(8, plan.size)
    // 入口块与头窗第 2 块同级（400ms）
    assertEquals(PieceDeadline(19, 400), plan.first { it.piece == 19 })
    // 其余尾块排在头窗之后：base = 4*400 + 1000 = 2600，逐个 +400
    assertEquals(PieceDeadline(12, 2600), plan.first { it.piece == 12 })
    assertEquals(PieceDeadline(18, 2600 + 6 * 400), plan.first { it.piece == 18 })
    assertEquals(emptyList<PieceDeadline>(), tailPlan(4, emptyList(), emptySet()))
  }

  @Test
  fun ascendingPlan_isMonotonicFromAnchor() {
    val plan = ascendingPlan(100, 103, anchor = 100)
    assertEquals(listOf(0, 400, 800, 1200), plan.map { it.deadlineMs })
    assertEquals(listOf(100, 101, 102, 103), plan.map { it.piece })
    // seek 到远端：deadline 从新锚点起递增（不是 0 洪泛）
    val seek = ascendingPlan(500, 502, anchor = 500)
    assertEquals(listOf(0, 400, 800), seek.map { it.deadlineMs })
    assertEquals(emptyList<PieceDeadline>(), ascendingPlan(5, 4))
  }

  // ------------------------------------------------------------ rangePlan（C21：字节口径夹取）

  @Test
  fun rangePlan_capsAtWindowPieces_andKeepsAnchor() {
    // 4MB 块：夹取口径 = windowPieces(4MB)=4 块（16MB 预算），不是旧的 60 块=240MB 洪泛
    val f = file(4_100_000_000L, 4194304, ".mp4")
    val plan = rangePlan(f, 4194304, startByte = 0, endByteExcl = 4_100_000_000L)
    assertEquals(4, plan.size)
    assertEquals(0, plan.first().piece)
    assertEquals(3, plan.last().piece)
    assertEquals(0, plan.first().deadlineMs)
    assertEquals(3 * 400, plan.last().deadlineMs)
    // 16KB 小块：窗口 60 块封顶（与旧行为逐字一致，契约冻结）
    val small = file(4_100_000_000L, 16384, ".mp4")
    assertEquals(60, rangePlan(small, 16384, 0, 4_100_000_000L).size)
    // 区间中点播：首块立即（0ms），后续递增
    val mid = rangePlan(f, 4194304, startByte = 100 * 4194304L, endByteExcl = 101 * 4194304L)
    assertEquals(listOf(100), mid.map { it.piece })
    assertEquals(listOf(0), mid.map { it.deadlineMs })
    // 非法块长 → 空计划
    assertEquals(emptyList<PieceDeadline>(), rangePlan(f, 0, 0, 1024))
  }
}
