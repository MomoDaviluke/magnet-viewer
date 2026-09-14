package com.bitseed.magnetviewer.data

import java.io.File
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * 缓存配额单测（**JVM**；只碰纯逻辑 + 临时目录，绝不实例化 libtorrent4j 原生对象）。
 *
 * 覆盖（逐条对应任务书 D）：
 * 1. LRU 选择顺序（含**同 mtime 按路径定序**）；
 * 2. 保护名单命中不删；
 * 3. `limit <= 0` 只统计不删；
 * 4. 删除失败**不计入释放量**（否则配额会提前「达标」而实际仍超限）；
 * 5. 删除前**重取保护名单**（防陈旧快照误删刚登记的任务目录）；
 * 6. 名单取用失败 → 本轮保守不删；
 * 7. `.preview` 下的散落文件不属于任何任务 → 不统计、不清理；
 * 8. 配额只动 `.preview/`：`downloads/` 一律不碰；
 * 9. `downloadedBytes`（显示口径）与 `dirSizeBytes`（磁盘占用口径）**不是一回事**。
 */
class CacheQuotaTest {

  private val miB = 1L shl 20

  private fun tmpRoot(tag: String): File =
    File(System.getProperty("java.io.tmpdir") ?: ".", "mv-quota-$tag-${System.nanoTime()}").also { it.mkdirs() }

  /** 建一个含 `bytes` 字节文件的预览任务目录，并把目录与文件 mtime 都钉到 [mtimeMs]。 */
  private fun dirWith(root: File, name: String, bytes: Long, mtimeMs: Long): File {
    val d = File(root, name)
    d.mkdirs()
    val f = File(d, "data.bin")
    f.writeBytes(ByteArray(bytes.toInt()))
    f.setLastModified(mtimeMs)
    d.setLastModified(mtimeMs)
    return d
  }

  private fun previewOf(root: File): File = File(root, PREVIEW_SUBDIR).also { it.mkdirs() }

  @Test
  fun lruDeletesOldestFirst_andBreaksTiesByPath() {
    val root = tmpRoot("lru")
    val preview = previewOf(root)
    val aaa = dirWith(preview, "aaa", miB, 1_600_000_000_000L)
    val bbb = dirWith(preview, "bbb", miB, 1_600_000_100_000L)
    val ccc = dirWith(preview, "ccc", miB, 1_600_000_100_000L)

    val res = enforcePreviewLimit(preview, 1, warn = {})

    // aaa 最旧先删；bbb/ccc 同 mtime → 按路径定序（bbb 先）→ 删到 <= 1 MiB 为止
    assertEquals("同 mtime 必须按路径定序", listOf(aaa.path, bbb.path), res.deleted)
    assertEquals(2L * miB, res.freedBytes)
    assertEquals(miB, res.totalBytes)
    assertFalse(aaa.exists())
    assertFalse(bbb.exists())
    assertTrue("还没超限就停手（ccc 留下）", ccc.isDirectory)
    root.deleteRecursively()
  }

  @Test
  fun protectedDirIsSkipped_evenIfItIsTheOldest() {
    val root = tmpRoot("keep")
    val preview = previewOf(root)
    val aaa = dirWith(preview, "aaa", miB, 1_600_000_000_000L)
    val bbb = dirWith(preview, "bbb", miB, 1_600_000_100_000L)
    val ccc = dirWith(preview, "ccc", miB, 1_600_000_200_000L)

    val res = enforcePreviewLimit(preview, 1, keepDirs = setOf(aaa.absolutePath), warn = {})

    assertTrue("活跃任务目录（最旧）必须保住", aaa.isDirectory)
    assertTrue(res.kept.contains(aaa.path))
    assertEquals(listOf(bbb.path, ccc.path), res.deleted)
    root.deleteRecursively()
  }

  @Test
  fun limitZeroOrNegative_onlyCounts_neverDeletes() {
    val root = tmpRoot("nolimit")
    val preview = previewOf(root)
    val aaa = dirWith(preview, "aaa", miB, 1_600_000_000_000L)
    val bbb = dirWith(preview, "bbb", miB, 1_600_000_000_000L)

    for (limit in listOf(0, -5)) {
      val res = enforcePreviewLimit(preview, limit, warn = {})
      assertEquals("limit=$limit 不得释放任何字节", 0L, res.freedBytes)
      assertEquals(2L * miB, res.totalBytes)
      assertTrue(aaa.isDirectory && bbb.isDirectory)
      assertTrue("要说明「只统计不删除」", res.notes.any { it.contains("只统计不删除") })
    }
    root.deleteRecursively()
  }

  @Test
  fun deleteFailure_isNotCountedAsFreed() {
    val root = tmpRoot("fail")
    val preview = previewOf(root)
    dirWith(preview, "aaa", miB, 1_600_000_000_000L)
    dirWith(preview, "bbb", miB, 1_600_000_000_000L)

    // 注入「删不掉」的实现（等价于文件被占用：deleteRecursively 返回 false / 目录仍在）
    val res = enforcePreviewLimit(preview, 1, warn = {}, deleteDir = { false })

    assertEquals("删除失败绝不能计入释放量", 0L, res.freedBytes)
    assertEquals("占用仍按原样报告（保守）", 2L * miB, res.totalBytes)
    assertEquals(2, res.failed.size)
    assertTrue(res.deleted.isEmpty())
    assertTrue(File(preview, "aaa").isDirectory && File(preview, "bbb").isDirectory)
    root.deleteRecursively()
  }

  @Test
  fun deleteThatSilentlyLeavesDirBehind_isAlsoNotCounted() {
    val root = tmpRoot("fake")
    val preview = previewOf(root)
    dirWith(preview, "aaa", miB, 1_600_000_000_000L)
    dirWith(preview, "bbb", miB, 1_600_000_000_000L)

    // 返回 true 但目录还在（假成功）：以「目录是否还在」判定，同样不入账
    val res = enforcePreviewLimit(preview, 1, warn = {}, deleteDir = { true })

    assertEquals(0L, res.freedBytes)
    assertEquals(2, res.failed.size)
    root.deleteRecursively()
  }

  @Test
  fun keepListIsReFetchedBeforeEachDelete_staleSnapshotCannotDeleteNewTaskDirs() {
    val root = tmpRoot("refetch")
    val preview = previewOf(root)
    val aaa = dirWith(preview, "aaa", miB, 1_600_000_000_000L)
    val bbb = dirWith(preview, "bbb", miB, 1_600_000_100_000L)
    val ccc = dirWith(preview, "ccc", miB, 1_600_000_200_000L)

    var calls = 0
    val res =
      enforcePreviewLimit(
        preview,
        1,
        keepProvider = {
          calls++
          // 入口快照是空的（模拟「取名单 → 扫描」窗口内刚转正/刚解析的任务）
          if (calls == 1) emptySet() else setOf(aaa.absolutePath)
        },
        warn = {},
      )

    assertTrue("删除前重取名单：命中即跳过（陈旧快照不得误删）", aaa.isDirectory)
    assertTrue(res.kept.contains(aaa.path))
    assertEquals(listOf(bbb.path, ccc.path), res.deleted)
    assertTrue("每删一个都要重取一次", calls >= 4)
    root.deleteRecursively()
  }

  @Test
  fun keepListFailure_skipsDeletionEntirely() {
    val root = tmpRoot("keepfail")
    val preview = previewOf(root)
    val aaa = dirWith(preview, "aaa", miB, 1_600_000_000_000L)
    val bbb = dirWith(preview, "bbb", miB, 1_600_000_000_000L)

    val res = enforcePreviewLimit(preview, 1, keepProvider = { error("名单取用失败") }, warn = {})

    assertEquals(0L, res.freedBytes)
    assertTrue(aaa.isDirectory && bbb.isDirectory)
    assertTrue(res.notes.any { it.contains("保护名单取用失败") })
    root.deleteRecursively()
  }

  @Test
  fun strayFilesUnderPreview_areNeitherCountedNorDeleted() {
    val root = tmpRoot("stray")
    val preview = previewOf(root)
    val aaa = dirWith(preview, "aaa", miB, 1_600_000_000_000L)
    val stray = File(preview, "stray.tmp")
    stray.writeBytes(ByteArray(4096))

    val res = enforcePreviewLimit(preview, 1, warn = {})

    assertTrue("散落文件不属于任何任务：不清理（保守）", stray.isFile)
    assertEquals(1L * miB, res.totalBytes)
    assertEquals(listOf<String>(), res.deleted)
    assertTrue(aaa.isDirectory)
    root.deleteRecursively()
  }

  @Test
  fun quotaNeverTouchesDownloadsOrSiblingEntries() {
    val root = tmpRoot("downloads")
    val preview = previewOf(root)
    val downloads = File(root, DOWNLOADS_SUBDIR).also { it.mkdirs() }
    val kept = File(downloads, "keep.mp4").also { it.writeBytes(ByteArray((2L * miB).toInt())) }
    dirWith(preview, "aaa", miB, 1_600_000_000_000L)
    dirWith(preview, "bbb", miB, 1_600_000_100_000L)
    dirWith(preview, "ccc", miB, 1_600_000_200_000L)

    val res = enforcePreviewLimit(preview, 1, warn = {})

    assertTrue("downloads/ 是用户数据：配额无权涉足", kept.isFile)
    assertEquals(2, res.deleted.size)
    root.deleteRecursively()
  }

  @Test
  fun purgeDeletesAllUnprotected_keepsProtected() {
    val root = tmpRoot("purge")
    val preview = previewOf(root)
    val alive = dirWith(preview, "alive", miB, 1_600_000_000_000L)
    val dead = dirWith(preview, "dead", miB, 1_600_000_100_000L)

    val res = purgePreviewDirs(preview, keepDirs = setOf(alive.absolutePath), warn = {})

    assertEquals(listOf(dead.path), res.deleted)
    assertEquals(miB, res.freedBytes)
    assertTrue(alive.isDirectory)
    assertFalse(dead.exists())
    root.deleteRecursively()
  }

  @Test
  fun displayBytesAndDiskBytesAreDifferentScales() {
    // 显示口径：fileProgress 求和（负值/空值容错）
    assertEquals(0L, downloadedBytes(null))
    assertEquals(0L, downloadedBytes(LongArray(0)))
    assertEquals(300L, downloadedBytes(longArrayOf(100, -5, 0, 200)))

    // 磁盘口径：目录内文件**逻辑大小**（稀疏预分配也照算）
    val root = tmpRoot("scales")
    val preview = previewOf(root)
    val d = dirWith(preview, "big", 3L * miB, 1_600_000_000_000L)
    assertEquals(3L * miB, dirSizeBytes(d))

    val usage = cacheUsage(root)
    assertEquals(3L * miB, usage.previewBytes)
    assertEquals(0L, usage.downloadsBytes)
    root.deleteRecursively()
  }

  @Test
  fun normPathFoldsDotDotAndRelativeSegments() {
    val root = tmpRoot("norm")
    val inner = File(root, "$PREVIEW_SUBDIR/abc")
    inner.mkdirs()
    assertEquals(normPath(inner.path), normPath(File(root, "$PREVIEW_SUBDIR/../$PREVIEW_SUBDIR/abc").path))
    root.deleteRecursively()
  }
}
