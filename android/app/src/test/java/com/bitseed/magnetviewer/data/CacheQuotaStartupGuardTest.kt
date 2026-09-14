package com.bitseed.magnetviewer.data

import java.io.File
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * **B-01（Blocker）回归**：启动期的缓存配额不得把活跃任务的预览数据删掉。
 *
 * 现场（QA-2026-09-14 实测，`.hermes/android-qa/QA-2026-09-14.md` 的 B-01）：
 * ```text
 * 09-14 17:34:14.575 I/MagnetViewer: 缓存配额（设置变更（上限 1 MB））：预览缓存占用 0 MB，本次释放 30 MB（删除 2 个 / 保护 0 个 / 失败 0 个）
 * （同一批日志随后才打印）启动恢复：恢复 2 个任务：成功 2 / 失败 0
 * ```
 * 机制：`MagnetViewModel.applySettings()` 在设置流每次发射时调 `enforceCacheLimit(...)`，
 * 而**启动时设置流先于任务恢复竞发** —— 那一刻 `engine.activeTaskDirs()` 还是空集，
 * 于是保护名单为空、`.preview/<ih>` 下活跃任务的目录被 LRU 整目录删光。
 *
 * 本测试锁三件事（对应修复的三层）：
 * 1. **顺序**：`pendingRestore = true`（启动恢复未完成）→ keep 为空也**一个都不删**，
 *    并在 `QuotaResult.notes` 里写明原因；
 * 2. **既有语义不许被改死**：keep 真为空且**不在恢复中**时照常按 LRU 删除
 *    （桌面 `_norm_keep` 的真值语义：空集 = 「没有活跃任务」是正常态）；
 * 3. **摘要口径**：`summary()` 里的「占用」必须是**清理前**的实际占用，
 *    不许再出现「占用 0 MB…本次释放 30 MB」这种自相矛盾的日志（QA I-04 同一条）。
 *
 * 用真实临时目录（端到端）；断言只落在「文件还在不在 / notes 写了什么」这类与
 * 删除实现无关的事实上，不依赖 `deleteRecursively()` 在某台机器上是否偶发失败。
 */
class CacheQuotaStartupGuardTest {

  private val miB = 1L shl 20

  private fun tmpRoot(tag: String): File =
    File(System.getProperty("java.io.tmpdir") ?: ".", "mv-quota-guard-$tag-${System.nanoTime()}").also { it.mkdirs() }

  private fun previewOf(root: File): File = File(root, PREVIEW_SUBDIR).also { it.mkdirs() }

  /** 建一个含 `bytes` 字节文件的预览任务目录（mtime 钉死，保证 LRU 顺序确定）。 */
  private fun dirWith(root: File, name: String, bytes: Long, mtimeMs: Long): File {
    val d = File(root, name)
    d.mkdirs()
    val f = File(d, "payload.bin")
    f.writeBytes(ByteArray(bytes.toInt()))
    f.setLastModified(mtimeMs)
    d.setLastModified(mtimeMs)
    return d
  }

  // ------------------------------------------------------------ ①-a/b 顺序 + 兜底

  @Test
  fun b01_startupRace_emptyKeepWithPendingRestore_deletesNothing_andNotesReason() {
    val root = tmpRoot("pending-restore")
    val active = dirWith(previewOf(root), "d14e4e7f2e19aabbccddeeff0011223344556677", 3 * miB, 1_000L)
    val older = dirWith(previewOf(root), "444b8b5c9a69b388a5013d85a42caef060f03225", miB, 2_000L)

    // 启动期现场：任务还没恢复进引擎，activeTaskDirs() 此刻就是空集。
    // pendingRestore = true 就是「恢复未完成」这一事实的显式化（VM 用 restoreDone 喂它）。
    val res =
      enforcePreviewLimit(
        previewOf(root),
        limitMb = 1,
        keepProvider = { emptySet() },
        pendingRestore = true,
      )

    assertTrue("活跃任务目录必须原封不动：${active.path}", active.exists())
    assertTrue("非保护目录这一轮也必须保住：${older.path}", older.exists())
    assertTrue("pendingRestore=true 时不得删除任何目录，实际删除=${res.deleted}", res.deleted.isEmpty())
    assertTrue(
      "notes 必须写明「启动恢复未完成」这一原因，实际 notes=${res.notes}",
      res.notes.any { it.contains("启动恢复未完成") },
    )
  }

  @Test
  fun b01_existingSemantics_emptyKeepWhileRunning_stillLruDeletes() {
    // 桌面 _norm_keep 的语义：keep 真为空 = 「没有活跃任务」，是正常态 → 照常 LRU 删除。
    // 这条是「不许为了保守把功能改死」的守门断言（pendingRestore 默认 false）。
    val root = tmpRoot("running")
    val older = dirWith(previewOf(root), "444b8b5c9a69b388a5013d85a42caef060f03225", miB, 1_000L)
    val newer = dirWith(previewOf(root), "d14e4e7f2e19aabbccddeeff0011223344556677", miB, 2_000L)

    val res = enforcePreviewLimit(previewOf(root), limitMb = 1, keepDirs = emptySet())

    assertFalse("非恢复中：超限就该按 LRU 删最旧的目录", older.exists())
    assertEquals("删掉的正是最旧那个", 1, res.deleted.size)
    assertEquals(older.path, res.deleted.first())
    assertTrue("装满上限的那个留下", newer.exists())
  }

  @Test
  fun b01_normalPath_keepHasValue_overLimit_stillDeletesUnprotected() {
    val root = tmpRoot("normal")
    // 保护目录刻意排成「最旧」：LRU 循环第一个就遇到它，于是 kept 一定被记上
    // （否则循环在它之前就因达标 break，看不出「保护名单真的被尊重」）。
    val keep = dirWith(previewOf(root), "d14e4e7f2e19aabbccddeeff0011223344556677", 2 * miB, 1_000L)
    val stale = dirWith(previewOf(root), "16f1cc4744edcb71296d782103bd620848e7b868", 2 * miB, 2_000L)

    val res = enforcePreviewLimit(previewOf(root), limitMb = 2, keepDirs = setOf(keep.path))

    assertTrue("保护名单里的目录不删", keep.exists())
    assertFalse("非保护目录按 LRU 删掉（功能没被改死）", stale.exists())
    assertEquals(listOf(stale.path), res.deleted)
    assertEquals("保护名单命中要如实记账", listOf(keep.path), res.kept)
  }

  // ------------------------------------------------------------ ①-c 摘要口径

  @Test
  fun b01_summary_reportsPreCleanupUsage_soLogIsSelfConsistent() {
    val root = tmpRoot("summary")
    dirWith(previewOf(root), "444b8b5c9a69b388a5013d85a42caef060f03225", 3 * miB, 1_000L)

    val res = enforcePreviewLimit(previewOf(root), limitMb = 1, keepDirs = emptySet())
    val text = res.summary()

    // 清理前占用 3 MB、释放 3 MB、清理后 0 MB —— 三个数字必须自洽
    assertEquals("清理前占用（报告口径的落点）", 3 * miB, res.beforeBytes)
    assertEquals(0L, res.totalBytes)
    assertEquals(3 * miB, res.freedBytes)
    assertTrue("「占用」必须是清理前的实际占用：$text", text.contains("占用 3 MB（清理前）"))
    assertTrue("同时给出清理后占用：$text", text.contains("0 MB（清理后）"))
    assertTrue("释放量如实：$text", text.contains("释放 3 MB"))
    assertFalse(
      "QA 实测那种自相矛盾的日志（占用 0 MB…释放 3 MB）不许再出现：$text",
      text.contains("占用 0 MB，本次释放"),
    )
  }
}
