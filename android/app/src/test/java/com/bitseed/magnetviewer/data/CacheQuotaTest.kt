package com.bitseed.magnetviewer.data

import java.io.File
import java.io.IOException
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * 缓存配额单测（**JVM**；纯逻辑 + 显式依赖，绝不实例化 libtorrent4j 原生对象）。
 *
 * 覆盖（逐条对应任务书 D）：
 * 1. LRU 选择顺序（含**同 mtime 按路径定序**）；
 * 2. 保护名单命中不删；
 * 3. `limit <= 0` 只统计不删；
 * 4. 删除失败**不计入释放量**（否则配额会提前「达标」而实际仍超限）；
 * 5. 删除前**重取保护名单**（防陈旧快照误删刚登记的任务目录）；
 * 6. 名单取用失败 → 本轮保守不删（含「删到一半名单故障」的中止路径）；
 * 7. `.preview` 下的散落文件不属于任何任务 → 不统计、不清理；
 * 8. 配额只动 `.preview/`：`downloads/` 一律不碰；
 * 9. `downloadedBytes`（显示口径）与 `dirSizeBytes`（磁盘占用口径）**不是一回事**。
 *
 * **为什么这些断言是确定性的**（旧写法在此处偶发翻红）：
 * `CacheQuota.kt` 与文件系统的接触面全部可注入（`scanDirs` / `deleteDir` / `dirExists`，
 * 默认即真实实现）。多数用例走 [VirtualPreview]——**内存虚拟目录树**：扫描、删除、
 * 存在性全由替身回答，删除结果可编程（真删 / 拒删 / 抛异常 / 假成功），一个真实文件都不碰。
 * 断言随之只落在配额**语义**上（LRU 顺序、保护名单、失败不计释放量），不再被「Windows
 * 上文件被占用或防病毒让 `deleteRecursively()` 偶发失败」这种环境噪声左右——旧写法里
 * 那种失败会把「应删的目录」从 `deleted` 挪进 `failed`，于是同一条断言随机翻红。
 *
 * 真实临时目录只用于两类用例：
 * - 端到端 2 个（[realTempTree_lru_everyCandidateIsEitherDeletedOrFailed]、
 *   [realTempTree_purge_protectedSurvives_andUnprotectedAccounted]），断言口径刻意
 *   **与「删除是否成功」无关**：只断言「每个候选要么被删要么进 failed，两者之和 = 候选数」
 *   以及账目自洽；
 * - 范围/口径 3 个（`limit<=0`、散落文件、`downloads/` 不被碰），本来就不依赖删除结果。
 */
class CacheQuotaTest {

  private val miB = 1L shl 20

  // ------------------------------------------------------------ 真实临时目录（端到端用）

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

  // ------------------------------------------------------------ 内存虚拟目录树（确定性替身）

  /**
   * 虚拟删除的可编程结果（见 [VirtualPreview.onDelete]）。
   *
   * 刻意覆盖四种真实世界结局，包括「返回 true 但目录还在」的**假成功**——配额必须
   * 以「目录是否还在」而不是「实现返回什么」判定是否入账。
   */
  private enum class DeleteOutcome {
    /** 真删：目录从虚拟树移除，删除实现返回 true。 */
    DELETED,
    /** 拒删：目录留在虚拟树，删除实现返回 false（等价于 rmtree 报失败）。 */
    REFUSED,
    /** 抛异常：目录留在虚拟树，删除实现抛 [IOException]（等价于文件被占用）。 */
    THREW,
    /** 假成功：删除实现返回 true，但目录仍在虚拟树里。 */
    LIED,
  }

  /**
   * 内存虚拟 `.preview` 目录树：**不创建、不读取、不删除任何真实文件**。
   *
   * 三个接缝（[scan] / [deleteDir] / [dirExists]）全部由它回答，删除行为由 [onDelete]
   * 编程，因此 LRU 顺序、保护名单、失败不计释放量这些语义的断言完全确定。
   * 根路径只作字符串标签（取自 `java.io.tmpdir`，刻意**不** `mkdirs`）。
   */
  private class VirtualPreview(tag: String) {
    /** 根路径：只是个标签，磁盘上并不存在。 */
    val previewRoot: File = File(File(System.getProperty("java.io.tmpdir") ?: ".", "mv-quota-virtual-$tag"), PREVIEW_SUBDIR)

    private class Dir(val path: String, val sizeBytes: Long, val lastActive: Double)

    private val live = LinkedHashMap<String, Dir>() // name -> 仍在的目录

    /** 删除行为（默认总是真删）；入参是目录名，便于「只让某个目录删不掉」。 */
    var onDelete: (String) -> DeleteOutcome = { DeleteOutcome.DELETED }

    /** 收到删除调用的路径（按调用顺序）——用来断言「谁被动了、以什么顺序」。 */
    val attempts = ArrayList<String>()

    /** 虚拟树里真正被移走的路径。 */
    val removed = ArrayList<String>()

    /** 扫描替身收到过的根路径。 */
    val scanCalls = ArrayList<File>()

    /** 加一个任务目录；返回它的路径（断言里直接用这个字符串）。 */
    fun dir(name: String, sizeBytes: Long, lastActiveSec: Double): String {
      val path = File(previewRoot, name).path
      live[name] = Dir(path, sizeBytes, lastActiveSec)
      return path
    }

    /**
     * 扫描替身：按**插入序倒序**返回（模拟真实 `listFiles()` 的任意顺序——排序必须由
     * 配额实现内部完成，用例把目录按「由旧到新」插入，于是替身给的顺序恰好是 LRU 的反序）。
     */
    fun scan(root: File): List<PreviewDirEntry> {
      scanCalls.add(root)
      return live.values.reversed().map { PreviewDirEntry(it.path, it.sizeBytes, it.lastActive) }
    }

    /** 删除替身：记录调用，再按 [onDelete] 落定虚拟树状态并返回实现返回值。 */
    val deleteDir: (File) -> Boolean = { f ->
      attempts.add(f.path)
      val name = File(f.path).name
      when (onDelete(name)) {
        DeleteOutcome.DELETED -> {
          if (live.remove(name) != null) removed.add(f.path)
          true
        }
        DeleteOutcome.REFUSED -> false
        DeleteOutcome.THREW -> throw IOException("被占用（虚拟）：$name")
        DeleteOutcome.LIED -> true
      }
    }

    /** 存在性替身：目录是否仍在虚拟树里（[DeleteOutcome.LIED] 时仍在）。 */
    val dirExists: (File) -> Boolean = { f -> live.values.any { it.path == f.path } }

    fun stillThere(path: String): Boolean = live.values.any { it.path == path }
  }

  /** 用虚拟树跑配额自动闸门（三个接缝全接管 → 完全确定）。 */
  private fun quotaOn(
    v: VirtualPreview,
    limitMb: Int,
    keepDirs: Set<String>? = null,
    keepProvider: (() -> Set<String>?)? = null,
    warn: (String) -> Unit = {},
  ): QuotaResult =
    enforcePreviewLimit(
      previewRoot = v.previewRoot,
      limitMb = limitMb,
      keepDirs = keepDirs,
      keepProvider = keepProvider,
      warn = warn,
      deleteDir = v.deleteDir,
      scanDirs = v::scan,
      dirExists = v.dirExists,
    )

  /** 用虚拟树跑「立即清理」。 */
  private fun purgeOn(
    v: VirtualPreview,
    keepDirs: Set<String>? = null,
    keepProvider: (() -> Set<String>?)? = null,
    warn: (String) -> Unit = {},
  ): QuotaResult =
    purgePreviewDirs(
      previewRoot = v.previewRoot,
      keepDirs = keepDirs,
      keepProvider = keepProvider,
      warn = warn,
      deleteDir = v.deleteDir,
      scanDirs = v::scan,
      dirExists = v.dirExists,
    )

  // ------------------------------------------------------------ LRU 顺序（虚拟树）

  @Test
  fun lruDeletesOldestFirst_andBreaksTiesByPath() {
    val v = VirtualPreview("lru")
    val aaa = v.dir("aaa", miB, 1_600_000_000.0)
    val bbb = v.dir("bbb", miB, 1_600_000_100.0)
    val ccc = v.dir("ccc", miB, 1_600_000_100.0)

    val res = quotaOn(v, 1)

    assertEquals("扫描替身必须收到 previewRoot（接缝确实接上了）", listOf(v.previewRoot), v.scanCalls)
    // aaa 最旧先删；bbb/ccc 同 mtime → 按路径定序（bbb 先）→ 删到 <= 1 MiB 为止
    assertEquals("同 mtime 必须按路径定序", listOf(aaa, bbb), res.deleted)
    assertEquals("虚拟扫描给的是 LRU 反序：顺序必须由实现内部排出来", listOf(aaa, bbb), v.attempts)
    assertEquals(2L * miB, res.freedBytes)
    assertEquals(miB, res.totalBytes)
    assertTrue("还没超限就停手（ccc 连碰都不该碰）", !v.attempts.contains(ccc))
    assertTrue("ccc 留下", v.stillThere(ccc))
    assertFalse(v.stillThere(aaa) || v.stillThere(bbb))
    assertTrue(res.failed.isEmpty() && res.kept.isEmpty())
  }

  @Test
  fun protectedDirIsSkipped_evenIfItIsTheOldest() {
    val v = VirtualPreview("keep")
    val aaa = v.dir("aaa", miB, 1_600_000_000.0)
    val bbb = v.dir("bbb", miB, 1_600_000_100.0)
    val ccc = v.dir("ccc", miB, 1_600_000_200.0)

    val res = quotaOn(v, 1, keepDirs = setOf(aaa))

    assertTrue("活跃任务目录（最旧）必须保住", v.stillThere(aaa))
    assertTrue(res.kept.contains(aaa))
    assertFalse("保护目录不得收到删除调用", v.attempts.contains(aaa))
    assertEquals(listOf(bbb, ccc), res.deleted)
    assertEquals(2L * miB, res.freedBytes)
    assertEquals("保护目录的字节仍算占用", miB, res.totalBytes)
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

  // ------------------------------------------------------------ 删除失败语义（虚拟树）

  @Test
  fun deleteFailure_isNotCountedAsFreed() {
    val v = VirtualPreview("fail")
    val aaa = v.dir("aaa", miB, 1_600_000_000.0)
    val bbb = v.dir("bbb", miB, 1_600_000_000.0)
    // 注入「删不掉」的实现（等价于文件被占用：deleteRecursively 返回 false / 目录仍在）
    v.onDelete = { DeleteOutcome.REFUSED }

    val res = quotaOn(v, 1)

    assertEquals("删除失败绝不能计入释放量", 0L, res.freedBytes)
    assertEquals("占用仍按原样报告（保守）", 2L * miB, res.totalBytes)
    assertEquals(listOf(aaa, bbb), res.failed)
    assertTrue(res.deleted.isEmpty())
    assertTrue("失败留待下次：虚拟树里一个都不能少", v.removed.isEmpty())
    assertTrue(v.stillThere(aaa) && v.stillThere(bbb))
  }

  @Test
  fun deleteThatSilentlyLeavesDirBehind_isAlsoNotCounted() {
    val v = VirtualPreview("fake")
    val aaa = v.dir("aaa", miB, 1_600_000_000.0)
    val bbb = v.dir("bbb", miB, 1_600_000_000.0)
    // 返回 true 但目录还在（假成功）：以「目录是否还在」判定，同样不入账
    v.onDelete = { DeleteOutcome.LIED }

    val res = quotaOn(v, 1)

    assertEquals("假成功不得计入释放量", 0L, res.freedBytes)
    assertEquals(2L * miB, res.totalBytes)
    assertEquals(listOf(aaa, bbb), res.failed)
    assertTrue(res.deleted.isEmpty())
    assertTrue(v.stillThere(aaa) && v.stillThere(bbb))
  }

  @Test
  fun deleteThrowing_isNotCounted_andFailureIsNoted() {
    val v = VirtualPreview("throw")
    val aaa = v.dir("aaa", miB, 1_600_000_000.0)
    val bbb = v.dir("bbb", miB, 1_600_000_000.0)
    // rmtree 抛异常（文件被占用等）：清理是旁路设施，异常必须被吞掉并留痕
    v.onDelete = { DeleteOutcome.THREW }

    val res = quotaOn(v, 1)

    assertEquals("抛异常同样不得计入释放量", 0L, res.freedBytes)
    assertEquals(2L * miB, res.totalBytes)
    assertEquals(listOf(aaa, bbb), res.failed)
    assertTrue(res.deleted.isEmpty())
    assertEquals("每个失败都要留痕（含异常原因）", 2, res.notes.count { it.contains("删除预览目录失败") })
    assertTrue("异常消息要带上", res.notes.any { it.contains("被占用（虚拟）") })
    assertTrue(v.stillThere(aaa) && v.stillThere(bbb))
  }

  @Test
  fun mixedOutcomes_failedDirDoesNotStopCleanup_andFreedCountsOnlySuccesses() {
    val v = VirtualPreview("mixed")
    val aaa = v.dir("aaa", miB, 1_600_000_000.0) // 被占用：删不掉
    val bbb = v.dir("bbb", miB, 1_600_000_100.0)
    val ccc = v.dir("ccc", miB, 1_600_000_200.0)
    val ddd = v.dir("ddd", miB, 1_600_000_300.0)
    v.onDelete = { name -> if (name == "aaa") DeleteOutcome.REFUSED else DeleteOutcome.DELETED }

    val res = quotaOn(v, 1) // 4 MiB → 需回到 1 MiB：3 个成功 + 1 个失败

    assertEquals("失败的那个只进 failed，不占释放量", listOf(aaa), res.failed)
    assertEquals("失败不停工：继续把后面的删到达标", listOf(bbb, ccc, ddd), res.deleted)
    assertEquals("释放量只算成功的三个", 3L * miB, res.freedBytes)
    assertEquals("失败目录的字节仍算占用", miB, res.totalBytes)
    assertEquals("仍按 LRU 序逐个尝试", listOf(aaa, bbb, ccc, ddd), v.attempts)
    assertTrue(v.stillThere(aaa))
  }

  // ------------------------------------------------------------ 保护名单（虚拟树）

  @Test
  fun keepListIsReFetchedBeforeEachDelete_staleSnapshotCannotDeleteNewTaskDirs() {
    val v = VirtualPreview("refetch")
    val aaa = v.dir("aaa", miB, 1_600_000_000.0)
    val bbb = v.dir("bbb", miB, 1_600_000_100.0)
    val ccc = v.dir("ccc", miB, 1_600_000_200.0)

    var calls = 0
    val res =
      quotaOn(
        v,
        1,
        keepProvider = {
          calls++
          // 入口快照是空的（模拟「取名单 → 扫描」窗口内刚转正/刚解析的任务）
          if (calls == 1) emptySet() else setOf(aaa)
        },
      )

    assertTrue("删除前重取名单：命中即跳过（陈旧快照不得误删）", v.stillThere(aaa))
    assertFalse("复核命中后不得再调删除", v.attempts.contains(aaa))
    assertTrue(res.kept.contains(aaa))
    assertEquals(listOf(bbb, ccc), res.deleted)
    // 入口 1 次 + 每个未保护候选（aaa/bbb/ccc）各复核 1 次 = 4
    assertEquals("每删一个都要重取一次", 4, calls)
  }

  @Test
  fun keepListFailure_skipsDeletionEntirely() {
    val v = VirtualPreview("keepfail")
    val aaa = v.dir("aaa", miB, 1_600_000_000.0)
    val bbb = v.dir("bbb", miB, 1_600_000_000.0)

    val res = quotaOn(v, 1, keepProvider = { error("名单取用失败") })

    assertEquals("名单故障：一个都不能删", emptyList<String>(), v.attempts)
    assertEquals(0L, res.freedBytes)
    assertEquals("占用如实报告", 2L * miB, res.totalBytes)
    assertTrue(v.stillThere(aaa) && v.stillThere(bbb))
    assertTrue(res.deleted.isEmpty() && res.failed.isEmpty() && res.kept.isEmpty())
    assertTrue(res.notes.any { it.contains("保护名单取用失败") })
  }

  @Test
  fun keepListFailureMidway_abortsRemainingDeletes() {
    val v = VirtualPreview("keepabort")
    val aaa = v.dir("aaa", miB, 1_600_000_000.0)
    val bbb = v.dir("bbb", miB, 1_600_000_100.0)

    var calls = 0
    val res =
      quotaOn(
        v,
        1,
        keepProvider = {
          calls++
          if (calls == 1) emptySet() else error("名单取用失败")
        },
      )

    assertEquals("入口快照之后复核即失败：中止本轮删除（保守）", 0, v.attempts.size)
    assertEquals(2, calls)
    assertEquals(0L, res.freedBytes)
    assertEquals(2L * miB, res.totalBytes)
    assertTrue(res.deleted.isEmpty() && res.failed.isEmpty())
    assertTrue(res.notes.any { it.contains("中止本轮剩余删除") })
    assertTrue(v.stillThere(aaa) && v.stillThere(bbb))
  }

  // ------------------------------------------------------------ 「立即清理」（虚拟树）

  @Test
  fun purgeDeletesAllUnprotected_keepsProtected() {
    val v = VirtualPreview("purge")
    val alive = v.dir("alive", miB, 1_600_000_000.0)
    val dead = v.dir("dead", miB, 1_600_000_100.0)
    val stale = v.dir("stale", miB, 1_600_000_200.0)

    val res = purgeOn(v, keepDirs = setOf(alive))

    assertEquals(listOf(dead, stale), res.deleted)
    assertEquals("不超限也照删；顺序仍是 LRU 由旧到新", listOf(dead, stale), v.attempts)
    assertEquals(2L * miB, res.freedBytes)
    assertEquals(miB, res.totalBytes)
    assertTrue("活跃任务目录（最旧）必须原样留着", v.stillThere(alive))
    assertTrue(res.kept.contains(alive))
    assertFalse("保护目录不得收到删除调用", v.attempts.contains(alive))
  }

  @Test
  fun purgeFailure_isNotCounted_andDirRetained() {
    val v = VirtualPreview("purgefail")
    val dead = v.dir("dead", miB, 1_600_000_000.0)
    v.onDelete = { DeleteOutcome.REFUSED }

    val res = purgeOn(v)

    assertEquals("清理失败同样不计入释放量", 0L, res.freedBytes)
    assertEquals("失败目录仍算占用", miB, res.totalBytes)
    assertEquals(listOf(dead), res.failed)
    assertTrue(res.deleted.isEmpty())
    assertTrue(v.stillThere(dead))
  }

  // ------------------------------------------------------------ 真实临时目录端到端（不依赖删除成功）

  /**
   * 真实文件系统上的 LRU 端到端。
   *
   * 断言口径**与「删除是否成功」无关**（Windows 上删除偶发失败属环境噪声，不是配额逻辑错）：
   * 只断言「每个候选要么被删要么进 failed，两者之和 = 尝试数」+「尝试序是 LRU 序的前缀」+ 账目自洽。
   */
  @Test
  fun realTempTree_lru_everyCandidateIsEitherDeletedOrFailed() {
    val root = tmpRoot("lru-real")
    val preview = previewOf(root)
    val aaa = dirWith(preview, "aaa", miB, 1_600_000_000_000L)
    val bbb = dirWith(preview, "bbb", miB, 1_600_000_100_000L)
    val ccc = dirWith(preview, "ccc", miB, 1_600_000_200_000L)
    val total = 3L * miB
    val lru = listOf(aaa.path, bbb.path, ccc.path)
    val attempts = ArrayList<String>()

    val res =
      enforcePreviewLimit(
        preview,
        1,
        warn = {},
        deleteDir = { f ->
          attempts.add(f.path)
          f.deleteRecursively()
        },
      )

    assertEquals("尝试顺序必须是 LRU 序的前缀（aaa 最旧先动）", lru.take(attempts.size), attempts)
    // 非空转防呆：3 MiB 要回到 <= 1 MiB，每个目录最大释放 1 MiB，故至少要动 2 个
    assertTrue("必须真的动过删除（否则下面的断言会空转）：attempts=$attempts", attempts.size >= 2)
    assertEquals("每个候选要么被删要么进 failed", attempts.size, res.deleted.size + res.failed.size)
    assertEquals(attempts.toSet(), (res.deleted + res.failed).toSet())
    assertTrue("deleted 与 failed 不得重叠", res.deleted.intersect(res.failed.toSet()).isEmpty())
    assertTrue("没有保护名单时不会有 kept", res.kept.isEmpty())
    assertEquals("释放量 = 成功删除的目录字节和", res.deleted.size * miB, res.freedBytes)
    assertEquals("剩余占用 = 总量 - 释放量", total - res.freedBytes, res.totalBytes)
    for (p in res.deleted) assertFalse("删掉的目录不该还在：$p", File(p).exists())
    for (p in res.failed) assertTrue("失败的目录必须还在（留待下次）：$p", File(p).exists())
    root.deleteRecursively()
  }

  /**
   * 真实文件系统上的「立即清理」端到端：保护目录必须活下来，其余目录全部被处理，
   * 且同样只断言「被删 / 进 failed，两者之和 = 候选数」。
   */
  @Test
  fun realTempTree_purge_protectedSurvives_andUnprotectedAccounted() {
    val root = tmpRoot("purge-real")
    val preview = previewOf(root)
    val alive = dirWith(preview, "alive", miB, 1_600_000_000_000L)
    val dead = dirWith(preview, "dead", miB, 1_600_000_100_000L)
    val stale = dirWith(preview, "stale", miB, 1_600_000_200_000L)
    val attempts = ArrayList<String>()

    val res =
      purgePreviewDirs(
        preview,
        keepDirs = setOf(alive.absolutePath),
        warn = {},
        deleteDir = { f ->
          attempts.add(f.path)
          f.deleteRecursively()
        },
      )

    assertTrue("活跃任务目录必须原样留着", alive.isDirectory)
    assertTrue(res.kept.contains(alive.path))
    assertFalse("保护目录不得收到删除调用", attempts.contains(File(alive.path).path))
    assertEquals("非保护目录全都要被尝试（立即清理不看上限）", listOf(dead.path, stale.path), attempts)
    assertEquals("每个候选要么被删要么进 failed", attempts.size, res.deleted.size + res.failed.size)
    assertEquals(attempts.toSet(), (res.deleted + res.failed).toSet())
    assertTrue("deleted 与 failed 不得重叠", res.deleted.intersect(res.failed.toSet()).isEmpty())
    assertEquals(res.deleted.size * miB, res.freedBytes)
    assertEquals(3L * miB - res.freedBytes, res.totalBytes)
    for (p in res.deleted) assertFalse("删掉的目录不该还在：$p", File(p).exists())
    for (p in res.failed) assertTrue("失败的目录必须还在（留待下次）：$p", File(p).exists())
    root.deleteRecursively()
  }

  // ------------------------------------------------------------ 范围 / 口径

  @Test
  fun strayFilesUnderPreview_areNeitherCountedNorDeleted() {
    val root = tmpRoot("stray")
    val preview = previewOf(root)
    val aaa = dirWith(preview, "aaa", miB, 1_600_000_000_000L)
    val stray = File(preview, "stray.tmp")
    stray.writeBytes(ByteArray(4096))

    val res = enforcePreviewLimit(preview, 1, warn = {})

    assertTrue("散落文件不属于任何任务：不清理（保守）", stray.isFile)
    assertEquals("散落文件也不计入占用", 1L * miB, res.totalBytes)
    assertEquals(listOf<String>(), res.deleted)
    assertTrue(aaa.isDirectory)
    root.deleteRecursively()
  }

  @Test
  fun quotaNeverTouchesDownloadsOrSiblingEntries() {
    val root = tmpRoot("dl-scope")
    val preview = previewOf(root)
    val downloads = File(root, DOWNLOADS_SUBDIR).also { it.mkdirs() }
    val kept = File(downloads, "keep.mp4").also { it.writeBytes(ByteArray((2L * miB).toInt())) }
    dirWith(preview, "aaa", miB, 1_600_000_000_000L)
    dirWith(preview, "bbb", miB, 1_600_000_100_000L)
    dirWith(preview, "ccc", miB, 1_600_000_200_000L)

    val res = enforcePreviewLimit(preview, 1, warn = {})

    assertTrue("downloads/ 是用户数据：配额无权涉足", kept.isFile)
    // 3 个候选里只有 2 个需要动（删到 <=1 MiB 即停）；这里断言的是「配额只对 .preview
    // 里的候选动手」，与「删除是否成功」无关——所以写 deleted+failed，不写 deleted。
    assertEquals("非保护候选都被处理", 2, res.deleted.size + res.failed.size)
    val dlPrefix = downloads.path + File.separator
    assertTrue(
      "downloads/ 下的任何路径都不得出现在结果里",
      (res.deleted + res.failed + res.kept).none { it.startsWith(dlPrefix) },
    )
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
