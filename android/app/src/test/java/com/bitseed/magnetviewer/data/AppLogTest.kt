package com.bitseed.magnetviewer.data

import com.bitseed.magnetviewer.engine.PreviewScheduler
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * **I-02 回归**：设置页的「运行日志」开关必须是**真开关**。
 *
 * QA-2026-09-14 实测：关掉并保存后（`settings.preferences_pb` 里 `logging_enabled=false`
 * 已持久化）业务日志照常产出 21 行（`点击解析…` / `加种复用…` / `无任务（连续 3s 空闲）：stopSelf()`
 * …），设置页文案却写「关闭后不再产运行日志」。源码自述见 `data/SettingsStore.kt` 的
 * 「cacheLimitMb / loggingEnabled 仅做持久化与回读」。
 *
 * 修法：所有日志出口（`BtEngine` / `MagnetViewModel` / `DownloadService`，以及
 * `PreviewScheduler` / `PreviewHandleOps` 里同族的 logcat 包装）统一走 [AppLog]；
 * 开关值由设置流（`applySettings`）写进 [AppLog.enabled]。
 *
 * 本测试：
 * 1. 断言 [AppLog] 本身的语义（关掉一条都不出、开着照常出、sink 可替换）；
 * 2. **通过一个真实的日志出口**（[PreviewScheduler] 的 `resume` 失败分支）断言开关真的
 *    接在业务代码上 —— 不是只测一个孤立对象。
 */
class AppLogTest {

  private val captured = ArrayList<String>()

  @After
  fun restore() {
    // 全局单例：用例结束后必须还原，避免影响其它用例
    AppLog.enabled = true
    AppLog.sink = AppLog.defaultSink
  }

  private fun capture() {
    captured.clear()
    AppLog.sink = { _, message -> captured.add(message) }
  }

  @Test
  fun appLog_disabled_emitsNothing() {
    capture()
    AppLog.enabled = false
    AppLog.log("这行不该出现")
    AppLog.warn("这行也不该出现")
    assertEquals("开关关掉后日志出口必须一条都不产出", 0, captured.size)
  }

  @Test
  fun appLog_enabled_emits() {
    capture()
    AppLog.enabled = true
    AppLog.log("元数据就绪")
    assertEquals(listOf("元数据就绪"), captured)
  }

  @Test
  fun appLog_syncFromSettings_followsLoggingEnabled() {
    capture()
    AppLog.syncFromSettings(AppSettings(loggingEnabled = false))
    assertFalse(AppLog.enabled)
    AppLog.log("关了")
    assertEquals(0, captured.size)

    AppLog.syncFromSettings(AppSettings(loggingEnabled = true))
    assertTrue(AppLog.enabled)
    AppLog.log("开了")
    assertEquals(listOf("开了"), captured)
  }

  @Test
  fun appLog_tagIsTheProjectTag() {
    // 单一 tag，logcat 过滤口径不变（logcat -s MagnetViewer）
    assertEquals("MagnetViewer", AppLog.TAG)
  }

  @Test
  fun previewScheduler_realOutlet_respectsTheSwitch() {
    // 真实出口：调度器没有预览任务时 resume() 会打一行日志（JVM 可直达的分支）
    capture()
    AppLog.enabled = true
    PreviewScheduler().resume()
    assertTrue("开着的时候业务日志要出（否则开关接线没生效）", captured.any { it.contains("resume") })

    capture()
    AppLog.enabled = false
    PreviewScheduler().resume()
    assertEquals("关掉后同一个出口一条都不许出：$captured", 0, captured.size)
  }
}
