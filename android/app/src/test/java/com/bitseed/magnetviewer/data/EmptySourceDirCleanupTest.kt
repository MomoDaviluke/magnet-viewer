package com.bitseed.magnetviewer.data

import java.io.File
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * **H-02 回归**：转正后 `.preview/<ih>/` 的空目录残留。
 *
 * QA-2026-09-14：转正（`storage_moved`）后 `du -k .preview/444b8b…` = `8`（空目录还在），
 * 而这个空目录还会参与配额统计与设置页的「当前占用」。
 *
 * 规则（**非空绝不删**）：搬运成功后，源目录**已空**才删；还有文件就原样留着
 * （可能出现「部分文件还在搬」或用户手动放进来的东西）。删除失败必须**如实报告**，
 * 不许把失败说成成功（本项目三次事故的共同形态）。
 */
class EmptySourceDirCleanupTest {

  private fun tmpDir(tag: String): File =
    File(System.getProperty("java.io.tmpdir") ?: ".", "mv-empty-src-$tag-${System.nanoTime()}").also { it.mkdirs() }

  // ------------------------------------------------------------ 真实临时目录（端到端）

  @Test
  fun emptySourceDir_isDeleted() {
    val d = tmpDir("empty")
    val r = cleanupEmptySourceDir(d)
    assertTrue("空目录（搬完了）就该删掉：${r.note}", r.removed)
    assertFalse("目录必须真的不在磁盘上了", d.exists())
    assertTrue("note 要写清做了什么：${r.note}", r.note.contains("删除"))
  }

  @Test
  fun nonEmptySourceDir_isKept() {
    val d = tmpDir("nonempty")
    val f = File(d, "leftover.mp4")
    f.writeBytes(ByteArray(16))
    val r = cleanupEmptySourceDir(d)
    assertFalse("非空目录绝不删", r.removed)
    assertTrue("目录与文件都要在", d.exists() && f.exists())
    assertTrue("note 要说明为何保留：${r.note}", r.note.contains("仍有 1 个文件"))
  }

  @Test
  fun missingSourceDir_isIdempotent() {
    val d = File(System.getProperty("java.io.tmpdir") ?: ".", "mv-empty-src-missing-${System.nanoTime()}")
    val r = cleanupEmptySourceDir(d)
    assertFalse("本来就不在 → 不算删除动作", r.removed)
    assertTrue("也不能报错：${r.note}", r.note.contains("不存在"))
  }

  @Test
  fun nullSourceDir_isReportedHonestly() {
    val r = cleanupEmptySourceDir(null)
    assertFalse(r.removed)
    assertTrue("没有源目录记录时要如实说明：${r.note}", r.note.contains("无源目录"))
  }

  // ------------------------------------------------------------ 注入替身（删除失败的真实形态）

  @Test
  fun onlyEmptySubdirs_isRemoved() {
    // libtorrent 搬运后的真实形态：源目录里只剩空的子目录壳（.preview/<ih>/payload_big/）
    val d = tmpDir("emptyshell")
    val sub = File(d, "payload_big").also { it.mkdirs() }
    assertTrue("前提：子目录壳是空的", sub.listFiles()?.isEmpty() == true)
    val r = cleanupEmptySourceDir(d)
    assertTrue("一个文件都没有（只剩空壳）→ 该删：${r.note}", r.removed)
    assertFalse("递归删掉整个壳目录", d.exists())
  }

  @Test
  fun fileInsideSubdir_isKept() {
    val d = tmpDir("subfile")
    val sub = File(d, "payload_big").also { it.mkdirs() }
    File(sub, "big.mp4").writeBytes(ByteArray(32))
    val r = cleanupEmptySourceDir(d)
    assertFalse("子目录里还有文件 → 绝不能删（数据安全）", r.removed)
    assertTrue(d.exists())
    assertTrue("note 要说明为何保留：${r.note}", r.note.contains("仍有 1 个文件"))
  }

  @Test
  fun deleteFailure_isReported_notSwallowed() {
    val fake = File(System.getProperty("java.io.tmpdir") ?: ".", "mv-empty-src-fake-${System.nanoTime()}")
    val r =
      cleanupEmptySourceDir(
        fake,
        countFiles = { 0L }, // 看起来是空的
        dirExists = { true }, // 但删不掉
        deleteDir = { false },
      )
    assertFalse("删除失败不许上报成功", r.removed)
    assertTrue("失败要如实写进 note：${r.note}", r.note.contains("失败"))
  }

  @Test
  fun unreadableDirectory_isKeptConservatively() {
    // 读不到目录内容：不能认定「空」，保守不删
    val fake = File(System.getProperty("java.io.tmpdir") ?: ".", "mv-empty-src-unreadable-${System.nanoTime()}")
    val r =
      cleanupEmptySourceDir(
        fake,
        countFiles = { null },
        dirExists = { true },
        deleteDir = { true },
      )
    assertFalse("读不到目录内容时不许删", r.removed)
    assertTrue("note 要写清原因：${r.note}", r.note.contains("读不到") || r.note.contains("保留"))
  }

  @Test
  fun cleanup_returnsThePathItActedOn() {
    val d = tmpDir("path")
    val r = cleanupEmptySourceDir(d)
    assertEquals(d.path, r.path)
  }
}
