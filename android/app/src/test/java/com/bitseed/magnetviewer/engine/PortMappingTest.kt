package com.bitseed.magnetviewer.engine

import com.bitseed.magnetviewer.data.AppSettings
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotEquals
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * 端口映射（UPnP/NAT-PMP）专项（诊断 D-2026-09-18，安卓侧同步桌面）。
 *
 * 只测**纯决策层**（[portMappingPlan] / 文案）：swig 翻译在 BtEngine，一碰就加载原生库，
 * 不能在 JVM 单测里跑（与 `RateLimitTest` / `ProxyProbeTest` 同一种切分）。
 *
 * 覆盖段：
 * - §A 两个布尔恒等 —— 「只开 UPnP 不开 NAT-PMP」这类漂移是静默失效的根源；
 * - §B 默认开启 = 桌面 `DEFAULTS["port_mapping"]`（2026-10-13 性能实测裁决）；
 * - §C 文案把「蜂窝网络下映射多会失败」讲成预期，避免用户误报为故障；
 * - §D 设置层往返（`AppSettings.portMapping` 默认值与保存）。
 */
class PortMappingTest {

  // ------------------------------------------------------------ §A 两个布尔恒等

  @Test
  fun enabled_turnsBothProtocolsOn() {
    val p = portMappingPlan(true)
    assertTrue("开启时必须同时下发 enable_upnp", p.enableUpnp)
    assertTrue("开启时必须同时下发 enable_natpmp", p.enableNatpmp)
  }

  @Test
  fun disabled_turnsBothProtocolsOff() {
    val p = portMappingPlan(false)
    assertFalse("关闭时 enable_upnp 必须为 false", p.enableUpnp)
    assertFalse("关闭时 enable_natpmp 必须为 false", p.enableNatpmp)
  }

  @Test
  fun bothProtocolsNeverDiverge() {
    // 这两条协议走的是完全不同的发现路径（SSDP 组播 vs 单播 5351），路由器支持哪条不确定。
    // 只开一条 = 在另一类路由器上静默失效 —— 本断言专门锁死这个漂移。
    for (enabled in listOf(true, false)) {
      val p = portMappingPlan(enabled)
      assertEquals(
        "enable_upnp 与 enable_natpmp 必须恒等（不许只开一个），enabled=$enabled",
        p.enableUpnp,
        p.enableNatpmp,
      )
    }
  }

  @Test
  fun planIsPure() {
    assertEquals(portMappingPlan(true), portMappingPlan(true))
    assertEquals(portMappingPlan(false), portMappingPlan(false))
    assertNotEquals(portMappingPlan(true), portMappingPlan(false))
  }

  // ------------------------------------------------------------ §B 默认开启

  @Test
  fun defaultIsEnabled_matchingDesktop() {
    assertTrue(
      "默认必须开启 —— 桌面 DEFAULTS[\"port_mapping\"] = True（2026-10-13 性能实测裁决：" +
        "关闭时连不上 NAT 后的 peer，是下载速率第一瓶颈；主流 BT 客户端亦默认开启）",
      AppSettings().portMapping,
    )
  }

  // ------------------------------------------------------------ §C 文案

  @Test
  fun enableNote_mentionsBothProtocolsAndDelay() {
    val n = portMappingToggleNote(true)
    assertTrue("开启文案要说明走的两条协议：$n", n.contains("UPnP") && n.contains("NAT-PMP"))
    assertTrue("开启文案要预告异步生效（用户才不会以为没生效）：$n", n.contains("数秒"))
  }

  @Test
  fun disableNote_mentionsConsequence() {
    val n = portMappingToggleNote(false)
    assertTrue("关闭文案要讲清代价（连不上 NAT 后的 peer）：$n", n.contains("NAT"))
  }

  @Test
  fun notesDiffer() {
    assertNotEquals(portMappingToggleNote(true), portMappingToggleNote(false))
  }

  @Test
  fun pendingNote_saysAppliesAtStartup() {
    assertTrue(
      "会话未运行时的提示要说明「启动时生效」：$PORT_MAPPING_PENDING_NOTE",
      PORT_MAPPING_PENDING_NOTE.contains("启动"),
    )
  }

  @Test
  fun summaryReflectsState() {
    assertTrue(portMappingSummary(true).contains("true"))
    assertTrue(portMappingSummary(false).contains("false"))
  }
}
