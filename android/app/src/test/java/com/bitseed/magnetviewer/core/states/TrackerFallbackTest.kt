package com.bitseed.magnetviewer.core.states

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotEquals
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * 兜底 tracker 注入面专项（诊断 D-2026-09-18，安卓侧同步桌面）。
 *
 * 覆盖段：
 * - §A 默认路径（`extraTrackers=false`）**逐字等于**既有 BOOTSTRAP_TRACKERS —— 这是本项
 *   最重要的不变量：新增能力不得改动老路径（既有断言 + 桌面 parity 夹具都压在这一条上）；
 * - §B 开启后 = BOOTSTRAP + FALLBACK，且**前 N 条不变**（顺序与桌面
 *   `BOOTSTRAP_TRACKERS + FALLBACK_TRACKERS` 完全一致）；
 * - §C FALLBACK 全是 http/https（TCP 通道）且与 BOOTSTRAP 不重叠 —— 若与 udp 那组
 *   重叠，「多一条通道」就变成同一条通道多发一次，没有任何冗余价值；
 * - §D 纯函数性：多次调用结果相等、不改动源常量（不可变性）。
 */
class TrackerFallbackTest {

  // ------------------------------------------------------------ §A 默认路径不变

  @Test
  fun defaultPath_isExactlyBootstrapTrackers() {
    assertEquals(
      "extraTrackers=false 必须逐字等于既有 5 条（老路径不许被改动）",
      BOOTSTRAP_TRACKERS,
      trackersFor(false),
    )
    assertEquals(5, trackersFor(false).size)
    assertEquals(BOOTSTRAP_TRACKERS.size, trackersFor(false).size)
  }

  @Test
  fun defaultPath_allUdp() {
    // 基线那 5 条全是 udp:// —— 这是桌面 parity 夹具冻结的形态，不许漂移
    assertTrue(
      "基线 tracker 必须全为 udp://（桌面 parity 冻结）",
      trackersFor(false).all { it.startsWith("udp://") },
    )
  }

  // ------------------------------------------------------------ §B 开启后追加

  @Test
  fun enabledPath_isBootstrapPlusFallback() {
    val on = trackersFor(true)
    assertEquals(BOOTSTRAP_TRACKERS + FALLBACK_TRACKERS, on)
    assertEquals(BOOTSTRAP_TRACKERS.size + FALLBACK_TRACKERS.size, on.size)
    assertEquals(10, on.size)
  }

  @Test
  fun enabledPath_keepsBootstrapPrefixInOrder() {
    val on = trackersFor(true)
    // 前 5 条必须原样、原序 —— 桌面是 `BOOTSTRAP_TRACKERS + FALLBACK_TRACKERS`，
    // 顺序一致才能「只多不少」地覆盖既有发现面。
    assertEquals(BOOTSTRAP_TRACKERS, on.take(BOOTSTRAP_TRACKERS.size))
    assertTrue("追加部分必须整体接在基线之后", on.takeLast(FALLBACK_TRACKERS.size) == FALLBACK_TRACKERS)
  }

  @Test
  fun togglingChangesTheList() {
    assertNotEquals("开关必须真的改变注入面", trackersFor(false), trackersFor(true))
  }

  // ------------------------------------------------------------ §C 通道互补性

  @Test
  fun fallback_isHttpOrHttps() {
    assertTrue(
      "兜底组必须走 TCP（http/https）—— 它的存在前提就是 UDP 出站受阻",
      FALLBACK_TRACKERS.all { it.startsWith("http://") || it.startsWith("https://") },
    )
    assertFalse(
      "兜底组里不该混入 udp://",
      FALLBACK_TRACKERS.any { it.startsWith("udp://") },
    )
  }

  @Test
  fun fallback_doesNotOverlapBootstrap() {
    val dup = FALLBACK_TRACKERS.intersect(BOOTSTRAP_TRACKERS.toSet())
    assertTrue("两组不得重叠（重叠 = 同一条通道发两次，无冗余价值）：$dup", dup.isEmpty())
  }

  @Test
  fun combinedListHasNoDuplicates() {
    val on = trackersFor(true)
    assertEquals("合并后不得有重复项", on.size, on.toSet().size)
  }

  // ------------------------------------------------------------ §D 纯函数性

  @Test
  fun repeatedCalls_areStable() {
    assertEquals(trackersFor(true), trackersFor(true))
    assertEquals(trackersFor(false), trackersFor(false))
  }

  @Test
  fun sourceConstants_areNotMutated() {
    val bBefore = BOOTSTRAP_TRACKERS.toList()
    val fBefore = FALLBACK_TRACKERS.toList()
    trackersFor(true)
    trackersFor(false)
    assertEquals("BOOTSTRAP_TRACKERS 不得被调用方改写", bBefore, BOOTSTRAP_TRACKERS)
    assertEquals("FALLBACK_TRACKERS 不得被调用方改写", fBefore, FALLBACK_TRACKERS)
  }

  @Test
  fun fallbackIsNotEmpty() {
    assertTrue("兜底组不能是空表，否则开关形同虚设", FALLBACK_TRACKERS.isNotEmpty())
  }
}
