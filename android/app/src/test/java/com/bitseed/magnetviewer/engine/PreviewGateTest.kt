package com.bitseed.magnetviewer.engine

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * **D-01 回归（引擎/状态语义侧）**：30s 开播门控超时后的文案与状态。
 *
 * QA-2026-09-14 实测：慢源点「预览」→ 30s 后 UI 写
 * `索引块/头部数据等待超时（可能无做种）`，而**做种端一直在正常上传**（源并不慢到无做种），
 * 且顶部状态行仍写「预览中」、文件行的「预览」按钮消失 —— 卡死不可重试。
 *
 * 修法（本用例锁引擎侧能测的部分）：
 * - 文案按 **peer 数**区分：peer>0 = 「源较慢」，**不许**归因「可能无做种」；
 * - 状态行在超时后不得再写「预览中」（VM 侧复位 `previewFailed=true` + `previewIndex=-1`，
 *   见 `MagnetViewModel.preview()`；屏幕重试按钮由 UI 代理按 `previewFailed` 渲染）。
 */
class PreviewGateTest {

  @Test
  fun timeoutNote_withPeers_blamesSlowSource_notAbsentSeeder() {
    val note = previewGateTimeoutNote(peers = 1)
    assertTrue("peer>0 时必须说明是「源较慢」：$note", note.contains("源较慢"))
    assertTrue("要带上 peer 数，用户才知道是连上了但没数据：$note", note.contains("1"))
    assertFalse("peer>0 时禁止归因「可能无做种」（QA D-01 的原话就是在这里误导）", note.contains("无做种"))
  }

  @Test
  fun timeoutNote_withoutPeers_mayBlameNoSeeder() {
    val note = previewGateTimeoutNote(peers = 0)
    assertTrue("一个 peer 都没有时「可能无做种」是诚实描述：$note", note.contains("无做种"))
    assertFalse(note.contains("源较慢"))
  }

  @Test
  fun failedStatus_neverSaysPreviewing_andOffersRetry() {
    for (peers in listOf(0, 2)) {
      val st = previewGateFailedStatus(peers)
      assertFalse("超时后状态行不得再写「预览中」：$st", st.contains("预览中"))
      assertTrue("要告诉用户预览没开始：$st", st.contains("预览未开始"))
      assertTrue("要给出下一步（重试）：$st", st.contains("重试"))
    }
  }

  @Test
  fun gateTimeout_isThirtySeconds() {
    assertEquals(30_000L, PREVIEW_GATE_TIMEOUT_MS)
  }
}
