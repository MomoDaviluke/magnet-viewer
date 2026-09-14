package com.bitseed.magnetviewer.engine

import com.bitseed.magnetviewer.core.model.ParseResult
import com.bitseed.magnetviewer.core.model.TorrentFile
import com.bitseed.magnetviewer.core.states.STATE_COMPLETED
import com.bitseed.magnetviewer.core.states.STATE_DOWNLOADING
import com.bitseed.magnetviewer.core.states.STATE_PAUSED
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * **J-01 / C-02 回归**：状态语义与进度口径必须自洽。
 *
 * QA-2026-09-14 实测（`.hermes/android-qa/QA-2026-09-14.md`）：
 * ```text
 * 任务状态更新（继续任务）：444b8b5c9a69… → DOWNLOADING
 * 任务完成：444b8b5c9a69…（下载 0 字节）
 * UI：总大小 157.9 KB · 已下载 0 B（100%）· COMPLETED      ← 自相矛盾且误导
 * ```
 * 机制：只解析任务的所有文件优先级是 0（`DEFAULT_DONT_DOWNLOAD`），libtorrent 对
 * 「没有想要的数据」的种子 `isFinished()` 是 **vacuous true** —— 点一下「继续」
 * 立刻被判成 COMPLETED（下载 0 字节）。
 *
 * 本测试锁三件事：
 * 1. **完成判定要排除「没有选中文件」的任务**（[isTaskCompleted]）；
 * 2. **百分比与字节数同一口径**：百分比 = 已下载 / **选中**字节；选中为 0 → 不显示
 *    （[selectedBytes] / [progressOf] / [TaskSnapshot.progress] / [TaskSnapshot.progressKnown]）；
 * 3. **「继续」不把仅解析任务送进下载态**（[stateAfterResume]），并给一句人话提示
 *    （[PARSE_ONLY_NO_SELECTION_NOTE]，VM 暴露为 `TaskRow.note` / `MagnetUiState.parseOnlyNote`）。
 */
class TaskProgressTest {

  private fun file(index: Int, path: String, size: Long, pad: Boolean = false): TorrentFile =
    TorrentFile(
      index = index,
      // .pad 文件按 libtorrent 的布局放在 `<dir>/.pad/<name>` 下（isPad 的判据）
      path = if (pad) "${path.substringBeforeLast('/')}/.pad/${path.substringAfterLast('/')}" else path,
      size = size,
      offset = 0L,
      startPiece = 0,
      endPiece = 0,
    )

  private fun result(vararg files: TorrentFile): ParseResult =
    ParseResult(
      infoHash = "444b8b5c9a69b388a5013d85a42caef060f03225",
      name = "payload",
      totalSize = files.filter { !it.isPad }.sumOf { it.size },
      pieceSize = 16384,
      numPieces = 10,
      files = files.toList(),
      trackers = emptyList(),
      comment = "",
      createdBy = "",
      source = "magnet",
    )

  // ------------------------------------------------------------ 选中字节 / 进度口径

  @Test
  fun selectedBytes_parseOnlyTask_hasNoSelection() {
    val r = result(file(0, "payload/a.mp4", 100_000L), file(1, "payload/b.mp4", 57_900L))
    // 只解析（download=false）且没有选中任何文件 → 选中字节 = 0（分母不成立）
    assertEquals(0L, selectedBytes(r, emptyList(), download = false))
  }

  @Test
  fun selectedBytes_downloadTaskEmptySelection_meansAllNonPadFiles() {
    val r = result(file(0, "payload/a.mp4", 100_000L), file(1, "payload/b.mp4", 57_900L), file(2, "payload/p", 16_384L, pad = true))
    assertEquals("空选中 = 全选，且 .pad 不算", 157_900L, selectedBytes(r, emptyList(), download = true))
  }

  @Test
  fun selectedBytes_selectedSubset_countsOnlyThoseFiles() {
    val r = result(file(0, "payload/a.mp4", 100_000L), file(1, "payload/b.mp4", 57_900L))
    assertEquals(57_900L, selectedBytes(r, listOf("payload/b.mp4"), download = true))
  }

  @Test
  fun selectedBytes_noMetadata_isUnknown() {
    assertEquals(0L, selectedBytes(null, listOf("payload/a.mp4"), download = true))
  }

  @Test
  fun progressOf_noSelection_isNull_notZero_notOne() {
    assertNull("选中字节为 0 → 进度不可知（UI 不显示百分比）", progressOf(0L, 0L))
    assertNull(progressOf(123L, 0L))
  }

  @Test
  fun progressOf_isDownloadedOverSelected() {
    assertEquals(0.5, progressOf(50L, 100L)!!, 1e-9)
    assertEquals("夹取到 1.0（不出现 120%）", 1.0, progressOf(120L, 100L)!!, 1e-9)
    assertEquals(0.0, progressOf(0L, 100L)!!, 1e-9)
  }

  // ------------------------------------------------------------ 完成判定（J-01 核心）

  @Test
  fun isTaskCompleted_parseOnlyTask_isNeverCompleted() {
    // libtorrent 对「want 0」的种子报 isFinished()=true（vacuous），但这不是完成
    assertFalse(
      "仅解析任务没有选中文件 → 不许判成完成（QA J-01 的根因）",
      isTaskCompleted(libtorrentFinished = true, download = false, selectedBytes = 0L),
    )
  }

  @Test
  fun isTaskCompleted_downloadTaskWithoutSelection_isNotCompleted() {
    assertFalse(
      "下载型任务但选中字节为 0（优先级全 0）同样不算完成",
      isTaskCompleted(libtorrentFinished = true, download = true, selectedBytes = 0L),
    )
  }

  @Test
  fun isTaskCompleted_realDownload_completes() {
    assertTrue(isTaskCompleted(libtorrentFinished = true, download = true, selectedBytes = 157_900L))
    assertFalse("没下完就不算完成", isTaskCompleted(libtorrentFinished = false, download = true, selectedBytes = 157_900L))
  }

  // ------------------------------------------------------------ 「继续」的状态语义

  @Test
  fun stateAfterResume_parseOnlyNoSelection_staysPaused() {
    assertEquals(
      "仅解析任务点「继续」不得进入 DOWNLOADING（否则下一步就被判 COMPLETED）",
      STATE_PAUSED,
      stateAfterResume(download = false, selected = emptyList()),
    )
  }

  @Test
  fun stateAfterResume_downloadTask_goesDownloading() {
    assertEquals(STATE_DOWNLOADING, stateAfterResume(download = true, selected = emptyList()))
    assertEquals(STATE_DOWNLOADING, stateAfterResume(download = false, selected = listOf("payload/a.mp4")))
  }

  @Test
  fun parseOnlyNote_isAUserSentence() {
    assertTrue("提示要写清「没有选中文件」：$PARSE_ONLY_NO_SELECTION_NOTE", PARSE_ONLY_NO_SELECTION_NOTE.contains("仅解析"))
    assertTrue(PARSE_ONLY_NO_SELECTION_NOTE.contains("未选择"))
  }

  // ------------------------------------------------------------ TaskSnapshot 收口（C-02）

  @Test
  fun snapshot_progress_usesSelectedBytes_andHidesPercentWithoutSelection() {
    val noSelection =
      TaskSnapshot(infoHash = "444b8b5c9a69b388a5013d85a42caef060f03225", name = "payload", state = STATE_COMPLETED, totalSize = 157_900L, downloadedBytes = 0L)
    assertEquals("「已下载 0 B（100%）」这种自相矛盾不许再出现", 0.0, noSelection.progress, 1e-9)
    assertFalse("没有选中文件 → 百分比不可知", noSelection.progressKnown)

    val half =
      TaskSnapshot(
        infoHash = "444b8b5c9a69b388a5013d85a42caef060f03225",
        name = "payload",
        state = STATE_DOWNLOADING,
        totalSize = 157_900L,
        downloadedBytes = 50_000L,
        selectedBytes = 100_000L,
      )
    assertEquals("百分比 = 已下载 / 选中字节（不是 / 总大小）", 0.5, half.progress, 1e-9)
    assertTrue(half.progressKnown)
  }

  @Test
  fun snapshot_progress_sumsTo100PercentOnlyWhenReallyDone() {
    val done =
      TaskSnapshot(
        infoHash = "444b8b5c9a69b388a5013d85a42caef060f03225",
        name = "payload",
        state = STATE_COMPLETED,
        totalSize = 157_900L,
        downloadedBytes = 157_900L,
        selectedBytes = 157_900L,
      )
    assertEquals(1.0, done.progress, 1e-9)
  }
}
