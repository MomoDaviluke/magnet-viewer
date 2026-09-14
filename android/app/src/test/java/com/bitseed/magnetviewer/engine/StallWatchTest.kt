package com.bitseed.magnetviewer.engine

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * **C-01 回归**：下载停滞要有反馈。
 *
 * QA-2026-09-14 实测：图片夹具下到 30%（1,196,934 字节）后做种端 `peers=0 rate=0`
 * （环境侧「连上即掉线」），UI 连续 4 次 dump（间隔 3s）一直只写 `下载中 30%…`，
 * 既没有掉线提示也没有重试说明 —— 用户只能干等。
 *
 * 判据（本用例锁的口径）：**下载中/元数据中** 且 **连续 N 秒（默认 20s）零字节增长**
 * 且 **peer = 0** → 给一条提示；字节一旦恢复增长，提示**自动消失**。
 *
 * 时钟是**注入**的（`clock: () -> Long`），所以「20 秒」在测试里是瞬间，不依赖 sleep。
 */
class StallWatchTest {

  private var nowMs = 1_000_000L
  private fun advance(seconds: Long) {
    nowMs += seconds * 1000
  }

  private fun watch(thresholdSeconds: Long = 20) = StallWatch(thresholdSeconds) { nowMs }

  @Test
  fun noNote_beforeThreshold() {
    val w = watch()
    assertNull("刚开始下载不该立刻报停滞", w.sample(active = true, downloadedBytes = 1000L, peers = 0))
    advance(19)
    assertNull("还没到 20 秒阈值：不应提示", w.sample(active = true, downloadedBytes = 1000L, peers = 0))
  }

  @Test
  fun noteAppears_afterThresholdWithZeroPeers() {
    val w = watch()
    w.sample(active = true, downloadedBytes = 1_196_934L, peers = 0)
    advance(20)
    val note = w.sample(active = true, downloadedBytes = 1_196_934L, peers = 0)
    assertTrue("20 秒零增长且 peer 0 必须给出提示，实际=$note", note != null)
    assertTrue("提示要说明秒数与 peer 状况：$note", note!!.contains("20 秒") && note.contains("peer 0"))
    assertTrue("提示要给出「正在重试」这类下一步信息：$note", note.contains("重试"))
    assertEquals("同一停滞状态下提示保持稳定", note, w.sample(active = true, downloadedBytes = 1_196_934L, peers = 0))
  }

  @Test
  fun noteDisappears_onceBytesGrow() {
    val w = watch()
    w.sample(active = true, downloadedBytes = 100L, peers = 0)
    advance(25)
    assertTrue(w.sample(active = true, downloadedBytes = 100L, peers = 0) != null)
    // 字节恢复增长 → 提示立即消失
    assertNull("恢复增长后提示必须自动消失", w.sample(active = true, downloadedBytes = 4096L, peers = 0))
  }

  @Test
  fun noNote_whilePeersArePresent() {
    val w = watch()
    w.sample(active = true, downloadedBytes = 100L, peers = 2)
    advance(120)
    assertNull("有 peer 只是在慢慢下（源慢），不该报「掉线」", w.sample(active = true, downloadedBytes = 100L, peers = 2))
  }

  @Test
  fun noNote_whenNotDownloading_andStateResets() {
    val w = watch()
    w.sample(active = true, downloadedBytes = 100L, peers = 0)
    advance(30)
    // 暂停/完成（不处于下载中）→ 不提示，且计时复位
    assertNull("不处于下载中/元数据中就不该提示", w.sample(active = false, downloadedBytes = 100L, peers = 0))
    advance(1)
    assertNull("复位后重新计时（不该沿用旧起点立刻报）", w.sample(active = true, downloadedBytes = 100L, peers = 0))
    advance(20)
    assertTrue("重新进入下载态后 20 秒无增长仍要提示", w.sample(active = true, downloadedBytes = 100L, peers = 0) != null)
  }

  @Test
  fun note_isAPlainSentence_forTheStatusLine() {
    val w = watch(thresholdSeconds = 20)
    w.sample(active = true, downloadedBytes = 0L, peers = 0)
    advance(20)
    val note = w.sample(active = true, downloadedBytes = 0L, peers = 0)!!
    assertTrue("文案要口语化且不带工程术语：$note", note.startsWith("已 ") && note.contains("无数据增长"))
  }
}
