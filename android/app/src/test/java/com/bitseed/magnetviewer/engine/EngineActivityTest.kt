package com.bitseed.magnetviewer.engine

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * 前台服务判据单测（JVM，纯数据）：**驻留**（busy）与**持唤醒锁**（transferActive）
 * 是两个不同的判据，不能混为一谈。
 *
 * - 解析中（meta）：驻留，但只走 DHT 拿元数据、没有文件传输 → 不持唤醒锁；
 * - 预览中（preview）/ convert 后台缓存（download）：驻留 + 持锁；
 * - 已暂停（paused）：仍驻留（等用户「继续」，通知还在），但已 pause 不再传输 → 不持锁；
 * - 无任务（idle）：不驻留 → 服务 stopSelf。
 */
class EngineActivityTest {

  @Test
  fun idle_isNotBusy_andHoldsNoWakeLock() {
    val a = EngineActivity()
    assertEquals(PHASE_IDLE, a.phase)
    assertFalse(a.busy)
    assertFalse(a.transferActive)
  }

  @Test
  fun metaPhase_isBusyWithoutWakeLock() {
    val a = EngineActivity(phase = PHASE_META, title = "fixture")
    assertTrue("解析中必须驻留", a.busy)
    assertFalse("解析阶段没有文件传输，不持唤醒锁", a.transferActive)
  }

  @Test
  fun previewPhase_isBusyWithWakeLock() {
    val a = EngineActivity(phase = PHASE_PREVIEW, title = "fixture.mp4", percent = 42, speedKbps = 300, peers = 2)
    assertTrue(a.busy)
    assertTrue("预览下载中必须持唤醒锁", a.transferActive)
    assertEquals(42, a.percent)
    assertEquals(300, a.speedKbps)
    assertEquals(2, a.peers)
  }

  @Test
  fun pausedPhase_staysResident_butReleasesWakeLock() {
    val a = EngineActivity(phase = PHASE_PAUSED, title = "fixture.mp4")
    assertTrue("暂停后仍要驻留（通知里要被点「继续」）", a.busy)
    assertFalse("已 pause 不再传输，不持唤醒锁", a.transferActive)
  }

  @Test
  fun downloadPhase_isBusyWithWakeLock() {
    val a = EngineActivity(phase = PHASE_DOWNLOAD)
    assertTrue(a.busy)
    assertTrue(a.transferActive)
  }

  @Test
  fun engineHub_publishAndResetRoundTrip() {
    val before = EngineHub.activity
    try {
      EngineHub.publish(EngineActivity(phase = PHASE_PREVIEW, title = "x.mp4"))
      assertEquals(PHASE_PREVIEW, EngineHub.activity.phase)
      assertTrue(EngineHub.activity.busy)
      EngineHub.reset()
      assertEquals(PHASE_IDLE, EngineHub.activity.phase)
      assertFalse(EngineHub.activity.busy)
    } finally {
      EngineHub.publish(before)
    }
  }

  @Test
  fun engineHub_tickReportsWhetherControlAttached() {
    val before = EngineHub.control
    try {
      EngineHub.attach(null)
      assertFalse("没有引擎挂载时 tick 必须报 false（服务据此宽限退场）", EngineHub.tick())
      var ticks = 0
      EngineHub.attach(
        object : EngineControl {
          override fun tick() {
            ticks++
          }

          override fun onPauseRequest() {}

          override fun onResumeRequest() {}

          override fun onStopRequest() {}

          override fun onTimeoutPause() {}
        }
      )
      assertTrue(EngineHub.tick())
      assertEquals(1, ticks)
      EngineHub.attach(null)
      assertNull(EngineHub.control)
    } finally {
      EngineHub.attach(before)
    }
  }
}
