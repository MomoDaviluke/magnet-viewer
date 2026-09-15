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

  // ---------------------------------------------------------------- 设备实测新增（N-2）

  /**
   * **N-2 回归**：停滐计时必须跟着「看的是哪个任务」走。
   *
   * 设备实测（`.hermes/android-qa/DEVICE-VERIFY-2026-09-15.md` §5，2026-09-15）：
   * 移除旧任务、**重新解析一个新任务**之后，新任务刚 `added`（state=META_FETCH、0 字节）
   * 状态区就已经写着 `已 155 秒无数据增长（peer 0）…` —— 计时是从**上一个任务**最后一次字节增长算起的，
   * 对新任务完全是错误信息（用户会以为新任务也挂了 155 秒）。
   *
   * 判据：**采样对象（infoHash）一变，计时立刻复位**，新对象从 0 起表。
   */
  @Test
  fun subjectChange_resetsTheTimer_forTheNewTask() {
    val w = watch(thresholdSeconds = 20)
    // 旧任务：连续 25 秒零增长（peer 0）→ 报「已 25 秒」
    w.sample(active = true, downloadedBytes = 0L, peers = 0, subject = "aaa")
    advance(25)
    val old = w.sample(active = true, downloadedBytes = 0L, peers = 0, subject = "aaa")
    assertTrue("旧任务该报停滞：$old", old != null && old.contains("25 秒"))

    // 换成新任务（同一时钟、字节同样为 0）：不得沿用旧起点
    assertNull(
      "换了采样对象必须立刻复位计时（否则新任务一上来就显示「已 155 秒」）",
      w.sample(active = true, downloadedBytes = 0L, peers = 0, subject = "bbb"),
    )
    advance(19)
    assertNull("新任务自己还差 1 秒到阈值", w.sample(active = true, downloadedBytes = 0L, peers = 0, subject = "bbb"))
    advance(1)
    val fresh = w.sample(active = true, downloadedBytes = 0L, peers = 0, subject = "bbb")
    assertTrue("新任务按自己的起点计时满 20 秒才提示：$fresh", fresh != null && fresh.contains("20 秒"))
  }

  @Test
  fun sameSubject_keepsCountingAcrossSamples() {
    val w = watch(thresholdSeconds = 20)
    w.sample(active = true, downloadedBytes = 0L, peers = 0, subject = "aaa")
    advance(12)
    assertNull(w.sample(active = true, downloadedBytes = 0L, peers = 0, subject = "aaa"))
    advance(8)
    assertTrue(
      "同一对象不因多次采样而重置（累计 20 秒）",
      w.sample(active = true, downloadedBytes = 0L, peers = 0, subject = "aaa") != null,
    )
  }
}
