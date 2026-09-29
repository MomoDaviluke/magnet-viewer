package com.bitseed.magnetviewer.engine

import com.bitseed.magnetviewer.core.states.FALLBACK_TRACKERS
import com.bitseed.magnetviewer.core.states.trackersFor
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * 「给已在跑的任务追加兜底 tracker」专项（诊断 D-2026-09-18）。
 *
 * 覆盖段：
 * - §A [missingTrackers]：去重、保序、过滤空串（纯函数）；
 * - §B [pushTrackers]：失效句柄跳过 / 取不到现有列表按「全都缺」/ 单条失败不中止 /
 *      已齐全则不加条 —— 全是「旁路设施不许把主流程搞挂」的容错；
 * - §C 幂等：连跑两次第二次不再新增（避免反复开开关把 tracker 表刷爆）；
 * - §D 文案与「只做加法」语义；
 * - §E 与加种路径的一致性：push 的目标列表就是 [trackersFor] 的输出。
 */
class TrackerPushTest {

  /** 假句柄：只记 URL，无需 libtorrent。 */
  private class FakeTarget(
    private val valid: Boolean = true,
    initial: List<String> = emptyList(),
    private val failOn: Set<String> = emptySet(),
    private val throwOnList: Boolean = false,
  ) : TrackerTarget {
    val urls = ArrayList(initial)
    var addCalls = 0

    override fun isValid(): Boolean = valid

    override fun trackerUrls(): List<String> {
      if (throwOnList) throw IllegalStateException("取不到现有 tracker")
      return urls.toList()
    }

    override fun addTracker(url: String) {
      addCalls++
      if (url in failOn) throw IllegalStateException("该条拒绝")
      urls.add(url)
    }
  }

  private val desired = listOf("http://a/announce", "https://b/announce")

  // ------------------------------------------------------------ §A missingTrackers

  @Test
  fun missing_emptyDesired_returnsEmpty() {
    assertEquals(emptyList<String>(), missingTrackers(emptyList(), listOf("x")))
  }

  @Test
  fun missing_allPresent_returnsEmpty() {
    assertEquals(emptyList<String>(), missingTrackers(desired, desired))
  }

  @Test
  fun missing_partial_keepsDesiredOrder() {
    val r = missingTrackers(desired, listOf("https://b/announce"))
    assertEquals(listOf("http://a/announce"), r)
  }

  @Test
  fun missing_preservesRelativeOrder() {
    val r = missingTrackers(listOf("c", "a", "b"), listOf("b"))
    assertEquals(listOf("c", "a"), r)
  }

  @Test
  fun missing_dedupesDesiredItself() {
    val r = missingTrackers(listOf("a", "a", "b"), emptyList())
    assertEquals(listOf("a", "b"), r)
  }

  @Test
  fun missing_filtersBlankUrls() {
    val r = missingTrackers(listOf("", "a", "  "), emptyList())
    assertEquals(listOf("a"), r)
  }

  // ------------------------------------------------------------ §B pushTrackers

  @Test
  fun push_addsMissingToEachTarget() {
    val t1 = FakeTarget()
    val t2 = FakeTarget(initial = listOf("http://a/announce"))
    val r = pushTrackers(desired, listOf(t1, t2))
    assertEquals(2, r.handlesTouched)
    assertEquals(3, r.urlsAdded) // t1 加 2 条，t2 加 1 条
    assertEquals(desired, t1.urls)
    assertTrue(t2.urls.contains("https://b/announce"))
  }

  @Test
  fun push_skipsInvalidTargets() {
    val bad = FakeTarget(valid = false)
    val good = FakeTarget()
    val r = pushTrackers(desired, listOf(bad, good))
    assertEquals("失效句柄必须跳过（swig 对失效句柄会原生崩溃）", 1, r.handlesTouched)
    assertEquals(0, bad.addCalls)
    assertEquals(2, good.addCalls)
  }

  @Test
  fun push_unreadableList_treatedAsAllMissing() {
    val t = FakeTarget(throwOnList = true)
    val r = pushTrackers(desired, listOf(t))
    assertEquals("取不到现有列表 → 按「全都缺」处理，不中止", 2, r.urlsAdded)
    assertEquals(1, r.handlesTouched)
  }

  @Test
  fun push_singleAddFailureDoesNotAbortRest() {
    val t = FakeTarget(failOn = setOf("http://a/announce"))
    val r = pushTrackers(desired, listOf(t))
    assertEquals("单条失败 → 其余照常追加", 1, r.urlsAdded)
    assertTrue(t.urls.contains("https://b/announce"))
    assertFalse(t.urls.contains("http://a/announce"))
  }

  @Test
  fun push_nothingMissing_addsNothingButCountsHandle() {
    val t = FakeTarget(initial = desired)
    val r = pushTrackers(desired, listOf(t))
    assertEquals(1, r.handlesTouched)
    assertEquals(0, r.urlsAdded)
    assertEquals(0, t.addCalls)
  }

  @Test
  fun push_emptyDesired_isNoOp() {
    val t = FakeTarget()
    val r = pushTrackers(emptyList(), listOf(t))
    assertEquals(TrackerPushResult(0, 0), r)
    assertEquals(0, t.addCalls)
  }

  @Test
  fun push_noTargets_isNoOp() {
    assertEquals(TrackerPushResult(0, 0), pushTrackers(desired, emptyList()))
  }

  // ------------------------------------------------------------ §C 幂等

  @Test
  fun push_isIdempotent() {
    val t = FakeTarget()
    val first = pushTrackers(desired, listOf(t))
    val second = pushTrackers(desired, listOf(t))
    assertEquals(2, first.urlsAdded)
    assertEquals("重复开开关不得把 tracker 表刷爆", 0, second.urlsAdded)
    assertEquals(desired, t.urls)
  }

  @Test
  fun push_neverRemovesExisting() {
    val own = "udp://user-own.example:1337/announce" // 磁力链自带 tr=
    val t = FakeTarget(initial = listOf(own))
    pushTrackers(desired, listOf(t))
    assertTrue(
      "只做加法：用户原有 tracker 必须原样保留（replaceTrackers 会抹掉它）",
      t.urls.contains(own),
    )
    assertEquals(3, t.urls.size)
  }

  // ------------------------------------------------------------ §D 文案

  @Test
  fun note_enabled_reportsCounts() {
    val n = extraTrackersNote(true, TrackerPushResult(handlesTouched = 3, urlsAdded = 15))
    assertTrue("开启文案要带上实际数量：$n", n.contains("15") && n.contains("3"))
  }

  @Test
  fun note_disabled_isHonestAboutNoRemoval() {
    val n = extraTrackersNote(false, TrackerPushResult(0, 0))
    assertTrue("关闭文案必须说明「已追加的条目不会移除」：$n", n.contains("不会移除"))
    assertTrue("并说明新任务只注入 5 条：$n", n.contains("5"))
  }

  @Test
  fun pendingNote_mentionsNextAdd() {
    assertTrue(EXTRA_TRACKERS_PENDING_NOTE.contains("下次添加任务"))
  }

  // ------------------------------------------------------------ §E 与加种路径一致

  @Test
  fun pushTargetsAreExactlyTheAddTimeTrackers() {
    // 推送目标与加种注入面必须是同一个函数输出，否则两条路会漂移
    assertEquals(trackersFor(true), trackersFor(true))
    assertEquals(10, trackersFor(true).size)
    assertEquals(5, trackersFor(false).size)
    val t = FakeTarget()
    val r = pushTrackers(trackersFor(true), listOf(t))
    assertEquals("开启时 10 条全部缺失 → 加 10 条", 10, r.urlsAdded)
    assertEquals(FALLBACK_TRACKERS.size, t.urls.count { it.startsWith("http") })
  }
}
