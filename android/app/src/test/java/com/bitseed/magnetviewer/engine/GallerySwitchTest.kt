package com.bitseed.magnetviewer.engine

import com.bitseed.magnetviewer.core.model.TorrentFile
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import org.libtorrent4j.Priority

/**
 * **切换预览目标**（画廊「翻到未下载的图 → 自动切目标」的引擎侧）单测（JVM，无需设备）。
 *
 * 断言的是调用序列这条**顺序事实**（不是看代码猜）：`PreviewScheduler` 只依赖窄接口
 * [PreviewHandleOps]（`TorrentHandle` 是 swig 类型，JVM 里造不出来），所以替身能把每次
 * 句柄调用按顺序记下来：
 * = 换目标必须「先清旧预约（clearPieceDeadlines）→ 再重建窗口（优先级 + 新 deadline）」；
 * - 同一目标幂等（桌面 `_on_gallery_file` 的 `f == self._preview_file: return`）；
 * - 无效句柄下什么都不碰（失效句柄上的 swig 调用是原生崩溃）。
 */
class GallerySwitchTest {

  private class FakeHandleOps(
    var valid: Boolean = true,
    private val pieceLength: Int = PIECE_LENGTH,
    private val numFiles: Int = 4,
  ) : PreviewHandleOps {
    val calls: MutableList<String> = mutableListOf()

    override fun isValid(): Boolean = valid

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

    override fun availability(): PieceAvailability =
      object : PieceAvailability {
        override fun have(piece: Int): Boolean = true
      }
  }

  /** 图片夹具里的两张图（64KB 分块，索引 0/1）。 */
  private fun imgBig() = TorrentFile(0, "payload_img/photo_big.png", 3_965_830L, 0L, 0, 60)
  private fun imgSmall() = TorrentFile(1, "payload_img/photo_small.jpg", 8_429L, 3_965_830L, 61, 61)

  @Test
  fun switchTarget_clearsOldDeadlines_thenRebuildsWindowForTheNewFile() {
    val ops = FakeHandleOps()
    val sched = PreviewScheduler()
    sched.begin(ops, imgBig())
    ops.calls.clear()

    val ok = sched.switchTarget(ops, imgSmall())

    assertTrue(ok)
    val iClear = ops.calls.indexOf("clearPieceDeadlines")
    assertTrue("换目标必须先清旧预约", iClear >= 0)
    val afterClear = ops.calls.drop(iClear + 1)
    assertTrue("清完必须重建窗口（新的 deadline）", afterClear.any { it.startsWith("setPieceDeadline") })
    assertTrue(
      "清旧预约之后、重建窗口之前必须先下发新文件的优先级",
      afterClear.indexOfFirst { it.startsWith("prioritizeFiles") } in 0 until afterClear.indexOfFirst {
        it.startsWith("setPieceDeadline")
      },
    )
    assertTrue(
      "只保留新目标文件的优先级（其余 IGNORE）",
      afterClear.first { it.startsWith("prioritizeFiles") }.contains("DEFAULT") &&
        afterClear.first { it.startsWith("prioritizeFiles") }.count { it == 'D' } == 1,
    )
    assertTrue("新目标窗口必须落在新文件的分块范围内", afterClear.contains("setPieceDeadline(61,0)"))
    assertEquals("调度器的当前文件必须是新目标", "photo_small.jpg", sched.previewFile?.name)
    assertTrue(sched.active)
    assertFalse(sched.paused)
    assertFalse(sched.released)
  }

  @Test
  fun switchTarget_sameFile_isIdempotent_noHandleCalls() {
    val ops = FakeHandleOps()
    val sched = PreviewScheduler()
    sched.begin(ops, imgBig())
    ops.calls.clear()

    val ok = sched.switchTarget(ops, imgBig())

    assertTrue("同目标必须返回成功", ok)
    assertTrue("同目标不得重复下发任何句柄调用（桌面 `f == self._preview_file: return`）", ops.calls.isEmpty())
    assertEquals("photo_big.png", sched.previewFile?.name)
  }

  @Test
  fun switchTarget_firstTarget_doesNotClearAnything_andOpensTheWindow() {
    val ops = FakeHandleOps()
    val sched = PreviewScheduler()

    val ok = sched.switchTarget(ops, imgSmall())

    assertTrue(ok)
    assertFalse("首个目标没有旧预约可清（不得凭空 clear）", ops.calls.contains("clearPieceDeadlines"))
    assertTrue("首个目标也要建窗口", ops.calls.any { it.startsWith("setPieceDeadline") })
    assertEquals("photo_small.jpg", sched.previewFile?.name)
  }

  @Test
  fun switchTarget_invalidHandle_touchesNothing() {
    val ops = FakeHandleOps(valid = false)
    val sched = PreviewScheduler()

    val ok = sched.switchTarget(ops, imgBig())

    assertFalse("无效句柄必须拒绝切换", ok)
    assertTrue("无效句柄下不得调用任何句柄方法", ops.calls.isEmpty())
    assertFalse(sched.active)
  }

  @Test
  fun switchTarget_afterPause_rebuildsWindowAndUnfreezes() {
    val ops = FakeHandleOps()
    val sched = PreviewScheduler()
    sched.begin(ops, imgBig())
    sched.pause()
    assertTrue(sched.paused)
    ops.calls.clear()

    val ok = sched.switchTarget(ops, imgSmall())

    assertTrue(ok)
    assertFalse("切目标后不再是暂停态（新文件要开始下载）", sched.paused)
    assertTrue("必须撤销暂停留下的锚点并重建窗口", ops.calls.any { it.startsWith("setPieceDeadline") })
  }
}

/** 夹具块长（与图片夹具的 64KB 分块一致）。 */
private const val PIECE_LENGTH = 65536
