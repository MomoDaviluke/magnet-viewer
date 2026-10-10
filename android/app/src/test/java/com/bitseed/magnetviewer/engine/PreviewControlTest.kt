package com.bitseed.magnetviewer.engine

import com.bitseed.magnetviewer.core.model.TorrentFile
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import org.libtorrent4j.Priority

/**
 * 预览控制（暂停 / 继续 / 停止 / 跳转）的**调用序列**单测（JVM，无需设备）。
 *
 * 怎么能在 JVM 里断言「句柄被调了什么」：调度器只依赖窄接口 [PreviewHandleOps]
 * （`TorrentHandle` 是 swig 类型，类静态块要加载原生库，造不出来）。替身把每次调用
 * 按顺序记进 `calls`，于是「hold 档必须有 pause()」「convert 档必须没有」都是
 * 可断言的**顺序事实**，而不是看代码猜。
 *
 * 对齐的桌面语义是 `core/scheduler.py:stop(release_only=…)`：
 * - `release_only=false`（hold）：clear_piece_deadlines → prioritize_files([0]*n)
 *   → unset_flags(auto_managed) → pause；
 * - `release_only=true`（convert）：**只** clear_piece_deadlines（+ 还原锚点），
 *   不 pause、不清优先级、不撤 auto_managed。
 */
class PreviewControlTest {

  /** 纯 Kotlin 替身句柄：不发任何原生调用，按顺序记录动作。 */
  private class FakeHandleOps(
    var valid: Boolean = true,
    private val pieceLength: Int = PIECE_LENGTH,
    private val numFiles: Int = 1,
    private val have: Set<Int> = emptySet(),
  ) : PreviewHandleOps {
    /** 句柄**动作**调用记录（不含 isValid 探测）。 */
    val calls: MutableList<String> = mutableListOf()

    /** isValid 被探了多少次（守卫确实在用）。 */
    var isValidCount: Int = 0
      private set

    override fun isValid(): Boolean {
      isValidCount++
      return valid
    }

    override fun pieceLength(): Int {
      calls += "pieceLength"
      return pieceLength
    }

    override fun numFiles(): Int {
      calls += "numFiles"
      return numFiles
    }

    override fun unsetUploadMode() {
      calls += "unsetUploadMode"
    }

    override fun setAutoManaged() {
      calls += "setAutoManaged"
    }

    override fun unsetAutoManaged() {
      calls += "unsetAutoManaged"
    }

    override fun resume() {
      calls += "resume"
    }

    override fun pause() {
      calls += "pause"
    }

    override fun prioritizeFiles(priorities: Array<Priority>) {
      calls += "prioritizeFiles(" + priorities.joinToString(",") { it.name } + ")"
    }

    override fun setPieceDeadline(piece: Int, deadlineMs: Int) {
      calls += "setPieceDeadline($piece,$deadlineMs)"
    }

    override fun clearPieceDeadlines() {
      calls += "clearPieceDeadlines"
    }

    override fun availability(): PieceAvailability {
      calls += "availability"
      return object : PieceAvailability {
        override fun have(piece: Int): Boolean = piece in have
      }
    }

    override fun setDownloadLimit(bytes: Int) {
      calls += "setDownloadLimit($bytes)"
    }
  }

  private fun file() = TorrentFile(index = 0, path = "fixture/fixture.mp4", size = 80832L, offset = 0L, startPiece = 0, endPiece = 4)

  // ------------------------------------------------------------ hold 档：清 → 优先级 0 → 撤 auto_managed → pause

  @Test
  fun holdStop_clearsThenZeroesPrioritiesThenUnsetsAutoManagedThenPauses() {
    val ops = FakeHandleOps()
    val sched = PreviewScheduler()
    sched.begin(ops, file())
    ops.calls.clear()

    sched.stop(releaseOnly = false)

    assertEquals(
      "hold 档的句柄动作必须逐条对齐桌面 stop(release_only=False)",
      listOf("clearPieceDeadlines", "numFiles", "prioritizeFiles(IGNORE)", "unsetAutoManaged", "pause"),
      ops.calls,
    )
    assertEquals("hold 档最后一击必须是 pause()", "pause", ops.calls.last())
    assertFalse("hold 档停止后调度器必须回到 inactive", sched.active)
    assertNull(sched.previewFile)
  }

  @Test
  fun pause_matchesHoldStopSequence_andKeepsStateForResume() {
    val ops = FakeHandleOps()
    val sched = PreviewScheduler()
    sched.begin(ops, file())
    ops.calls.clear()

    sched.pause()

    assertEquals(
      listOf("clearPieceDeadlines", "numFiles", "prioritizeFiles(IGNORE)", "unsetAutoManaged", "pause"),
      ops.calls,
    )
    assertTrue("暂停后调度器仍持有预览任务（要继续）", sched.active)
    assertTrue(sched.paused)
    assertFalse(sched.released)
  }

  // ------------------------------------------------------------ convert 档：只清 deadline，绝不 pause

  @Test
  fun convertStop_clearsDeadlinesOnly_neverPauses() {
    val ops = FakeHandleOps()
    val sched = PreviewScheduler()
    sched.begin(ops, file())
    ops.calls.clear()

    sched.stop(releaseOnly = true)

    assertEquals("convert 档只允许清 deadline 一个句柄动作", listOf("clearPieceDeadlines"), ops.calls)
    assertFalse("convert 档**不得**调用 pause()", ops.calls.contains("pause"))
    assertFalse(ops.calls.any { it.startsWith("prioritizeFiles") })
    assertFalse(ops.calls.contains("unsetAutoManaged"))
    assertTrue("convert 档后仍在「已释放」态（tick/seek 停手）", sched.released)
  }

  // ------------------------------------------------------------ 暂停 / 继续

  @Test
  fun resume_replaysBeginActions_andRebuildsWindow() {
    val ops = FakeHandleOps()
    val sched = PreviewScheduler()
    sched.begin(ops, file())
    sched.pause()
    ops.calls.clear()

    val ok = sched.resume()

    assertTrue(ok)
    val iAuto = ops.calls.indexOf("setAutoManaged")
    val iResume = ops.calls.indexOf("resume")
    val iPrio = ops.calls.indexOfFirst { it.startsWith("prioritizeFiles") }
    assertTrue("必须重新打开 auto_managed", iAuto >= 0)
    assertTrue("auto_managed 必须在 resume 之前", iResume > iAuto)
    assertTrue("必须重新下发文件优先级", iPrio >= 0)
    assertTrue("必须重建预约窗口", ops.calls.any { it.startsWith("setPieceDeadline") })
    assertFalse(sched.paused)
    assertFalse(sched.released)
  }

  @Test
  fun pausedScheduler_ignoresTickSeekAndOnDemand() {
    val ops = FakeHandleOps()
    val sched = PreviewScheduler()
    sched.begin(ops, file())
    sched.pause()
    ops.calls.clear()

    sched.tick()
    sched.seekToByte(4096L)
    sched.requestRange(0L, 16384L)

    assertTrue("暂停期间不得再下任何 deadline / 跳转", ops.calls.isEmpty())
  }

  @Test
  fun releasedScheduler_stopsAutoArming_butStillAnswersAvailability() {
    val ops = FakeHandleOps(have = setOf(0))
    val sched = PreviewScheduler()
    sched.begin(ops, file())
    sched.stop(releaseOnly = true)
    ops.calls.clear()

    sched.tick()
    sched.seekToByte(4096L)
    assertTrue("convert 释放后不得自动补窗", ops.calls.none { it.startsWith("setPieceDeadline") })

    assertEquals("释放后分块可用性查询照旧（流服务还在读）", 16384L, sched.contiguousProgress())
  }

  // ------------------------------------------------------------ C23 rearm：无条件重刷（催数据）

  @Test
  fun rearm_unconditionallyReschedulesTailAndPlayWindow() {
    val ops = FakeHandleOps()
    val sched = PreviewScheduler()
    sched.begin(ops, file())
    ops.calls.clear()

    sched.rearm()

    // 对齐桌面 scheduler.py:722-755：不 clear、直接覆盖式重下 deadline
    assertFalse("rearm 不得 clearPieceDeadlines（桌面同样只覆盖）", ops.calls.contains("clearPieceDeadlines"))
    val deadlines = ops.calls.filter { it.startsWith("setPieceDeadline") }
    assertTrue("rearm 必须重刷播放位置窗口（首块 deadline=0）", deadlines.contains("setPieceDeadline(0,0)"))
    // 尾窗（入口块）与头窗都必须在名单里：夹具文件 5 块全部是尾窗+入口+头窗的并集
    assertTrue("必须覆盖到文件尾块", deadlines.any { it.startsWith("setPieceDeadline(4,") })
    assertTrue("头窗必须递增（piece1 → 400ms）", deadlines.contains("setPieceDeadline(1,400)"))
  }

  @Test
  fun rearm_pausedOrReleased_doesNothing() {
    val ops = FakeHandleOps()
    val sched = PreviewScheduler()
    sched.begin(ops, file())
    sched.pause()
    ops.calls.clear()
    sched.rearm()
    assertTrue("暂停期间 rearm 不得动句柄", ops.calls.isEmpty())

    val ops2 = FakeHandleOps()
    val sched2 = PreviewScheduler()
    sched2.begin(ops2, file())
    sched2.stop(releaseOnly = true)
    ops2.calls.clear()
    sched2.rearm()
    assertTrue("convert 释放后 rearm 不得动句柄", ops2.calls.isEmpty())
  }

  // ------------------------------------------------------------ seek：先 clear 再重建

  @Test
  fun seek_clearsPreviousDeadlines_beforeRebuildingWindow() {
    val ops = FakeHandleOps()
    val sched = PreviewScheduler()
    sched.begin(ops, file())
    ops.calls.clear()

    sched.seekToByte(2L * PIECE_LENGTH) // 跳到第 2 块

    val iClear = ops.calls.indexOf("clearPieceDeadlines")
    assertTrue("跳转必须先 clearPieceDeadlines()", iClear >= 0)
    val afterClear = ops.calls.drop(iClear + 1)
    assertTrue("清完必须重建窗口", afterClear.any { it.startsWith("setPieceDeadline") })
    assertTrue("clear 之后第一个动作就得是重建预约", afterClear.first().startsWith("setPieceDeadline"))
    assertTrue("新位置窗口必须从目标块起（piece 2 的 deadline 为 0）", afterClear.contains("setPieceDeadline(2,0)"))
  }

  // ------------------------------------------------------------ 无效句柄：所有入口都不碰句柄

  @Test
  fun invalidHandle_allNewEntriesTouchNothing() {
    val ops = FakeHandleOps()
    val sched = PreviewScheduler()
    sched.begin(ops, file())
    ops.calls.clear()
    ops.valid = false

    sched.pause()
    sched.resume()
    sched.tick()
    sched.seekToByte(16384L)
    sched.requestRange(0L, 16384L)
    sched.stop(releaseOnly = true)
    sched.stop(releaseOnly = false)

    assertTrue("无效句柄下所有入口都不得调用句柄方法（实际：" + ops.calls + "）", ops.calls.isEmpty())
    assertTrue("守卫必须真的探过 isValid()", ops.isValidCount > 0)
  }

  @Test
  fun invalidHandle_beginThrowsWithoutTouchingAnything() {
    val ops = FakeHandleOps(valid = false)
    val sched = PreviewScheduler()

    var threw = false
    try {
      sched.begin(ops, file())
    } catch (e: IllegalStateException) {
      threw = true
    }

    assertTrue("无效句柄下 begin 必须抛 IllegalStateException", threw)
    assertTrue("begin 也不得触碰任何句柄方法", ops.calls.isEmpty())
    assertFalse(sched.active)
  }
}

/** 夹具块长（与靶场 16KB 分块一致）。 */
private const val PIECE_LENGTH = 16384
