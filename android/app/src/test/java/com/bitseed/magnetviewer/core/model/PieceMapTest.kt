package com.bitseed.magnetviewer.core.model

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * PieceMap 位图/连续字节区间计算的回归护栏（JVM 纯逻辑，桌面 `core/models.py` 同构）。
 *
 * 背景：`contiguousBytes / readyUntil / rangeAvailable` 是流式播放「能读到哪」的唯一判据，
 * S6 之后又加了增量视图派发层（`*Of`）——**锚点换算公式**（块号 → 文件内字节）
 * 两条路径必须逐值一致，否则快路径会把「没下完」误判成「已就绪」（播放花屏/读零）。
 * 本文件锁三件事：
 * 1. 位图纯函数路径的边界语义（全空/全满/稀疏/夹取/退化输入）；
 * 2. 派发层对视图的**信任边界**：可判定才用、不可判定必须退回纯函数、
 *    守卫分支不得触碰视图（防「把不可判定当成已就绪」）；
 * 3. 随机演进下两条路径逐值对拍（共用换算公式的回归钉）。
 */
class PieceMapTest {

  private val PL = 16384 // 16 KB 块

  /** 构造：文件从全局字节 offset 起、长 size，覆盖块 [startPiece, endPiece]。 */
  private fun pieceMap(
    offset: Long,
    size: Long,
    startPiece: Int,
    endPiece: Int,
    pieceLength: Int = PL,
    have: (Int) -> Boolean,
  ) = PieceMap(pieceLength = pieceLength, offset = offset, startPiece = startPiece, endPiece = endPiece, size = size, have = have)

  /** 可控视图桩：记录调用参数，答案可设（null = 不可判定）。 */
  private class StubView : IncrementalAvailability {
    var contiguousAnswer: Int? = null
    var rangeAnswer: Boolean? = null
    var contiguousCalls = 0
    var rangeCalls = 0
    var lastStart = -1
    var lastLimit = -1
    var lastP0 = -1
    var lastP1 = -1

    override fun contiguousFrom(start: Int, limit: Int): Int? {
      contiguousCalls++
      lastStart = start
      lastLimit = limit
      return contiguousAnswer
    }

    override fun rangeReadyPieces(p0: Int, p1: Int): Boolean? {
      rangeCalls++
      lastP0 = p0
      lastP1 = p1
      return rangeAnswer
    }
  }

  // ---------------------------------------------------------------- contiguousBytes（位图路径）

  @Test
  fun contiguousBytes_allPiecesHave_clampsToSize() {
    // 文件 5000..25000（offset 在块 0 中间，跨块 0/1）：全 have 时按 size 夹取，不得溢出
    val pm = pieceMap(offset = 5000L, size = 20000L, startPiece = 0, endPiece = 1) { true }
    assertEquals(20000L, contiguousBytes(pm))
  }

  @Test
  fun contiguousBytes_noPieceHave_isZero_notNegative() {
    // 块 0 缺失：contig = 0*PL - offset 为负 → 必须夹到 0（防负数进度）
    val pm = pieceMap(offset = 5000L, size = 20000L, startPiece = 0, endPiece = 1) { false }
    assertEquals(0L, contiguousBytes(pm))
  }

  @Test
  fun contiguousBytes_sparse_stopsAtFirstGap() {
    // 稀疏位图：块 0 有、块 1 缺、块 2 有 → 只算头部连续段（中途断即停），
    // 防「求和所有 have 块」把未落盘的稀疏零数据当可读
    val pm = pieceMap(offset = 0L, size = 40000L, startPiece = 0, endPiece = 2) { it != 1 }
    assertEquals("断点 = 块 1 起点", PL.toLong(), contiguousBytes(pm))
  }

  @Test
  fun contiguousBytes_limitClampsBelowSize() {
    // 流式播放按「播放头前方一小段」问可用性：limit 必须参与夹取
    val pm = pieceMap(offset = 0L, size = 40000L, startPiece = 0, endPiece = 2) { true }
    assertEquals(12345L, contiguousBytes(pm, limit = 12345L))
    assertEquals("limit 超过 size 时仍按 size 夹", 40000L, contiguousBytes(pm, limit = 999999L))
  }

  @Test
  fun contiguousBytes_degenerateInputs_isZero() {
    // 空文件 / 负 size / 非法块长（0 或负）→ 一律 0，不得除零或负数
    assertEquals(0L, contiguousBytes(pieceMap(0L, 0L, 0, 0) { true }))
    assertEquals(0L, contiguousBytes(pieceMap(0L, -5L, 0, 0) { true }))
    assertEquals(0L, contiguousBytes(pieceMap(0L, 100L, 0, 0, pieceLength = 0) { true }))
    assertEquals(0L, contiguousBytes(pieceMap(0L, 100L, 0, 0, pieceLength = -1) { true }))
  }

  @Test
  fun contiguousBytes_fileExactlyOnPieceBoundary() {
    // 整块对齐的文件（offset=PL、size=PL）：全 have 恰好一个 PL，不多不少
    val pm = pieceMap(offset = PL.toLong(), size = PL.toLong(), startPiece = 1, endPiece = 1) { true }
    assertEquals(PL.toLong(), contiguousBytes(pm))
    val pm2 = pieceMap(offset = PL.toLong(), size = PL.toLong(), startPiece = 1, endPiece = 1) { false }
    assertEquals(0L, contiguousBytes(pm2))
  }

  // ---------------------------------------------------------------- readyUntil（位图路径）

  @Test
  fun readyUntil_allReady_returnsEndExcl() {
    val pm = pieceMap(offset = 5000L, size = 20000L, startPiece = 0, endPiece = 1) { true }
    assertEquals(20000L, readyUntil(pm, 0L, 20000L))
  }

  @Test
  fun readyUntil_gap_stopsAtPieceBoundary() {
    // 块 1 缺失：从 0 起第一个未就绪字节 = 块 1 在文件内的位置 = PL - offset
    val pm = pieceMap(offset = 5000L, size = 20000L, startPiece = 0, endPiece = 1) { it == 0 }
    assertEquals((PL - 5000).toLong(), readyUntil(pm, 0L, 20000L))
  }

  @Test
  fun readyUntil_startMidPiece_firstByteNotReady_returnsStart() {
    // 起点落在缺失块中间：返回 start 本身（「该处一个就绪字节都没有」），不得倒退到块边界
    val pm = pieceMap(offset = 5000L, size = 20000L, startPiece = 0, endPiece = 1) { false }
    assertEquals(15000L, readyUntil(pm, 15000L, 20000L))
  }

  @Test
  fun readyUntil_guardClauses_returnStart() {
    // 空区间 / 越界 / 非法块长：保守返回 start（调用方据此知道「此处不可读」）
    val pm = pieceMap(offset = 0L, size = 20000L, startPiece = 0, endPiece = 1) { true }
    assertEquals(5L, readyUntil(pm, 5L, 5L))
    assertEquals(5L, readyUntil(pm, 5L, 4L))
    assertEquals("越出文件大小必须保守拒绝", 0L, readyUntil(pm, 0L, 20001L))
    assertEquals(0L, readyUntil(pieceMap(0L, 100L, 0, 0, pieceLength = 0) { true }, 0L, 100L))
  }

  // ---------------------------------------------------------------- rangeAvailable（位图路径）

  @Test
  fun rangeAvailable_emptyRange_isTrue() {
    // 空区间「无字节需要」→ 恒真（endExcl<=0 与 start>=endExcl 两个分支都要钉住）
    val pm = pieceMap(offset = 0L, size = 100L, startPiece = 0, endPiece = 0) { false }
    assertTrue(rangeAvailable(pm, 0L, 0L))
    assertTrue(rangeAvailable(pm, 10L, 3L))
    assertTrue(rangeAvailable(pm, 0L, -1L))
  }

  @Test
  fun rangeAvailable_outOfBounds_isFalse() {
    val pm = pieceMap(offset = 0L, size = 20000L, startPiece = 0, endPiece = 1) { true }
    assertFalse("越出文件大小一律不可用", rangeAvailable(pm, 0L, 20001L))
    assertFalse(rangeAvailable(pieceMap(0L, 100L, 0, 0, pieceLength = 0) { true }, 0L, 100L))
  }

  @Test
  fun rangeAvailable_anyMissingPiece_isFalse() {
    val pm = pieceMap(offset = 0L, size = 40000L, startPiece = 0, endPiece = 2) { it != 1 }
    assertFalse(rangeAvailable(pm, 0L, 40000L))
    assertTrue("只问连续可用的子段", rangeAvailable(pm, 0L, PL.toLong()))
    assertFalse("子段踩到缺块 1", rangeAvailable(pm, 0L, PL.toLong() + 1))
  }

  // ---------------------------------------------------------------- 派发层：视图可判定才用

  @Test
  fun contiguousBytesOf_usesView_whenDecidable() {
    val pm = pieceMap(offset = 5000L, size = 20000L, startPiece = 0, endPiece = 1) { false }
    val v = StubView().apply { contiguousAnswer = 1 } // 视图说：块 0 起连续到块 1（不含）
    pm.view = v
    assertEquals(11384L, contiguousBytesOf(pm))
    assertEquals("必须按 [startPiece, endPiece+1) 问", 0, v.lastStart)
    assertEquals(2, v.lastLimit)
    // 视图给「全部就绪」时按 size 夹取
    v.contiguousAnswer = 2
    assertEquals(20000L, contiguousBytesOf(pm))
  }

  @Test
  fun contiguousBytesOf_fallsBackToBitmap_whenViewUndecidable() {
    // 防回归核心：不可判定 ≠ 已就绪，必须退回纯函数
    val pm = pieceMap(offset = 5000L, size = 20000L, startPiece = 0, endPiece = 1) { it == 0 }
    val v = StubView().apply { contiguousAnswer = null }
    pm.view = v
    assertEquals(contiguousBytes(pm), contiguousBytesOf(pm))
    assertEquals(1, v.contiguousCalls)
    // 无视图时同样走纯函数
    pm.view = null
    assertEquals(11384L, contiguousBytesOf(pm))
  }

  @Test
  fun contiguousBytesOf_guardBranches_neverTouchView() {
    // 空文件/非法块长：守卫直接返回 0，不得把视图答案当结论（防「不可判定当就绪」的另一半）
    val pm = pieceMap(offset = 0L, size = 0L, startPiece = 0, endPiece = 0) { true }
    val v = StubView().apply { contiguousAnswer = 999 }
    pm.view = v
    assertEquals(0L, contiguousBytesOf(pm))
    assertEquals(0, v.contiguousCalls)
  }

  @Test
  fun readyUntilOf_usesView_andClampsToWindow() {
    val pm = pieceMap(offset = 5000L, size = 20000L, startPiece = 0, endPiece = 1) { true }
    val v = StubView().apply { contiguousAnswer = 1 }
    pm.view = v
    assertEquals((PL - 5000).toLong(), readyUntilOf(pm, 0L, 20000L))
    assertEquals("按窗口字节算出的块号必须传给视图", 0, v.lastStart)
    assertEquals(2, v.lastLimit)
    // 视图说全就绪 → 夹到 endExcl，不得越过窗口
    v.contiguousAnswer = 5
    assertEquals(20000L, readyUntilOf(pm, 0L, 20000L))
    // 守卫分支不触碰视图
    v.contiguousCalls = 0
    assertEquals(3L, readyUntilOf(pm, 3L, 3L))
    assertEquals(0L, readyUntilOf(pm, 0L, 20001L))
    assertEquals(0, v.contiguousCalls)
  }

  @Test
  fun readyUntilOf_fallsBackToBitmap_whenViewUndecidable() {
    val pm = pieceMap(offset = 5000L, size = 20000L, startPiece = 0, endPiece = 1) { it == 0 }
    val v = StubView()
    pm.view = v
    assertEquals(readyUntil(pm, 0L, 20000L), readyUntilOf(pm, 0L, 20000L))
    assertEquals(1, v.contiguousCalls)
  }

  @Test
  fun rangeAvailableOf_usesView_andFallsBack() {
    val pm = pieceMap(offset = 5000L, size = 20000L, startPiece = 0, endPiece = 1) { false }
    val v = StubView().apply { rangeAnswer = true }
    pm.view = v
    assertTrue("视图可判定时以视图为准（快路径）", rangeAvailableOf(pm, 0L, 20000L))
    assertEquals(0, v.lastP0)
    assertEquals(1, v.lastP1)
    // 不可判定 → 退回位图（位图说缺块 → false），绝不因视图而放行
    v.rangeAnswer = null
    assertFalse(rangeAvailableOf(pm, 0L, 20000L))
    // 守卫分支不触碰视图
    v.rangeCalls = 0
    assertTrue(rangeAvailableOf(pm, 5L, 5L))
    assertFalse(rangeAvailableOf(pm, 0L, 20001L))
    assertEquals(0, v.rangeCalls)
  }

  @Test
  fun viewPath_agreesWithBitmapPath_randomly() {
    // 共用换算公式的回归钉：视图（暴力扫描同一位图）与纯函数必须逐值一致——
    // 任何一侧改了「块号 → 字节」公式（如忘了减 offset / 夹取方向反了）这里就红
    val truth = booleanArrayOf(false, false)
    val pm = pieceMap(offset = 5000L, size = 20000L, startPiece = 0, endPiece = 1) { truth[it] }
    pm.view = object : IncrementalAvailability {
      override fun contiguousFrom(start: Int, limit: Int): Int? {
        var p = start
        while (p < limit && truth[p]) p++
        return p
      }

      override fun rangeReadyPieces(p0: Int, p1: Int): Boolean? {
        for (p in p0..p1) if (!truth[p]) return false
        return true
      }
    }
    val rng = java.util.Random(20261009L)
    for (round in 0 until 60) {
      for (i in truth.indices) truth[i] = rng.nextBoolean()
      for (start in longArrayOf(0L, 15000L, 11384L)) {
        val endExcl = start + 1L + rng.nextInt((20000L - start).toInt())
        assertEquals("round=$round contiguous", contiguousBytes(pm), contiguousBytesOf(pm))
        assertEquals("round=$round readyUntil($start,$endExcl)", readyUntil(pm, start, endExcl), readyUntilOf(pm, start, endExcl))
        assertEquals("round=$round rangeAvailable($start,$endExcl)", rangeAvailable(pm, start, endExcl), rangeAvailableOf(pm, start, endExcl))
      }
    }
  }

  @Test
  fun view_undecidableIsNeverReady() {
    // 语义钉：接口文档承诺「null = 不可判定，调用方退回纯函数，绝不把不可判定当成已就绪」
    val pm = pieceMap(offset = 0L, size = 100L, startPiece = 0, endPiece = 0) { false }
    pm.view = object : IncrementalAvailability {
      override fun contiguousFrom(start: Int, limit: Int): Int? = null
      override fun rangeReadyPieces(p0: Int, p1: Int): Boolean? = null
    }
    assertNull(pm.view!!.contiguousFrom(0, 1))
    assertFalse("位图说缺块，视图不可判定 → 结果必须仍是不可用", rangeAvailableOf(pm, 0L, 100L))
    assertEquals(0L, contiguousBytesOf(pm))
  }
}
