package com.bitseed.magnetviewer.engine

import org.junit.Assert.assertEquals
import org.junit.Test

/**
 * **I-01 回归（引擎侧）**：下载限速要真的下发到**每一个句柄**。
 *
 * 现场（QA-2026-09-14）：设置「下载限速 = 64 KB/s」后日志确认已下发
 * （`应用下载限速：64 KB/s（0 = 不限）会话运行中=true`），但实测下载 1304 KB/s（≈20×）。
 *
 * 根因（本项目 2026-09-10 的实测记录 + 本轮 javap 复核）：libtorrent 的**会话级**
 * `download_rate_limit` 只管**远端** peer；本机/局域网/回环对端走
 * `local_download_rate_limit`（默认 0 = 不限）。而 libtorrent4j 2.1.0-39 的
 * `settings_pack.int_types` 里**没有** `local_download_rate_limit`（javap 全量枚举实测），
 * 会话级这条路走不通 → 改成同时下发**逐任务** `TorrentHandle.setDownloadLimit(bytes)`。
 *
 * 本用例锁：字节换算、逐句柄下发、0 = 取消限速、失效句柄跳过且如实计数。
 */
class RateLimitTest {

  private class FakeHandleOps(var valid: Boolean = true) : PreviewHandleOps {
    val calls = mutableListOf<String>()
    var downloadLimit: Int? = null

    override fun isValid(): Boolean {
      calls += "isValid"
      return valid
    }
    override fun pieceLength(): Int = 0
    override fun numFiles(): Int = 0
    override fun unsetUploadMode() {}
    override fun setAutoManaged() {}
    override fun unsetAutoManaged() {}
    override fun resume() {}
    override fun pause() {}
    override fun prioritizeFiles(priorities: Array<org.libtorrent4j.Priority>) {}
    override fun setPieceDeadline(piece: Int, deadlineMs: Int) {}
    override fun clearPieceDeadlines() {}
    override fun availability(): PieceAvailability? = null

    override fun setDownloadLimit(bytes: Int) {
      calls += "setDownloadLimit($bytes)"
      downloadLimit = bytes
    }
  }

  @Test
  fun rateLimitBytes_convertsKbpsToBytesPerSecond() {
    assertEquals("libtorrent 单位是字节/秒", 64 * 1024, rateLimitBytes(64))
    assertEquals(0, rateLimitBytes(0))
    assertEquals("负数按 0（不限）", 0, rateLimitBytes(-5))
    assertEquals(300 * 1024, rateLimitBytes(300))
  }

  @Test
  fun applyRateLimit_everyHandleGetsIt() {
    val a = FakeHandleOps()
    val b = FakeHandleOps()
    val n = applyRateLimitToHandles(64, listOf(a, b))
    assertEquals("每个句柄都要下发（否则运行的会话吃不到）", 2, n)
    assertEquals(64 * 1024, a.downloadLimit)
    assertEquals(64 * 1024, b.downloadLimit)
  }

  @Test
  fun applyRateLimit_zeroCancelsLimit() {
    val h = FakeHandleOps().apply { downloadLimit = 64 * 1024 }
    applyRateLimitToHandles(0, listOf(h))
    assertEquals("0 = 不限，要真的下发 0 而不是跳过", 0, h.downloadLimit)
  }

  @Test
  fun applyRateLimit_skipsInvalidHandles_andCountsHonestly() {
    val ok = FakeHandleOps()
    val dead = FakeHandleOps(valid = false)
    val n = applyRateLimitToHandles(128, listOf(dead, ok))
    assertEquals("失效句柄跳过（swig 调用会崩），只算成功的", 1, n)
    assertEquals(null, dead.downloadLimit)
    assertEquals(128 * 1024, ok.downloadLimit)
  }

  @Test
  fun applyRateLimit_noHandles_isZeroNotAnError() {
    assertEquals(0, applyRateLimitToHandles(64, emptyList()))
  }
}
