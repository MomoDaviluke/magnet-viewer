package com.bitseed.magnetviewer.data

import java.io.File
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * 元数据缓存单测（JVM；**不实例化任何 libtorrent4j 对象**——解码在引擎侧 native）。
 *
 * 覆盖：路径约定（`.meta/<ih>.torrent`）、文件名校验、原子写往返、超上限拒绝、
 * 命中统计、LRU 淘汰、删除；以及**编码器最关键的两条性质**——
 * 字典键升序、info 段以**裸字节**插入（不是长度前缀字符串，否则 libtorrent
 * 反解会报 `missing or invalid 'info' section`）。
 */
class MetaCacheTest {

  private fun tempDir(tag: String): File {
    val d = File(System.getProperty("java.io.tmpdir"), "mv-meta-$tag-${System.nanoTime()}")
    d.mkdirs()
    return d
  }

  /** 一段「看起来像 info 段」的最小 bencode：`d4:name3:abc6:pieces0:e`。 */
  private fun sampleInfoSection(): ByteArray =
    "d6:lengthi1024e4:name3:abc12:piece lengthi16384e6:pieces0:e".toByteArray(Charsets.US_ASCII)

  @Test
  fun path_matchesDesktopLayout_andRejectsBadHash() {
    val cache = tempDir("path")
    assertEquals(File(cache, ".meta"), MetaCache.dir(cache))
    assertEquals(File(File(cache, ".meta"), "$IH.torrent"), MetaCache.path(cache, IH))
    assertEquals("大写归一", File(File(cache, ".meta"), "$IH.torrent"), MetaCache.path(cache, IH.uppercase()))
    assertNull(MetaCache.path(cache, "tmp-1"))
    assertNull(MetaCache.path(cache, "../escape"))
    assertNull(MetaCache.path(cache, IH + "/../../x"))
    assertNull(MetaCache.path(cache, ""))
    assertNull(MetaCache.path(cache, null))
    assertNotNull(MetaCache.path(cache, IH2_64))
    cache.deleteRecursively()
  }

  @Test
  fun writeThenRead_roundTrips_andCountsStats() {
    val cache = tempDir("roundtrip")
    MetaCache.resetStats()
    val blob = MetaCache.encodeTorrent(sampleInfoSection())

    assertNull("未命中返回 null", MetaCache.read(cache, IH))
    assertEquals(1, MetaCache.stats().misses)

    val written = MetaCache.writeQuietly(cache, IH, blob) { }
    assertNotNull(written)
    assertTrue("落盘后 has 为真", MetaCache.has(cache, IH))
    assertEquals(1, MetaCache.stats().writes)

    val back = MetaCache.read(cache, IH)
    assertNotNull(back)
    assertEquals("字节完全一致", blob.toList(), back!!.toList())
    assertEquals(1, MetaCache.stats().hits)
    assertEquals(50, MetaCache.stats().hitRatePercent)

    assertEquals("非法 ih 不写盘也不抛", null, MetaCache.writeQuietly(cache, "tmp-1", blob) { })
    assertEquals("空数据不写盘", null, MetaCache.writeQuietly(cache, IH, ByteArray(0)) { })
    MetaCache.resetStats()
    cache.deleteRecursively()
  }

  @Test
  fun write_rejectsOversize_andWarnsInsteadOfThrowing() {
    val cache = tempDir("oversize")
    val warns = mutableListOf<String>()
    val tooBig = ByteArray(MetaCache.MAX_BYTES + 1)
    assertNull(MetaCache.writeQuietly(cache, IH, tooBig) { warns.add(it) })
    assertTrue("超上限只告警", warns.any { it.contains("过大") })
    assertFalse(MetaCache.has(cache, IH))
    cache.deleteRecursively()
  }

  @Test
  fun delete_isIdempotent() {
    val cache = tempDir("delete")
    MetaCache.writeQuietly(cache, IH, MetaCache.encodeTorrent(sampleInfoSection())) { }
    assertTrue(MetaCache.delete(cache, IH))
    assertFalse(MetaCache.has(cache, IH))
    assertTrue("再删一次也算成功", MetaCache.delete(cache, IH))
    assertFalse("非法 ih 删除返回 false", MetaCache.delete(cache, "tmp-1"))
    cache.deleteRecursively()
  }

  @Test
  fun prune_evictsOldestByMtime() {
    val cache = tempDir("prune")
    val hashes = listOf(
      "1111111111111111111111111111111111111111",
      "2222222222222222222222222222222222222222",
      "3333333333333333333333333333333333333333",
    )
    hashes.forEachIndexed { i, ih ->
      MetaCache.writeQuietly(cache, ih, MetaCache.encodeTorrent(sampleInfoSection())) { }
      // 明确拉开 mtime，避免同毫秒导致淘汰顺序不确定
      MetaCache.path(cache, ih)!!.setLastModified(1_600_000_000_000L + i * 60_000L)
    }
    assertEquals("写第三条时已自动淘汰到 2 条", 1, MetaCache.prune(cache, 2))
    assertFalse("最旧的被淘汰", MetaCache.has(cache, hashes[0]))
    assertTrue(MetaCache.has(cache, hashes[1]))
    assertTrue(MetaCache.has(cache, hashes[2]))
    assertEquals("未超限不删", 0, MetaCache.prune(cache, 5))
    assertEquals("maxFiles<=0 视为不限", 0, MetaCache.prune(cache, 0))
    cache.deleteRecursively()
  }

  // ---------------------------------------------------------------- 编码器

  @Test
  fun encodeTorrent_insertsInfoSectionRaw() {
    val info = sampleInfoSection()
    val blob = MetaCache.encodeTorrent(info)
    val parsed = parseDict(blob)

    assertEquals("info 段必须逐字节原样（info_hash 才不变）", info.toList(), parsed["info"]!!.toList())
    assertEquals(
      "字典键必须升序（bencode 规范）",
      parsed.keys.toList().sorted(),
      parsed.keys.toList(),
    )
    assertTrue("含 created by", parsed.containsKey("created by"))
    assertTrue("含 creation date", parsed.containsKey("creation date"))
    assertFalse("无 tracker 时不写 announce", parsed.containsKey("announce"))
  }

  @Test
  fun encodeTorrent_withTrackers_writesAnnounceAndList() {
    val info = sampleInfoSection()
    val blob = MetaCache.encodeTorrent(
      info,
      trackers = listOf(A, "  ", B, A),
    )
    val parsed = parseDict(blob)
    assertEquals("info 段仍原样", info.toList(), parsed["info"]!!.toList())
    assertEquals(
      "键仍升序",
      parsed.keys.toList().sorted(),
      parsed.keys.toList(),
    )
    assertTrue("announce 存在", parsed.containsKey("announce"))
    assertTrue("announce-list 存在", parsed.containsKey("announce-list"))
    // 输入 [a, 空白, b, a]（4 项 → 2 条有效、含重复）。期望线格式：
    //   announce      = <bs(a)>
    //   announce-list = ll<bs(a)>el<bs(b)>ee        ← 去重后恰好 2 项（a 只出现一次）
    // 若没去重，list 会有 3 项、a 会出现 3 次。
    assertEquals("announce = 首条 tracker", bs(A).toList(), parsed["announce"]!!.toList())
    val expectedList = "l".toByteArray() + "l".toByteArray() + bs(A) + "e".toByteArray() +
      "l".toByteArray() + bs(B) + "e".toByteArray() + "e".toByteArray()
    assertEquals("announce-list = 去重后的 2 条", expectedList.toList(), parsed["announce-list"]!!.toList())
    assertEquals("a 出现 2 次（announce + list 各一）", 2, occurrences(blob, A))
    assertEquals("b 出现 1 次（只在 list）", 1, occurrences(blob, B))
  }

  @Test(expected = Exception::class)
  fun encodeTorrent_rejectsEmptyInfo() {
    MetaCache.encodeTorrent(ByteArray(0))
  }

  // ---------------------------------------------------------------- 极简 bencode 读（仅供断言）

  /** 测试侧独立实现的 bencode 字符串（`<字节长度>:<utf8>`）——不复用产品代码的编码器。 */
  private fun bs(s: String): ByteArray {
    val body = s.toByteArray(Charsets.UTF_8)
    return (body.size.toString() + ":").toByteArray(Charsets.US_ASCII) + body
  }

  /** haystack 里 needle（UTF-8 字节串）出现的次数。 */
  private fun occurrences(haystack: ByteArray, needle: String): Int {
    val n = needle.toByteArray(Charsets.UTF_8)
    var count = 0
    var i = 0
    while (i <= haystack.size - n.size) {
      if (haystack.copyOfRange(i, i + n.size).toList() == n.toList()) count++
      i++
    }
    return count
  }

  /** 解析**顶层字典**：值按「裸字节/字符串」取值，键按出现顺序保留（用于验升序）。 */
  private fun parseDict(data: ByteArray): LinkedHashMap<String, ByteArray> {
    var i = 0
    assertEquals("顶层必须是字典", 'd'.code.toByte(), data[i++])
    val out = LinkedHashMap<String, ByteArray>()
    while (i < data.size && data[i].toInt().toChar() != 'e') {
      val (key, ni) = readString(data, i)
      i = ni
      val valueStart = i
      i = skipValue(data, i)
      out[key.decodeToString()] = data.copyOfRange(valueStart, i)
    }
    return out
  }

  private fun readString(data: ByteArray, start: Int): Pair<ByteArray, Int> {
    var i = start
    var len = 0
    while (data[i].toInt().toChar() != ':') {
      len = len * 10 + (data[i].toInt().toChar() - '0')
      i++
    }
    i++ // 跳过 ':'
    return data.copyOfRange(i, i + len) to (i + len)
  }

  private fun skipValue(data: ByteArray, start: Int): Int {
    when (data[start].toInt().toChar()) {
      'i' -> {
        var i = start + 1
        while (data[i].toInt().toChar() != 'e') i++
        return i + 1
      }
      'l', 'd' -> {
        var i = start + 1
        while (data[i].toInt().toChar() != 'e') i = skipValue(data, i)
        return i + 1
      }
      else -> return readString(data, start).second
    }
  }

  private companion object {
    const val IH = "444b8b5c9a69b388a5013d85a42caef060f03225"
    const val IH2_64 = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
    const val A = "udp://a:80/announce"
    const val B = "udp://b:80/announce"
  }
}
