package com.bitseed.magnetviewer.ui.components

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/** 删除确认 Sheet 的纯逻辑单测（顺序/默认焦点/视觉分级/确认结果）。 */
class DeleteConfirmLogicTest {

  @Test
  fun order_keepsSafetyFirst_dangerLater() {
    assertEquals(
      listOf(DeleteConfirmLogic.Choice.KEEP, DeleteConfirmLogic.Choice.DELETE),
      DeleteConfirmLogic.ORDER,
    )
    assertEquals("默认焦点=保留（安全档）", DeleteConfirmLogic.Choice.KEEP, DeleteConfirmLogic.ORDER.first())
  }

  @Test
  fun dangerousFlag_onlyForDelete() {
    assertFalse(DeleteConfirmLogic.isDangerous(DeleteConfirmLogic.Choice.KEEP))
    assertTrue(DeleteConfirmLogic.isDangerous(DeleteConfirmLogic.Choice.DELETE))
  }

  @Test
  fun gap_is16dp() {
    assertEquals(16, DeleteConfirmLogic.GAP_DP)
  }

  @Test
  fun confirm_nullWhenNoChoice() {
    assertNull(DeleteConfirmLogic.confirm(null))
  }

  @Test
  fun confirm_keepMeansNoDelete() {
    assertEquals(false, DeleteConfirmLogic.confirm(DeleteConfirmLogic.Choice.KEEP))
  }

  @Test
  fun confirm_deleteMeansDeleteData() {
    assertEquals(true, DeleteConfirmLogic.confirm(DeleteConfirmLogic.Choice.DELETE))
  }
}
