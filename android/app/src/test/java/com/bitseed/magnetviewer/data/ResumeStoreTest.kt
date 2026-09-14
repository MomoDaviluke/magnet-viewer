package com.bitseed.magnetviewer.data

import java.io.File
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * fastresume 落盘单测（JVM；**不实例化任何 libtorrent4j 对象**——解码在引擎侧 native）：
 * 路径约定（`.resume/<ih>.fastresume`）、原子写、读缺失/损坏不抛、删除、
 * 以及**文件名注入防护**（ih 会拼进文件名，非法 ih 一律拒）。
 */
class ResumeStoreTest {

  private fun tempDir(tag: String): File {
    val d = File(System.getProperty("java.io.tmpdir"), "mv-resume-$tag-${System.nanoTime()}")
    d.mkdirs()
    return d
  }

  @Test
  fun path_matchesDesktopLayout_andRejectsBadHash() {
    val cache = tempDir("path")
    assertEquals(File(cache, ".resume"), ResumeStore.dir(cache))
    assertEquals(File(File(cache, ".resume"), "$IH.fastresume"), ResumeStore.path(cache, IH))
    assertEquals("大写归一", File(File(cache, ".resume"), "$IH.fastresume"), ResumeStore.path(cache, IH.uppercase()))
    assertNull(ResumeStore.path(cache, "tmp-1"))
    assertNull(ResumeStore.path(cache, "../escape"))
    assertNull(ResumeStore.path(cache, IH + "/../../x"))
    assertNull(ResumeStore.path(cache, ""))
    assertNotNull(ResumeStore.path(cache, IH2_64))
    cache.deleteRecursively()
  }

  @Test
  fun writeThenRead_roundTrips_andIsAtomic() {
    val cache = tempDir("roundtrip")
    val data = byteArrayOf(0x64, 0x31, 0x30, 0x3A, 0x00, 0x7F, 0x00)
    val f = ResumeStore.write(cache, IH, data)
    assertEquals(File(File(cache, ".resume"), "$IH.fastresume").path, f.path)
    assertTrue("父目录 .resume 必须自动建出来", f.parentFile!!.isDirectory)
    assertTrue(data.contentEquals(ResumeStore.read(cache, IH)))
    assertTrue("不得留 .tmp 残留", f.parentFile!!.listFiles()!!.none { it.name.endsWith(".tmp") })
    // 覆盖写
    val data2 = "第二个版本".toByteArray(Charsets.UTF_8)
    ResumeStore.write(cache, IH, data2)
    assertTrue(data2.contentEquals(ResumeStore.read(cache, IH)))
    cache.deleteRecursively()
  }

  @Test
  fun read_missingOrWrongHash_isNull_andNeverThrows() {
    val cache = tempDir("read")
    assertNull(ResumeStore.read(cache, IH))
    assertNull(ResumeStore.read(cache, "非法"))
    // 目录占位（而不是文件）也不能抛
    val p = ResumeStore.path(cache, IH)!!
    p.mkdirs()
    assertNull(ResumeStore.read(cache, IH))
    cache.deleteRecursively()
  }

  @Test
  fun write_badHash_throwsInsteadOfWritingOutside() {
    val cache = tempDir("badih")
    var threw = false
    try {
      ResumeStore.write(cache, "../../evil", byteArrayOf(1))
    } catch (_: Throwable) {
      threw = true
    }
    assertTrue("非法 ih 必须拒绝写盘（路径注入防线）", threw)
    assertFalse(File(cache.parentFile, "evil.fastresume").exists())
    assertNull(ResumeStore.writeQuietly(cache, "../evil", byteArrayOf(1)) { })
    cache.deleteRecursively()
  }

  @Test
  fun deleteAndKeyQueries() {
    val cache = tempDir("keys")
    ResumeStore.write(cache, IH, byteArrayOf(1, 2, 3))
    assertTrue(ResumeStore.delete(cache, IH))
    assertFalse(File(ResumeStore.dir(cache), "$IH.fastresume").exists())
    assertTrue("再删一次也算成功（幂等：不存在即达成）", ResumeStore.delete(cache, IH))
    ResumeStore.write(cache, IH, byteArrayOf(9))
    assertEquals(listOf(IH), ResumeStore.presentKeys(cache, listOf(IH, IH2_64)))
    assertEquals(listOf(IH2_64), ResumeStore.missingKeys(cache, listOf(IH, IH2_64)))
    cache.deleteRecursively()
  }

  private companion object {
    const val IH = "444b8b5c9a69b388a5013d85a42caef060f03225"
    const val IH2_64 = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
  }
}
