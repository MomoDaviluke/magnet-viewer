package com.bitseed.magnetviewer.data

import java.io.File
import java.nio.file.Paths

/**
 * 预览缓存配额：超限按 LRU 清理最旧的预览任务目录
 * （桌面 `core/cache_quota.py` 的逐条移植；纯逻辑 + 显式依赖，JVM 可测）。
 *
 * 策略（与桌面同款）：
 * 1. **只动 `.preview/`**：本模块仅扫描/删除 `<cache>/.preview/` 的一级子目录。
 *    `downloads/`（用户下载数据）、`.tasks.json`、`.resume/` 一律不碰——那是
 *    `cache_guard` 保留名单管辖的用户数据，本模块无权涉足；
 * 2. **保护名单**：`keepDirs` 中的目录（活跃任务的落盘目录）绝不删除。调用方从
 *    引擎侧取（`BtEngine.activeTaskDirs()`），并且**每个候选目录删除前再取一次**
 *    （回调形态），堵住「取名单 → 扫描 → 执行删除」窗口内新登记目录被陈旧快照误删的竞态；
 * 3. **LRU**：按「最近活跃时间」从旧到新整目录删除，直到总占用回到上限以内。
 *    活跃时间 = 目录内全部文件的最大 mtime（写入即刷新），缺省退化为目录自身 mtime；
 *    同刻按路径排序保证确定性；
 * 4. **绝不抛异常**：单个目录删除失败（文件被占用等）跳过留待下次，只经 `warn` 留痕。
 *    日志与清理都是旁路设施，不能影响预览主流程。
 *
 * **两种字节口径不可互换**（桌面 `cache_quota.downloaded_bytes` 的原注释）：
 * - 配额判定走 [dirSizeBytes]（目录逻辑大小 = `st_size` 求和）：它管**磁盘占用**，
 *   要保守（稀疏文件预分配后逻辑大小恒等于文件大小）；
 * - 界面「已下载」走 [downloadedBytes]（`handle.fileProgress()` 求和）：它管**显示**，
 *   反映真实进度。若把预分配尺寸当进度显示，4.1GB 的种子才下 59MB 就会显示
 *   「缓存 4.1 GB / 2.0 GB」，既误导又像爆缓存。
 */

/** 预览缓存子目录（桌面 `core/cache_quota.py` 的同名布局）。 */
const val PREVIEW_SUBDIR: String = ".preview"

/** 下载数据子目录（用户数据；配额清理与「清空缓存」都不碰）。 */
const val DOWNLOADS_SUBDIR: String = "downloads"

/** 路径归一化（桌面 `os.path.normcase(os.path.abspath(p))` 的安卓落点）。
 *
 * POSIX 上 normcase 是恒等，abspath 只折叠相对路径与 `.`/`..`——这里用
 * `java.nio.file.Paths.normalize()` 达到同样效果（不解析符号链接，与桌面一致）。
 */
fun normPath(p: String): String =
  try {
    Paths.get(p).toAbsolutePath().normalize().toString()
  } catch (_: Throwable) {
    File(p).absolutePath
  }

/** 目录逻辑大小（字节；含子目录，统计失败按 0 计）。 */
fun dirSizeBytes(path: File): Long {
  var total = 0L
  try {
    walkFiles(path) { f -> total += f.length() }
  } catch (_: Throwable) {
    // 统计失败按已累计值返回（与桌面「失败按 0 计」同型：绝不抛）
  }
  return total
}

/** 目录内全部文件（递归；目录不可读时返回已遍历到的部分）。 */
private fun walkFiles(root: File, visit: (File) -> Unit) {
  val children = try {
    root.listFiles() ?: return
  } catch (_: Throwable) {
    return
  }
  for (c in children) {
    try {
      if (c.isDirectory) walkFiles(c, visit) else visit(c)
    } catch (_: Throwable) {
      // 单项异常跳过（部分统计优于失败）
    }
  }
}

/**
 * 已下载字节汇总（`handle.fileProgress()` 的**显示**口径；纯函数、不碰文件系统）。
 *
 * 容错：null / 非法项跳过、负值截 0（不产生负数占用）。
 */
fun downloadedBytes(fileProgress: LongArray?): Long {
  var total = 0L
  for (v in fileProgress ?: LongArray(0)) if (v > 0) total += v
  return total
}

/** 目录最近活跃时间（秒）：全部文件的最大 mtime，空目录用目录 mtime；失败返回 0.0。 */
fun lastActiveSeconds(path: File): Double {
  var newest = 0.0
  try {
    walkFiles(path) { f ->
      val t = f.lastModified()
      if (t > 0) {
        val sec = t / 1000.0
        if (sec > newest) newest = sec
      }
    }
    if (newest <= 0.0) {
      val t = path.lastModified()
      if (t > 0) newest = t / 1000.0
    }
  } catch (_: Throwable) {
    // 读不到就按 0（最旧）——与桌面 OSError 分支同型
  }
  return newest
}

/** 一个预览任务目录：绝对路径 / 逻辑大小 / 最近活跃时间。 */
data class PreviewDirEntry(val path: String, val sizeBytes: Long, val lastActive: Double)

/** 列出 `.preview` 下的一级子目录（散落的杂项文件不属于任何任务，不清理，保守）。 */
fun scanPreviewDirs(previewRoot: File): List<PreviewDirEntry> {
  val entries = try {
    previewRoot.listFiles() ?: return emptyList()
  } catch (_: Throwable) {
    return emptyList()
  }
  val out = ArrayList<PreviewDirEntry>(entries.size)
  for (e in entries) {
    val isDir = try {
      e.isDirectory
    } catch (_: Throwable) {
      false
    }
    if (!isDir) continue
    out.add(PreviewDirEntry(e.path, dirSizeBytes(e), lastActiveSeconds(e)))
  }
  return out
}

/** 一次配额执行的结果（全部是普通类型，UI/日志直接用）。 */
data class QuotaResult(
  /** 清理后 `.preview` 总占用（字节）。 */
  val totalBytes: Long = 0L,
  /** 本次实际释放（字节）——**删除失败的不计入**。 */
  val freedBytes: Long = 0L,
  val deleted: List<String> = emptyList(),
  val kept: List<String> = emptyList(),
  val failed: List<String> = emptyList(),
  /** 保护名单取用失败（本轮保守不删）等说明。 */
  val notes: List<String> = emptyList(),
) {
  /** 一句话总结（写日志/上 UI 都用它）。 */
  fun summary(): String =
    "预览缓存占用 ${totalBytes / 1024 / 1024} MB，本次释放 ${freedBytes / 1024 / 1024} MB" +
      "（删除 ${deleted.size} 个 / 保护 ${kept.size} 个 / 失败 ${failed.size} 个）"
}

/**
 * 保护名单归一。
 *
 * **`null` = 空名单**（桌面 `_norm_keep`：`{... for d in (kd or set()) if d}`）——「没有活跃
 * 任务」是正常态（会话未起/已停机），不是「名单故障」；只有**回调抛异常**才算故障
 * （见 [callKeep]，那种情况返回 null → 调用方本轮保守不删）。
 */
private fun normalizeKeep(raw: Set<String>?): Set<String> =
  (raw ?: emptySet()).map { normPath(it) }.toSet()

/** 回调形态取名单：异常 → null（名单故障，保守不删）；正常空集 → 空集（照常删）。 */
private fun callKeep(provider: () -> Set<String>?): Set<String>? =
  try {
    normalizeKeep(provider())
  } catch (_: Throwable) {
    null
  }

/**
 * 执行配额：超限时按 LRU 删除最旧的任务目录。
 *
 * 返回 [QuotaResult]（清理后总占用 / 本次释放 / 明细）。`limitMb <= 0` 视为不限制，
 * **只统计不删除**。
 *
 * @param keepDirs 保护名单（活跃任务落盘目录）。给回调时：入口取一次快照，
 *   **每个候选目录删除前再取一次**复核；回调抛异常 = 名单故障 → 本轮保守不删。
 *   给固定集合时行为与「一次性快照」一致（无逐项复核开销）。
 * @param deleteDir 删除实现（默认整目录递归删除）；**返回后仍以「目录是否还在」判定
 *   是否计入释放量**——`deleteRecursively()` 会吞掉删除失败（如文件被占用），
 *   若把失败也计入 `freed`，配额会提前「达标」而实际仍超限。
 */
fun enforcePreviewLimit(
  previewRoot: File,
  limitMb: Int,
  keepDirs: Set<String>? = null,
  keepProvider: (() -> Set<String>?)? = null,
  warn: (String) -> Unit = {},
  deleteDir: (File) -> Boolean = { it.deleteRecursively() },
): QuotaResult {
  val notes = ArrayList<String>(4)
  fun note(msg: String) {
    notes.add(msg)
    try {
      warn(msg)
    } catch (_: Throwable) {
      // 日志本身不能成为失败点
    }
  }

  val keep = if (keepProvider != null) callKeep(keepProvider) else normalizeKeep(keepDirs)
  val items = scanPreviewDirs(previewRoot)
  var total = items.sumOf { it.sizeBytes }
  if (keep == null) {
    // 名单故障：一个都不删（保守），但仍然如实报告占用
    note("保护名单取用失败：本轮跳过配额删除（保守）")
    return QuotaResult(totalBytes = total, notes = notes)
  }
  if (limitMb <= 0) {
    note("缓存上限 <= 0（不限）：只统计不删除，当前占用 ${total / 1024 / 1024} MB")
    return QuotaResult(totalBytes = total, notes = notes)
  }
  val limit = limitMb.toLong() * 1024L * 1024L
  if (total <= limit) {
    note("预览缓存占用 ${total / 1024 / 1024} MB <= 上限 ${limitMb} MB：无需清理")
    return QuotaResult(totalBytes = total, notes = notes)
  }
  note("预览缓存超限：占用 ${total / 1024 / 1024} MB > 上限 ${limitMb} MB，按 LRU 清理")

  var freed = 0L
  var currentKeep: Set<String> = keep
  val deleted = ArrayList<String>(4)
  val kept = ArrayList<String>(4)
  val failed = ArrayList<String>(4)
  // 最旧优先；同刻按路径定序（确定性）
  for (item in items.sortedWith(compareBy({ it.lastActive }, { it.path }))) {
    if (total - freed <= limit) break
    val norm = normPath(item.path)
    if (norm in currentKeep) {
      kept.add(item.path)
      continue
    }
    if (keepProvider != null) {
      // 删除动作与入口快照之间可能插入新登记目录：rmtree 前复核
      val fresh = callKeep(keepProvider)
      if (fresh == null) {
        note("保护名单取用失败：中止本轮剩余删除（保守）")
        break
      }
      currentKeep = fresh
      if (norm in currentKeep) {
        kept.add(item.path)
        continue
      }
    }
    val target = File(item.path)
    val ok = try {
      deleteDir(target)
    } catch (e: Throwable) {
      note("删除预览目录失败（留待下次）：${item.path}：${e.message ?: e.javaClass.simpleName}")
      failed.add(item.path)
      continue
    }
    if (!ok || target.exists()) {
      // 删除失败（文件被占用等）/ 假成功：**不计入 freed**，否则配额提前达标而实际仍超限
      note("删除预览目录失败（留待下次）：${item.path}")
      failed.add(item.path)
      continue
    }
    freed += item.sizeBytes
    deleted.add(item.path)
    note("预览缓存超限，按 LRU 清理：${item.path}（释放 ${item.sizeBytes} 字节）")
  }
  return QuotaResult(
    totalBytes = total - freed,
    freedBytes = freed,
    deleted = deleted,
    kept = kept,
    failed = failed,
    notes = notes,
  )
}

/**
 * 立即清理（设置页按钮）：把 `.preview` 下**全部非保护**目录删掉（不只看上限）。
 *
 * 与 [enforcePreviewLimit] 的分工：那个是「超限才按 LRU 删」的自动闸门（`limit<=0` 只统计），
 * 这个是用户显式动作（对应桌面设置页的「立即清理」= 清空非保留内容）。两条路都遵守同一个
 * 保护名单（删除前重取）与同一条失败不入账规则。
 */
fun purgePreviewDirs(
  previewRoot: File,
  keepDirs: Set<String>? = null,
  keepProvider: (() -> Set<String>?)? = null,
  warn: (String) -> Unit = {},
  deleteDir: (File) -> Boolean = { it.deleteRecursively() },
): QuotaResult {
  val notes = ArrayList<String>(4)
  fun note(msg: String) {
    notes.add(msg)
    try {
      warn(msg)
    } catch (_: Throwable) {
      // 日志本身不能成为失败点
    }
  }
  val items = scanPreviewDirs(previewRoot)
  val total = items.sumOf { it.sizeBytes }
  val keepSnap = if (keepProvider != null) callKeep(keepProvider) else normalizeKeep(keepDirs)
  if (keepSnap == null) {
    note("保护名单取用失败：跳过清理（保守）")
    return QuotaResult(totalBytes = total, notes = notes)
  }
  var keep: Set<String> = keepSnap
  var freed = 0L
  val deleted = ArrayList<String>(4)
  val kept = ArrayList<String>(4)
  val failed = ArrayList<String>(4)
  for (item in items.sortedWith(compareBy({ it.lastActive }, { it.path }))) {
    val norm = normPath(item.path)
    if (norm in keep) {
      kept.add(item.path)
      note("保留活跃任务目录：${item.path}")
      continue
    }
    if (keepProvider != null) {
      val fresh = callKeep(keepProvider)
      if (fresh == null) {
        note("保护名单取用失败：中止剩余清理（保守）")
        break
      }
      keep = fresh
      if (norm in keep) {
        kept.add(item.path)
        continue
      }
    }
    val target = File(item.path)
    val ok = try {
      deleteDir(target)
    } catch (e: Throwable) {
      note("清理目录失败（留待下次）：${item.path}：${e.message ?: e.javaClass.simpleName}")
      failed.add(item.path)
      continue
    }
    if (!ok || target.exists()) {
      note("清理目录失败（留待下次）：${item.path}")
      failed.add(item.path)
      continue
    }
    freed += item.sizeBytes
    deleted.add(item.path)
    note("立即清理：删除 ${item.path}（释放 ${item.sizeBytes} 字节）")
  }
  return QuotaResult(
    totalBytes = total - freed,
    freedBytes = freed,
    deleted = deleted,
    kept = kept,
    failed = failed,
    notes = notes,
  )
}

/** 缓存占用快照（**目录实际字节**口径；与「已下载字节」两回事，不可互换）。 */
data class CacheUsage(
  val previewBytes: Long = 0L,
  val downloadsBytes: Long = 0L,
) {
  val totalBytes: Long get() = previewBytes + downloadsBytes

  fun text(): String =
    "预览缓存 ${mb(previewBytes)} MB · 下载数据 ${mb(downloadsBytes)} MB · 合计 ${mb(totalBytes)} MB"

  private fun mb(n: Long): Long = n / 1024 / 1024
}

/** 统计缓存占用：`.preview/` 与 `downloads/` 分别计（目录逻辑字节）。 */
fun cacheUsage(cacheRoot: File): CacheUsage =
  CacheUsage(
    previewBytes = dirSizeBytes(File(cacheRoot, PREVIEW_SUBDIR)),
    downloadsBytes = dirSizeBytes(File(cacheRoot, DOWNLOADS_SUBDIR)),
  )
