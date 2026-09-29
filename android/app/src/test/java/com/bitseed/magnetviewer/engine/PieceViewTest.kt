package com.bitseed.magnetviewer.engine

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * S6 增量可用性视图的对拍护栏（桌面 `pieceview_test.py` 判据的 JVM 移植）。
 *
 * 覆盖六条硬性质：
 * 1. **对拍**：随机演进下 `contiguousFrom` / `rangeReadyPieces` 与暴力扫描逐值一致；
 * 2. 失效**电平**（totalDone 下降）触发基线重建——没有它锚点会返回陈旧结论；
 * 3. 失效**边沿**（进入 checking）只重建**一次**——电平触发会让长期 checking 的
 *    种子每轮重建（桌面 D5 探针踩过的坑），必须锁死；
 * 4. 推进触顶 [PieceView.MAX_ADVANCE] → 强制重建并给出正确结论（绝不把截断当就绪）；
 * 5. 锚点摊销：稳态下每次调用的单查增量是 O(新增块) 而非 O(前缀)；
 * 6. 保守退化：标量不可判定跳过自检、基线物化失败只用「下限损失」不误判、
 *    块数为 0 直接判不可判定。
 */
class PieceViewTest {

  /** 假探针：真值数组 + 演进控制 + 单查计数。 */
  private class FakeProbe(
    override val identity: Any = Object(),
    val n: Int = 200,
  ) : PieceView.PieceProbe {
    /** 实时真值（测试演进它；只允许 false→true，符合「块恒 have」不变量）。 */
    val truth = BooleanArray(n)

    /** 基线将看到的位图（默认与 truth 同步；测试可故意置陈旧）。 */
    var baselineTruth: BooleanArray? = null

    /** 模拟「首次基线陈旧、重建时拿到实时位图」——触顶用例用。 */
    var staleFirstBaseline: Boolean = false

    var totalDone: Long = 0
    var checking: Boolean = false

    /** 标量是否可判定（模拟句柄失效等场景）。 */
    var scalarsAvailable: Boolean = true

    var haveCalls: Int = 0
      private set

    var baselineCalls: Int = 0
      private set

    /** 演进后同步 total_done（模拟 libtorrent 的 total_done 同步增长）。 */
    fun syncTotalDone() {
      totalDone = truth.count { it }.toLong()
    }

    /** 模拟「块退回 not-have」+ total_done 下降（失效电平的真实来源）。 */
    fun regress(piece: Int) {
      truth[piece] = false
      totalDone -= 1
    }

    override fun have(piece: Int): Boolean {
      haveCalls++
      return truth[piece]
    }

    override fun scalars(): PieceView.Scalars? =
      if (!scalarsAvailable) {
        null
      } else {
        PieceView.Scalars(totalDone, checking)
      }

    override fun baseline(): PieceView.Baseline? {
      baselineCalls++
      // staleFirstBaseline：首次物化给陈旧位图（模拟句柄被换过 / 基线异常），
      // 重建（第 2 次）起给实时位图——对应真实场景：重建走 snapshotOf(handle) 的实时快照。
      val b =
        if (staleFirstBaseline && baselineCalls <= 1) {
          BooleanArray(n)
        } else {
          baselineTruth ?: truth
        }
      return object : PieceView.Baseline {
        override val size: Int = n
        override fun have(piece: Int): Boolean = b[piece]
      }
    }
  }

  // ---------- 1. 对拍：随机演进下与暴力扫描逐值一致 ----------

  @Test
  fun contiguousAndRange_matchBruteforce_acrossMonotonicEvolution() {
    val probe = FakeProbe(n = 160)
    val view = PieceView(probe, probe.n)
    val rng = java.util.Random(20260927L)
    // 演进前先建一次基线（陈旧基线只允许损失下限，不允许损失正确性）
    probe.baselineTruth = probe.truth.copyOf()
    for (step in 0 until 240) {
      // 随机把 1~5 个仍是 false 的块置 true（单调演进）
      repeat(1 + rng.nextInt(5)) {
        val p = rng.nextInt(probe.n)
        probe.truth[p] = true
      }
      probe.syncTotalDone()
      // 多起点对拍：0 / 中部 / 末端附近
      for (start in intArrayOf(0, 37, 100, 159)) {
        val limit = start + rng.nextInt(probe.n - start + 1)
        var expect = start
        while (expect < limit && probe.truth[expect]) expect++
        assertEquals(
          "step=$step contiguousFrom($start,$limit) 与暴力扫描不一致",
          expect,
          view.contiguousFrom(start, limit),
        )
      }
      // rangeReadyPieces 对拍：随机小区间
      val a = rng.nextInt(probe.n)
      val b = a + rng.nextInt(probe.n - a)
      var expectReady = true
      for (p in a..b) {
        if (!probe.truth[p]) {
          expectReady = false
          break
        }
      }
      assertEquals(
        "step=$step rangeReadyPieces($a,$b) 与暴力扫描不一致",
        expectReady,
        view.rangeReadyPieces(a, b),
      )
    }
  }

  // ---------- 2. 失效电平：totalDone 下降必须作废锚点 ----------

  @Test
  fun totalDoneShrink_invalidatesStaleAnchor() {
    val probe = FakeProbe(n = 64)
    val view = PieceView(probe, probe.n)
    for (p in 0 until 5) probe.truth[p] = true
    probe.syncTotalDone()
    assertEquals(5, view.contiguousFrom(0, probe.n))
    val baselineCountBefore = view.baselineCount

    // 块 2 退回 not-have 且 total_done 下降（真实来源：删除文件/校验坏块）
    probe.regress(2)
    assertEquals(
      "totalDone 下降后必须重建并给出实时结论（锚点 5 是陈旧的）",
      2,
      view.contiguousFrom(0, probe.n),
    )
    assertTrue(
      "失效必须触发基线重建（$baselineCountBefore -> ${view.baselineCount}）",
      view.baselineCount > baselineCountBefore,
    )
  }

  // ---------- 3. 失效边沿：进入 checking 只重建一次 ----------

  @Test
  fun checkingEdge_invalidatesOnce_notPerCall() {
    val probe = FakeProbe(n = 64)
    val view = PieceView(probe, probe.n)
    probe.checking = false
    probe.syncTotalDone()
    view.contiguousFrom(0, probe.n)
    val before = view.baselineCount

    // 边沿：非 checking → checking（触发一次）
    probe.checking = true
    view.contiguousFrom(0, probe.n)
    assertEquals("进入 checking 必须重建基线", before + 1, view.baselineCount)

    // 电平场景：连续多轮仍处于 checking —— 绝不能再重建（桌面 D5 坑）
    repeat(10) {
      probe.syncTotalDone()
      view.contiguousFrom(0, probe.n)
    }
    assertEquals(
      "长期处于 checking 不得反复重建（电平触发 = 每轮物化，比不优化更糟）",
      before + 1,
      view.baselineCount,
    )
  }

  // ---------- 4. 推进触顶：强制重建并给正确结论 ----------

  @Test
  fun advanceCap_forcesRebuild_andNeverTruncates() {
    val probe = FakeProbe(n = 5000)
    val view = PieceView(probe, probe.n)
    // 首次基线全 False（陈旧），真值全 have：推进必然超过 MAX_ADVANCE；
    // 重建时探针给出实时位图（对应真实场景 snapshotOf(handle)）→ 完整前缀。
    probe.staleFirstBaseline = true
    for (p in 0 until probe.n) probe.truth[p] = true
    probe.syncTotalDone()
    val got = view.contiguousFrom(0, probe.n)
    assertEquals(
      "触顶后必须重建基线并给出完整前缀（绝不把截断当「就绪到此为止」）",
      probe.n,
      got,
    )
  }

  // ---------- 5. 锚点摊销：稳态单查增量 O(新增块) ----------

  @Test
  fun anchorReuse_amortizesProbeCalls() {
    val probe = FakeProbe(n = 300)
    val view = PieceView(probe, probe.n)
    for (p in 0 until 100) probe.truth[p] = true
    probe.syncTotalDone()
    assertEquals(100, view.contiguousFrom(0, probe.n))
    val afterFirst = probe.haveCalls

    // 无演进重查：基线下限直接命中锚点，只允许 ≤2 次「首缺探测」
    assertEquals(100, view.contiguousFrom(0, probe.n))
    assertTrue(
      "无演进重查不应重扫前缀（haveCalls ${afterFirst}->${probe.haveCalls}）",
      probe.haveCalls - afterFirst <= 2,
    )

    // 推进 1 块：只允许 O(1) 次新增单查
    probe.truth[100] = true
    probe.syncTotalDone()
    assertEquals(101, view.contiguousFrom(0, probe.n))
    assertTrue(
      "推进 1 块只应单查 1~2 次（haveCalls ${afterFirst}->${probe.haveCalls}）",
      probe.haveCalls - afterFirst <= 4,
    )
  }

  // ---------- 6. 保守退化 ----------

  @Test
  fun undecidableScalars_skipInvalidation() {
    val probe = FakeProbe(n = 64)
    val view = PieceView(probe, probe.n)
    for (p in 0 until 8) probe.truth[p] = true
    probe.syncTotalDone()
    assertEquals(8, view.contiguousFrom(0, probe.n))
    probe.scalarsAvailable = false // 标量不可判定（句柄失效等）：跳过自检，绝不误判失效
    assertEquals(
      "标量不可判定时不得作废基线（宁可依赖显式 invalidate）",
      8,
      view.contiguousFrom(0, probe.n),
    )
  }

  @Test
  fun baselineUnavailable_stillCorrectViaLiveProbe() {
    val probe = FakeProbe(n = 64)
    val view = PieceView(probe, probe.n)
    for (p in 0 until 10) probe.truth[p] = true
    probe.syncTotalDone()
    // 基线物化失败：全 False 下限只损失效率；结论必须仍来自实时单查
    val failing = object : PieceView.PieceProbe by probe {
      override fun baseline(): PieceView.Baseline? = null
    }
    val v2 = PieceView(failing, probe.n)
    assertEquals(10, v2.contiguousFrom(0, probe.n))
  }

  @Test
  fun zeroPieces_undecidable() {
    val probe = FakeProbe(n = 0)
    val view = PieceView(probe, 0)
    assertNull("块数为 0 必须判不可判定（保守）", view.contiguousFrom(0, probe.n))
  }

  @Test
  fun identityChange_notMatches() {
    val probeA = FakeProbe()
    val probeB = FakeProbe()
    val view = PieceView(probeA, probeA.n)
    assertTrue(view.matches(probeA))
    assertFalse("句柄换代（identity 变化）必须判不匹配，防串台", view.matches(probeB))
  }
}
