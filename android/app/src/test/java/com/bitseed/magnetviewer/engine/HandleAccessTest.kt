package com.bitseed.magnetviewer.engine

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * 回归单测（JVM，不需要设备）：**无效句柄绝不允许触碰 status()**。
 *
 * 背景（本次 P0）：`TorrentHandle.status()` 是 swig 原生调用，对失效句柄调用会在
 * `std::__shared_weak_count::lock()` 上 SIGSEGV —— 原生崩溃，`try/catch(Throwable)`
 * **接不住**。唯一防线是调用前先 `isValid()`。这里的替身句柄记录了 `statusReading()`
 * 被调用了几次，用来断言「isValid=false 时一次也没调」。
 */
class HandleAccessTest {

  /** 纯 Kotlin 替身句柄：不发任何原生调用。 */
  private class FakeHandle(
    private val valid: Boolean,
    /** isValid() 本身是否抛异常（模拟 swig 包装对象已被 delete）。 */
    private val throwOnIsValid: Boolean = false,
    /** statusReading() 是否抛 Java 异常（模拟「即使有效也读失败」）。 */
    private val throwOnRead: Boolean = false,
  ) : HandleProbe {
    var readCount: Int = 0
      private set

    override fun isValid(): Boolean {
      if (throwOnIsValid) throw IllegalStateException("对象已删除")
      return valid
    }

    override fun statusReading(): StatusReading {
      readCount++
      if (throwOnRead) throw IllegalStateException("status 读取失败")
      return StatusReading(peers = 2, seeds = 1, downloadRateKbps = 128)
    }
  }

  // ------------------------------------------------------------ 核心回归：无效句柄不调 status

  @Test
  fun invalidHandle_neverCallsStatus_andReturnsInvalid() {
    val fake = FakeHandle(valid = false)

    val result = readHandleStatus(fake)

    assertEquals("无效句柄必须一次也不读 status", 0, fake.readCount)
    assertTrue("无效句柄必须返回 Invalid 结果，实际：" + result, result is HandleReadResult.Invalid)
    assertEquals(HANDLE_INVALID_TEXT, (result as HandleReadResult.Invalid).message)
  }

  @Test
  fun invalidHandle_readsNothingEvenIfRequestedRepeatedly() {
    val fake = FakeHandle(valid = false)
    repeat(5) { readHandleStatus(fake) }
    assertEquals("轮询 5 次也一次都不能读 status", 0, fake.readCount)
  }

  // ------------------------------------------------------------ 正常路径

  @Test
  fun validHandle_readsExactlyOnce_andReturnsReading() {
    val fake = FakeHandle(valid = true)

    val result = readHandleStatus(fake)

    assertEquals(1, fake.readCount)
    assertTrue(result is HandleReadResult.Ok)
    val reading = (result as HandleReadResult.Ok).reading
    assertEquals(2, reading.peers)
    assertEquals(1, reading.seeds)
    assertEquals(128, reading.downloadRateKbps)
  }

  @Test
  fun noHandle_returnsNoHandle_withoutAnyRead() {
    val result = readHandleStatus(null)
    assertTrue(result is HandleReadResult.NoHandle)
  }

  // ------------------------------------------------------------ 异常兜底（Java 异常，不崩）

  @Test
  fun isValidThrowing_isReportedAsFailed_notPropagated() {
    val fake = FakeHandle(valid = true, throwOnIsValid = true)

    val result = readHandleStatus(fake)

    assertEquals("isValid 抛异常时也不能继续读 status", 0, fake.readCount)
    assertTrue("必须转成 Failed，实际：" + result, result is HandleReadResult.Failed)
  }

  @Test
  fun statusThrowing_isReportedAsFailed_notPropagated() {
    val fake = FakeHandle(valid = true, throwOnRead = true)

    val result = readHandleStatus(fake)

    assertEquals(1, fake.readCount)
    assertTrue("必须转成 Failed，实际：" + result, result is HandleReadResult.Failed)
    assertFalse((result as HandleReadResult.Failed).message.isEmpty())
  }
}
