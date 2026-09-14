package com.bitseed.magnetviewer.data

import com.bitseed.magnetviewer.core.states.STATE_DOWNLOADING
import java.io.File

/**
 * 任务操作（**桌面 `core/taskops.py` 的语义对齐**）：优先级、转正（promote）、
 * 移除（remove，含删数据守卫）。本文件只放**纯逻辑**（路径计算 / 守卫判定 / 计划），
 * 真正碰句柄与会话的部分在 `engine/BtEngine.kt`。
 *
 * 与桌面的两处差异（如实记录，不假装逐字一致）：
 * 1. **优先级落点**：桌面 `lt_priority(p) = {0:1, 1:50, 2:150, 3:255}` 走 python-libtorrent 的
 *    `torrent_priority`；libtorrent4j 2.1.0-39 的 `TorrentHandle` **没有**对应 API
 *    （javap 已核对：只有 `queue_position_*`），故安卓侧把同一映射用于**队列位置**
 *    （高优先 → 队列靠前）+ `auto_managed` 队列，配合会话的 `active_downloads` 限并发；
 * 2. **转正落盘**：桌面 D10 原地沿用 `.preview/<ih>`；安卓侧按本任务书要求把任务目录
 *    **转成 `<cache>/downloads/<ih>`**（`TorrentHandle.moveStorage`），因为 `.preview/`
 *    是配额清理的作业区，用户数据放进去会被 LRU 清掉。
 *
 * 落盘布局（与桌面同根）：`<cache>/.preview/<ih>/`（预览）与 `<cache>/downloads/<ih>/`（下载）。
 */

// ---------------------------------------------------------------- 优先级（0..3）

/** 任务优先级下界（桌面 `taskops.set_priority` 的值域：0~3）。 */
const val TASK_PRIORITY_MIN: Int = 0

/** 任务优先级上界（桌面：3 = 最高；4 以上非法，不「顺手」截断）。 */
const val TASK_PRIORITY_MAX: Int = 3

/** 优先级是否在值域内（桌面：值域外 `return False`，不修改现状）。 */
fun isValidTaskPriority(p: Int): Boolean = p in TASK_PRIORITY_MIN..TASK_PRIORITY_MAX

/** 优先级归一到值域内（只用于**显示**回读，不用于校验）。 */
fun clampTaskPriority(p: Int): Int = p.coerceIn(TASK_PRIORITY_MIN, TASK_PRIORITY_MAX)

/**
 * 任务优先级 0~3 → libtorrent 队列权重（桌面 `core/taskops.py: lt_priority` 逐值对齐）。
 *
 * 0 = 默认/最低档（1），1/2/3 逐档提升；未知值回落 1（与桌面 `.get(int(p), 1)` 同款）。
 */
fun ltPriority(p: Int): Int =
  when (p) {
    0 -> 1
    1 -> 50
    2 -> 150
    3 -> 255
    else -> 1
  }

/**
 * 队列顺序：高优先在前；**同优先级保持原顺序**（稳定排序 = 「先来先下」）。
 *
 * 返回 info_hash 列表，索引即队列位置（0 = 最靠前）。会话 `active_downloads` 限制
 * 并发时，排在前面的先动 —— 这就是「优先级影响并发」的落点。
 */
fun priorityQueueOrder(entries: List<Pair<String, Int>>): List<String> =
  entries
    .withIndex()
    .sortedWith(compareByDescending<IndexedValue<Pair<String, Int>>> { clampTaskPriority(it.value.second) }
      .thenBy { it.index })
    .map { it.value.first }

// ---------------------------------------------------------------- 转正（promote）

/**
 * 转正计划：把预览任务目录 `.preview/<ih>` 转成 `<cache>/downloads/<ih>`
 * （桌面 D10「沿用已下载分块、零额外下载」的语义 + 本任务书的落盘要求）。
 */
data class PromotePlan(
  val infoHash: String = "",
  val valid: Boolean = false,
  /** 转正前目录（`.preview/<ih>`）。 */
  val fromDir: File? = null,
  /** 转正后目录（`downloads/<ih>`）。 */
  val toDir: File? = null,
  /** 该任务本来就是下载任务（落盘已在 downloads/）——幂等：不重复搬运。 */
  val alreadyPromoted: Boolean = false,
  val error: String = "",
  val note: String = "",
)

/** 转正后的落盘目录：`<cache>/downloads/<ih>`；ih 非法返回 null（绝不拼出穿越路径）。 */
fun promoteDirFor(cacheDir: File, infoHash: String): File? {
  val ih = normalizeInfoHash(infoHash) ?: return null
  return File(cacheDir, DOWNLOADS_SUBDIR + File.separator + ih)
}

/** 转正前的落盘目录：`<cache>/.preview/<ih>`（与 `previewDirFor` 同一算法）。 */
fun previewDirOf(cacheDir: File, infoHash: String): File? {
  val ih = normalizeInfoHash(infoHash) ?: return null
  return File(cacheDir, PREVIEW_SUBDIR + File.separator + ih)
}

/**
 * 计算转正路径（纯函数）。
 *
 * @param savePath 任务当前落盘目录（空 = 未记录，按 `.preview/<ih>` 推断）
 */
fun planPromote(infoHash: String, cacheDir: File, savePath: String = ""): PromotePlan {
  val ih = normalizeInfoHash(infoHash)
    ?: return PromotePlan(infoHash = infoHash, valid = false, error = "转正失败：任务缺少合法 info_hash")
  val to = promoteDirFor(cacheDir, ih) ?: return PromotePlan(infoHash = ih, valid = false, error = "转正失败：目标目录不可用")
  val current = savePath.trim().takeIf { it.isNotEmpty() }?.let { File(it) } ?: previewDirOf(cacheDir, ih)
  val already = current != null && isWithin(to, current)
  return PromotePlan(
    infoHash = ih,
    valid = true,
    fromDir = current,
    toDir = to,
    alreadyPromoted = already,
    note =
      if (already) "已在 downloads/ 落盘（转正幂等：不重复搬运）"
      else "转正：${current?.path ?: "-"} → ${to.path}（沿用已下载分块，继续下载该文件）",
  )
}

/** 转正后的任务状态（桌面 `_convert_to_download_locked`：直接进 DOWNLOADING）。 */
const val PROMOTE_STATE: String = STATE_DOWNLOADING

// ---------------------------------------------------------------- 转正后的空源目录清理（H-02）

/** 空源目录清理的结果（诚实报告：删了就 removed=true，没删/删不掉都要在 note 里说清）。 */
data class EmptyDirCleanup(
  val path: String = "",
  /** 是否真的把目录删掉了（以「目录是否还在」复核，不看删除实现的返回值）。 */
  val removed: Boolean = false,
  val note: String = "",
)

/**
 * 递归数一个目录里的**文件**个数（目录壳不计；读不到返回 null）。
 *
 * 为什么按「文件数」而不是「entry 数」判空：libtorrent 搬运（`moveStorage`）把数据搬走后，
 * 源目录里通常**只剩空的子目录壳**（实测：`.preview/<ih>/payload_big/` 空壳，`du -k` = 8），
 * 按 entry 判空会永远判不了空 → 残留目录继续参与配额统计与「当前占用」（H-02 的原现象）。
 */
fun countFilesRecursively(dir: File): Long? {
  var n = 0L
  val stack = ArrayDeque<File>()
  stack.addLast(dir)
  while (stack.isNotEmpty()) {
    val d = stack.removeLast()
    val children =
      try {
        d.listFiles()
      } catch (_: Throwable) {
        return null
      }
        ?: return null
    for (c in children) {
      val isDir =
        try {
          c.isDirectory
        } catch (_: Throwable) {
          return null
        }
      if (isDir) stack.addLast(c) else n++
    }
  }
  return n
}

/**
 * 转正（`storage_moved` 回执）之后清理**已空**的源目录（`.preview/<ih>`）。
 *
 * 规则（H-02）：
 * - 目录里**一个文件都没有**（含「只剩空子目录壳」的常见形态）→ 递归删掉它
 *   （它还会参与配额统计与「当前占用」，留着只是噪声）；
 * - 目录里有**任何文件** → **绝不删**（可能在搬、也可能是用户放进来的东西；宁可留残留）；
 * - 读不到目录内容 → 按「非空」保守处理；
 * - 目录本来就不在 → 幂等成功（不算删除动作）；
 * - 删除失败 → [EmptyDirCleanup.removed] 保持 false 且 note 写明失败，**不假装成功**。
 *
 * 文件系统接触面（[countFiles] / [deleteDir] / [dirExists]）可注入：生产调用不传参数，
 * 单测可整套换成替身（删除失败/读不到这类真实形态不必真造）。
 */
fun cleanupEmptySourceDir(
  dir: File?,
  countFiles: (File) -> Long? = ::countFilesRecursively,
  deleteDir: (File) -> Boolean = { it.deleteRecursively() },
  dirExists: (File) -> Boolean = { it.exists() },
): EmptyDirCleanup {
  if (dir == null) return EmptyDirCleanup(note = "无源目录记录：跳过空目录清理")
  val path = dir.path
  val exists =
    try {
      dirExists(dir)
    } catch (_: Throwable) {
      false
    }
  if (!exists) return EmptyDirCleanup(path = path, note = "源目录不存在（幂等）：$path")
  val files =
    try {
      countFiles(dir)
    } catch (_: Throwable) {
      null
    }
  if (files == null) {
    return EmptyDirCleanup(path = path, note = "源目录内容读不到（保守保留）：$path")
  }
  if (files > 0L) {
    return EmptyDirCleanup(path = path, note = "源目录仍有 $files 个文件（保留，不删）：$path")
  }
  val ok =
    try {
      deleteDir(dir)
    } catch (_: Throwable) {
      false
    }
  val stillThere =
    try {
      dirExists(dir)
    } catch (_: Throwable) {
      true
    }
  return when {
    ok && !stillThere -> EmptyDirCleanup(path = path, removed = true, note = "已删除搬运后的空预览目录：$path")
    else -> EmptyDirCleanup(path = path, note = "空预览目录删除失败（保留，留待人工/下次）：$path")
  }
}

// ---------------------------------------------------------------- 移除（remove，含删数据守卫）

/**
 * 移除计划。
 *
 * **守卫（桌面 `taskops.delete_task_files`）**：只有「目标目录位于受管根内」**且**
 * 「目录名 == 任务 info_hash」时才允许删除——两条缺一即拒绝（[rejected] = true），
 * 宁可留残留目录，也绝不删到用户其它数据。
 */
data class RemovePlan(
  val infoHash: String = "",
  val valid: Boolean = false,
  /** 任务落盘目录（未归一化前的路径；[dataDir] 为 null 表示没有可删目录）。 */
  val dataDir: File? = null,
  /** 是否**真的**删除该目录（默认 false：只有显式要求 + 守卫通过才为 true）。 */
  val deleteData: Boolean = false,
  /** 是否摘掉 `.tasks.json` 里的记录。 */
  val removeEntry: Boolean = false,
  /** 是否删除 `<ih>.fastresume`（清单没了，续传数据也没意义）。 */
  val removeResume: Boolean = false,
  /** 守卫拒绝（显式要求删除但目标不可信）。 */
  val rejected: Boolean = false,
  val error: String = "",
  val note: String = "",
)

/**
 * 计算移除任务该做什么（纯函数；桌面 `remove_task` + `delete_task_files` 的判定部分）。
 *
 * @param savePath 任务落盘目录（磁盘记录里的 `save_path`）
 * @param cacheRoot 缓存根（受管范围；`.preview/<ih>` 与 `downloads/<ih>` 都在它下面）
 * @param deleteData 是否删除数据（**默认 false = 保留数据**，桌面同款）
 * @param extraRoots 额外受管根（例如应用私有根，便于兼容历史记录里的绝对路径）
 */
fun planRemoveTask(
  infoHash: String,
  savePath: String,
  cacheRoot: File,
  deleteData: Boolean = false,
  extraRoots: List<File> = emptyList(),
): RemovePlan {
  val ih = normalizeInfoHash(infoHash)
    ?: return RemovePlan(infoHash = infoHash, valid = false, error = "info_hash 非法，拒绝删除")
  val planBase = RemovePlan(infoHash = ih, valid = true, removeEntry = true, removeResume = true)
  val raw = savePath.trim()
  if (raw.isEmpty()) {
    return planBase.copy(
      deleteData = false,
      rejected = deleteData,
      error = if (deleteData) "拒绝删除：任务没有可核验的落盘目录" else "",
      note = "任务无落盘目录记录：只摘记录 + 删 fastresume",
    )
  }
  val dir = File(raw)
  val roots = listOf(cacheRoot) + extraRoots
  val inside = roots.any { isWithin(it, dir) }
  val base = dir.name.trim().lowercase()
  val trusted = inside && base == ih
  if (!deleteData) {
    return planBase.copy(deleteData = false, dataDir = dir, note = "保留数据（显式未要求删除）：${dir.path}")
  }
  if (!trusted) {
    // 守卫拒绝：目录不在受管范围内，或目录名与任务键不符（共享目录/用户目录的典型形态）
    val why = if (!inside) "不在受管目录内" else "目录名与 info_hash 不符（'$base'）"
    return planBase.copy(
      deleteData = false,
      dataDir = dir,
      rejected = true,
      error = "拒绝删除非受管任务目录（$why）：${dir.path}",
      note = "守卫拒绝删除，数据保留",
    )
  }
  return planBase.copy(deleteData = true, dataDir = dir, note = "按显式要求删除数据目录：${dir.path}")
}

/** [applyRemoveTask] 的结果（全部普通类型，UI/日志直接用）。 */
data class RemoveOutcome(
  val resumeDeleted: Boolean = false,
  val dataDeleted: Boolean = false,
  /** 数据目录**被尝试删除但没删掉**（拒删 / 抛异常 / 假成功）、与 [dataDeleted] 互斥——如实报告用。 */
  val dataDeleteFailed: Boolean = false,
  val rejected: Boolean = false,
  val note: String = "",
)

/**
 * 执行移除的 IO 部分（**纯 IO，单测可直接验证守卫不变量**）。
 *
 * 顺序与桌面一致：先删 fastresume，再删数据目录（仅当 [RemovePlan.deleteData]）。
 * 目录不存在也算成功（幂等：不存在即达成）。
 *
 * **如实报告**：「删成功」一律以 [dirExists]**复核**为准，不看删除实现的返回值——
 * `deleteRecursively()` 会吞掉删除失败（Windows 上文件被占用/防病毒时偶发），
 * 假成功（返回 true 而目录仍在）与拒删、抛异常一视同仁：不计入 [RemoveOutcome.dataDeleted]，
 * 改记 [RemoveOutcome.dataDeleteFailed] 并在 note 里写明数据仍在。
 *
 * @param deleteDir 目录删除实现（默认 [File.deleteRecursively]）。
 * @param dirExists 目录存在性判定（默认 [File.exists]）。
 *
 * [deleteDir] / [dirExists] 是本函数与文件系统的**全部接触面**（移除只做目录级删除，
 * 不列举目录内容），两者都带默认真实实现：生产调用（`BtEngine.removeTask` /
 * `applyDeletion`）一个参数都不用传，测试却能整套换成内存虚拟树——于是「rmtree 偶发失败」
 * 这种环境噪声不再能让断言随机翻红（见 `TaskOpsTest` 的虚拟树用例）。
 */
fun applyRemoveTask(
  plan: RemovePlan,
  cacheRoot: File?,
  deleteDir: (File) -> Boolean = { it.deleteRecursively() },
  dirExists: (File) -> Boolean = { it.exists() },
): RemoveOutcome {
  if (!plan.valid) return RemoveOutcome(rejected = plan.rejected, note = plan.error)
  val resumeDeleted =
    if (plan.removeResume && cacheRoot != null) ResumeStore.delete(cacheRoot, plan.infoHash) else false
  var dataDeleted = false
  var dataDeleteFailed = false
  val dir = plan.dataDir
  if (plan.deleteData && dir != null) {
    dataDeleted =
      try {
        if (!dirExists(dir)) true else deleteDir(dir) && !dirExists(dir)
      } catch (_: Throwable) {
        false
      }
    dataDeleteFailed = !dataDeleted
  }
  val note =
    if (dataDeleteFailed) "删除数据目录未成功（数据仍在，已如实报告）：${dir?.path}"
    else plan.note.ifEmpty { if (plan.deleteData) "已删除数据目录" else "数据目录保留" }
  return RemoveOutcome(
    resumeDeleted = resumeDeleted,
    dataDeleted = dataDeleted,
    dataDeleteFailed = dataDeleteFailed,
    rejected = plan.rejected,
    note = note,
  )
}
