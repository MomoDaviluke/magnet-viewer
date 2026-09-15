package com.bitseed.magnetviewer.ui.components

import com.bitseed.magnetviewer.ui.magnet.TaskRow
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/** TaskCard 的纯逻辑单测（同一取整 D-03；FAILED 禁用操作 B-02；百分比不显 C-02；完成提示）。 */
class TaskCardLogicTest {

  private fun row(state: String, percentText: String = "42%") =
    TaskRow(
      infoHash = "h",
      name = "n",
      state = state,
      totalSizeText = "0 B",
      downloadedText = "0 B",
      progressPercent = 0,
      percentText = percentText,
      note = "",
      priority = 0,
      isDownload = false,
      savePath = "",
      focused = false,
      expanded = false,
    )

  // ---- D-03：同一取整函数 ----
  @Test
  fun percentOf_roundsConsistently() {
    assertEquals(0, TaskCardLogic.percentOf(0f))
    assertEquals(81, TaskCardLogic.percentOf(0.814f))
    assertEquals(82, TaskCardLogic.percentOf(0.815f))
    assertEquals(100, TaskCardLogic.percentOf(1f))
    assertEquals(0, TaskCardLogic.percentOf(Float.NaN))
  }

  // ---- C-02：百分比空 = 不显 ----
  @Test
  fun percentText_emptyWhenUnknown() {
    val r = row(state = "PAUSED", percentText = "")
    assertEquals("", TaskCardLogic.percentText(r))
  }

  @Test
  fun percentText_passesThrough() {
    val r = row(state = "DOWNLOADING", percentText = "42%")
    assertEquals("42%", TaskCardLogic.percentText(r))
  }

  // ---- B-02：FAILED 禁用 转正/暂停/继续 ----
  @Test
  fun shouldDisableOps_forFailedTrue() {
    assertTrue(TaskCardLogic.shouldDisableOps("FAILED"))
    assertTrue(TaskCardLogic.isFailed("FAILED"))
  }

  @Test
  fun shouldDisableOps_forActiveFalse() {
    assertFalse(TaskCardLogic.shouldDisableOps("DOWNLOADING"))
    assertFalse(TaskCardLogic.isFailed("PAUSED"))
  }

  // ---- 完成态：优先级禁用 + 提示 ----
  @Test
  fun completionNote_onlyForCompleted() {
    assertEquals("完成不再影响速度", TaskCardLogic.completionNote("COMPLETED"))
    assertEquals("", TaskCardLogic.completionNote("DOWNLOADING"))
  }

  @Test
  fun priorityEnabled_falseForCompleted() {
    assertFalse(TaskCardLogic.priorityEnabled("COMPLETED"))
    assertTrue(TaskCardLogic.priorityEnabled("DOWNLOADING"))
  }
}
