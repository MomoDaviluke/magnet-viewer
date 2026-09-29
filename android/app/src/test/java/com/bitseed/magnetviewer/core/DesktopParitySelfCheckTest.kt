package com.bitseed.magnetviewer.core

import com.bitseed.magnetviewer.core.model.PieceMap
import com.bitseed.magnetviewer.core.model.TorrentFile
import com.bitseed.magnetviewer.core.model.contiguousBytes
import com.bitseed.magnetviewer.core.model.humanSize
import com.bitseed.magnetviewer.core.model.rangeAvailable
import com.bitseed.magnetviewer.core.model.readyUntil
import com.bitseed.magnetviewer.core.model.safeRelPath
import com.bitseed.magnetviewer.core.sched.tailEntryPieces
import com.bitseed.magnetviewer.core.sched.tailPieceWindow
import com.bitseed.magnetviewer.core.sched.tailWindowBytes
import com.bitseed.magnetviewer.core.sched.windowPieces
import com.bitseed.magnetviewer.core.states.BOOTSTRAP_TRACKERS
import com.bitseed.magnetviewer.core.states.FALLBACK_TRACKERS
import com.bitseed.magnetviewer.core.states.DOWNLOAD_STATES
import com.bitseed.magnetviewer.core.states.STATE_COMPLETED
import com.bitseed.magnetviewer.core.states.STATE_DELETED
import com.bitseed.magnetviewer.core.states.STATE_DOWNLOADING
import com.bitseed.magnetviewer.core.states.STATE_FAILED
import com.bitseed.magnetviewer.core.states.STATE_META_FETCH
import com.bitseed.magnetviewer.core.states.STATE_PAUSED
import com.bitseed.magnetviewer.core.states.STATE_QUEUED
import com.bitseed.magnetviewer.core.states.STATE_SEEDING
import com.bitseed.magnetviewer.core.states.STATE_STOPPED
import com.bitseed.magnetviewer.core.states.STATE_VALIDATE
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonNull
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.boolean
import kotlinx.serialization.json.int
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import kotlinx.serialization.json.long
import org.junit.Assert.assertEquals
import org.junit.Test

/**
 * 跨语言对拍自检：夹具 `parity/parity.json` 的每个 expect 都是**真实调用桌面
 * `core/` 实现**得到的返回值（唯一真相源），这里逐条断言 Kotlin 侧同名纯函数一致。
 *
 * 夹具由 `android/tools/gen_parity_fixtures.py` 生成（本测试只读不改）。
 */
class DesktopParitySelfCheckTest {

  private val cases: JsonObject by lazy {
    val stream = javaClass.classLoader!!.getResourceAsStream("parity/parity.json")
      ?: error("夹具缺失：app/src/test/resources/parity/parity.json")
    Json.parseToJsonElement(stream.readBytes().decodeToString()).jsonObject["cases"]!!.jsonObject
  }

  private fun group(name: String): JsonArray =
    cases[name]?.jsonArray ?: error("夹具里没有分组 $name（先跑 gen_parity_fixtures.py）")

  @Test
  fun windowPieces_matchesDesktop() {
    for (c in group("window_pieces")) {
      val pl = c.jsonObject["input"]!!.jsonObject["piece_length"]!!.jsonPrimitive.int
      val expect = c.jsonObject["expect"]!!.jsonPrimitive.int
      assertEquals("windowPieces($pl)", expect, windowPieces(pl))
    }
  }

  @Test
  fun tailWindowBytes_matchesDesktop() {
    for (c in group("tail_window_bytes")) {
      val size = c.jsonObject["input"]!!.jsonObject["size"]!!.jsonPrimitive.long
      val expect = c.jsonObject["expect"]!!.jsonPrimitive.long
      assertEquals("tailWindowBytes($size)", expect, tailWindowBytes(size))
    }
  }

  @Test
  fun tailEntryPieces_matchesDesktop() {
    for (c in group("tail_entry_pieces")) {
      val o = c.jsonObject
      val inp = o["input"]!!.jsonObject
      val pl = inp["piece_length"]!!.jsonPrimitive.int
      val fileEl = inp["file"]
      val file = if (fileEl == null || fileEl is JsonNull) null else fileOf(fileEl.jsonObject)
      val expect = o["expect"]!!.jsonArray.map { it.jsonPrimitive.int }
      assertEquals("tailEntryPieces(${inp}, $pl)", expect, tailEntryPieces(file, pl))
    }
  }

  @Test
  fun tailPieceWindow_matchesDesktop() {
    for (c in group("tail_piece_window")) {
      val o = c.jsonObject
      val inp = o["input"]!!.jsonObject
      val pl = inp["piece_length"]!!.jsonPrimitive.int
      val file = fileOf(inp["file"]!!.jsonObject)
      val expect = o["expect"]!!.jsonArray.map { it.jsonPrimitive.int }
      assertEquals("tailPieceWindow(${inp}, $pl)", expect, tailPieceWindow(file, pl))
    }
  }

  @Test
  fun contiguousBytes_matchesDesktop() {
    for (c in group("contiguous_bytes")) {
      val inp = c.jsonObject["input"]!!.jsonObject
      val limit = if (inp["limit"] == null || inp["limit"] is JsonNull) null else inp["limit"]!!.jsonPrimitive.long
      val expect = c.jsonObject["expect"]!!.jsonPrimitive.long
      assertEquals("contiguousBytes($inp, limit=$limit)", expect, contiguousBytes(pieceMapOf(inp), limit))
    }
  }

  @Test
  fun readyUntil_matchesDesktop() {
    for (c in group("ready_until")) {
      val inp = c.jsonObject["input"]!!.jsonObject
      val start = inp["start"]!!.jsonPrimitive.long
      val endExcl = inp["end_excl"]!!.jsonPrimitive.long
      val expect = c.jsonObject["expect"]!!.jsonPrimitive.long
      assertEquals("readyUntil($inp)", expect, readyUntil(pieceMapOf(inp), start, endExcl))
    }
  }

  @Test
  fun rangeAvailable_matchesDesktop() {
    for (c in group("range_available")) {
      val inp = c.jsonObject["input"]!!.jsonObject
      val start = inp["start"]!!.jsonPrimitive.long
      val endExcl = inp["end_excl"]!!.jsonPrimitive.long
      val expect = c.jsonObject["expect"]!!.jsonPrimitive.boolean
      assertEquals("rangeAvailable($inp)", expect, rangeAvailable(pieceMapOf(inp), start, endExcl))
    }
  }

  @Test
  fun humanSize_matchesDesktop() {
    for (c in group("human_size")) {
      val n = c.jsonObject["input"]!!.jsonPrimitive.long
      val expect = c.jsonObject["expect"]!!.jsonPrimitive.content
      assertEquals("humanSize($n)", expect, humanSize(n))
    }
  }

  @Test
  fun safeRelPath_matchesDesktop() {
    for (c in group("safe_rel_path")) {
      val segs = c.jsonObject["input"]!!.jsonArray.map { it.jsonPrimitive.content }.toTypedArray()
      val expect = c.jsonObject["expect"]!!.jsonPrimitive.content
      assertEquals("safeRelPath(${segs.toList()})", expect, safeRelPath(*segs))
    }
  }

  @Test
  fun stateConstants_matchDesktop() {
    assertEquals(DOWNLOAD_STATES.sorted(), group("download_states")[0].jsonObject["expect"]!!.jsonArray.map { it.jsonPrimitive.content }.sorted())
    assertEquals(BOOTSTRAP_TRACKERS.sorted(), group("bootstrap_trackers")[0].jsonObject["expect"]!!.jsonArray.map { it.jsonPrimitive.content }.sorted())
    // 诊断 D-2026-09-18：兜底 tracker（extra_trackers 开关的追加通道）同样逐条对拍 ——
    // 它是「UDP 受阻时多一条发现通道」的全部内容，两边漂移就等于行为不同。
    assertEquals(FALLBACK_TRACKERS.sorted(), group("fallback_trackers")[0].jsonObject["expect"]!!.jsonArray.map { it.jsonPrimitive.content }.sorted())
    val table =
      mapOf(
        "STATE_QUEUED" to STATE_QUEUED,
        "STATE_META_FETCH" to STATE_META_FETCH,
        "STATE_VALIDATE" to STATE_VALIDATE,
        "STATE_DOWNLOADING" to STATE_DOWNLOADING,
        "STATE_PAUSED" to STATE_PAUSED,
        "STATE_COMPLETED" to STATE_COMPLETED,
        "STATE_STOPPED" to STATE_STOPPED,
        "STATE_FAILED" to STATE_FAILED,
        "STATE_SEEDING" to STATE_SEEDING,
        "STATE_DELETED" to STATE_DELETED,
        "STATE_READY" to com.bitseed.magnetviewer.core.states.STATE_READY,
      )
    for (c in group("state_constants")) {
      val name = c.jsonObject["input"]!!.jsonObject["name"]!!.jsonPrimitive.content
      val expect = c.jsonObject["expect"]!!.jsonPrimitive.content
      assertEquals(name, expect, table[name])
    }
  }

  // ------------------------------------------------------------ 夹具 → 对象

  private fun fileOf(o: JsonObject): TorrentFile =
    TorrentFile(
      index = 0,
      path = "root/movie" + o["ext"]!!.jsonPrimitive.content,
      size = o["size"]!!.jsonPrimitive.long,
      offset = o["offset"]!!.jsonPrimitive.long,
      startPiece = o["start_piece"]!!.jsonPrimitive.int,
      endPiece = o["end_piece"]!!.jsonPrimitive.int,
    )

  private fun pieceMapOf(inp: JsonObject): PieceMap {
    val have = inp["have_pieces"]!!.jsonArray.map { it.jsonPrimitive.int }.toSet()
    return PieceMap(
      pieceLength = inp["piece_length"]!!.jsonPrimitive.int,
      offset = inp["offset"]!!.jsonPrimitive.long,
      startPiece = inp["start_piece"]!!.jsonPrimitive.int,
      endPiece = inp["end_piece"]!!.jsonPrimitive.int,
      size = inp["size"]!!.jsonPrimitive.long,
    ) { p -> p in have }
  }
}
