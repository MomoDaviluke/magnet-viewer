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
 *
 * 复审追加（2026-09-15，「假完成」）：上面第 1 条只挡住了「没有选中文件」，**没挡住「选中了但没下完」**。
 * 真磁力链实测 `（下载 1511277003 字节 / 选中 3591733632 字节）` 被判 COMPLETED ——
 * 根因是 libtorrent 的 `isFinished()` 是**「wanted 集合」口径**，而预览暂停 / hold 档停止会把
 * 全部文件优先级置 0（wanted = 0）→ **虚真**；此时任务仍是 DOWNLOADING、字节一个没多。
 * 因此完成判定改为同时要求 **「选中文件已落字节 ≥ 选中字节」**（[pickedDoneBytes]）：
 * 分子分母同源（都只算选中集合、逐文件按 size 夹取），不再给虚真留缝。
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

  // ------------------------------------------------------------ 选中文件的已下载字节（选定集合口径）

  @Test
  fun pickedDoneBytes_parseOnlyTask_isZero() {
    val r = result(file(0, "payload/a.mp4", 100_000L))
    assertEquals(0L, pickedDoneBytes(r, emptyList(), download = false, fileProgress = longArrayOf(100_000L)))
  }

  @Test
  fun pickedDoneBytes_emptySelection_sumsAllNonPadFiles_cappedPerFile() {
    val r = result(file(0, "payload/a.mp4", 100_000L), file(1, "payload/b.mp4", 57_900L), file(2, "payload/p", 16_384L, pad = true))
    // 逐文件按 size 夹取：既排除 pad（index 2 的读数一律不计），也不让「共享分块导致读数 > size」膨胀
    assertEquals(157_900L, pickedDoneBytes(r, emptyList(), download = true, fileProgress = longArrayOf(100_000L, 60_000L, 16_384L)))
    assertEquals(100_000L, pickedDoneBytes(r, emptyList(), download = true, fileProgress = longArrayOf(120_000L, 0L, 16_384L)))
  }

  @Test
  fun pickedDoneBytes_selectedSubset_countsOnlyThoseFiles() {
    val r = result(file(0, "payload/a.mp4", 100_000L), file(1, "payload/b.mp4", 57_900L))
    assertEquals(57_900L, pickedDoneBytes(r, listOf("payload/b.mp4"), download = true, fileProgress = longArrayOf(100_000L, 57_900L)))
  }

  @Test
  fun pickedDoneBytes_missingOrShortReadings_areConservativeNotCrash() {
    val r = result(file(0, "payload/a.mp4", 100_000L), file(1, "payload/b.mp4", 57_900L))
    assertEquals("读数为 null → 保守 0（宁可晚判完成，不可早判）", 0L, pickedDoneBytes(r, emptyList(), download = true, fileProgress = null))
    assertEquals("读数数组比文件数短 → 缺的按 0", 100_000L, pickedDoneBytes(r, emptyList(), download = true, fileProgress = longArrayOf(100_000L)))
    assertEquals("负数读数按 0", 0L, pickedDoneBytes(r, emptyList(), download = true, fileProgress = longArrayOf(-5L, -1L)))
    assertEquals(0L, pickedDoneBytes(null, emptyList(), download = true, fileProgress = longArrayOf(1L)))
  }

  // ------------------------------------------------------------ 完成判定（J-01 + 假完成 核心）

  @Test
  fun isTaskCompleted_parseOnlyTask_isNeverCompleted() {
    // libtorrent 对「want 0」的种子报 isFinished()=true（vacuous），但这不是完成
    assertFalse(
      "仅解析任务没有选中文件 → 不许判成完成（QA J-01 的根因）",
      isTaskCompleted(libtorrentFinished = true, download = false, selectedBytes = 0L, doneBytes = 0L),
    )
  }

  @Test
  fun isTaskCompleted_downloadTaskWithoutSelection_isNotCompleted() {
    assertFalse(
      "下载型任务但选中字节为 0（优先级全 0）同样不算完成",
      isTaskCompleted(libtorrentFinished = true, download = true, selectedBytes = 0L, doneBytes = 0L),
    )
  }

  @Test
  fun isTaskCompleted_realDownload_completes() {
    assertTrue(isTaskCompleted(libtorrentFinished = true, download = true, selectedBytes = 157_900L, doneBytes = 157_900L))
    assertTrue("共享分块/pad 可能让读数略超 → 仍算完成", isTaskCompleted(libtorrentFinished = true, download = true, selectedBytes = 157_900L, doneBytes = 160_000L))
    assertFalse("没下完就不算完成", isTaskCompleted(libtorrentFinished = false, download = true, selectedBytes = 157_900L, doneBytes = 157_900L))
  }

  @Test
  fun isTaskCompleted_partialDownload_isNotCompleted_theRealMagnetCase() {
    // RELEASE-TEST-2026-09-15 §6.2 实测原文：
    //   任务完成：5806ff5dc7f9…（下载 1511277003 字节 / 选中 3591733632 字节）
    // → 已下载 1.51 GB 远小于选中 3.59 GB，却被判 COMPLETED。
    assertFalse(
      "已下载 < 选中 → 绝不许判完成（哪怕 libtorrent 说 finished）",
      isTaskCompleted(
        libtorrentFinished = true,
        download = true,
        selectedBytes = 3_591_733_632L,
        doneBytes = 1_511_277_003L,
      ),
    )
  }

  @Test
  fun isTaskCompleted_previewHoldResetsWanted_isNotCompleted() {
    // 根因复刻：预览「暂停 / hold 档停止」把**全部文件优先级置 IGNORE**（wanted = 0），
    // libtorrent 的 finished 变成 vacuous true；此时任务仍在下载态、一个字节都没多下。
    assertFalse(
      "预览暂停把 wanted 归零 → libtorrent 虚真 finished，但字节没到 → 不许判完成",
      isTaskCompleted(libtorrentFinished = true, download = true, selectedBytes = 157_900L, doneBytes = 0L),
    )
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

  // ------------------------------------------------------------ C41 周期脏写筛选（对齐桌面 resume_sweep）

  @Test
  fun resumeSweepNeeded_onlyDownloadingTasks() {
    // 桌面 core/session.py:823-827：仅「download 且 state == DOWNLOADING」进周期脏写名单
    assertTrue(resumeSweepNeeded(download = true, state = STATE_DOWNLOADING))
    assertFalse("暂停态没有新进度", resumeSweepNeeded(download = true, state = STATE_PAUSED))
    assertFalse("完成态同样不再脏写", resumeSweepNeeded(download = true, state = STATE_COMPLETED))
    assertFalse("仅解析任务不落 fastresume（C44 守卫同口径）", resumeSweepNeeded(download = false, state = STATE_DOWNLOADING))
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
