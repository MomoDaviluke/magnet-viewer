package com.bitseed.magnetviewer.data

import com.bitseed.magnetviewer.core.model.safeRelPath
import com.bitseed.magnetviewer.core.states.DOWNLOAD_STATES
import com.bitseed.magnetviewer.core.states.STATE_COMPLETED
import com.bitseed.magnetviewer.core.states.STATE_DOWNLOADING
import com.bitseed.magnetviewer.core.states.STATE_META_FETCH
import com.bitseed.magnetviewer.core.states.STATE_PAUSED
import com.bitseed.magnetviewer.core.states.STATE_QUEUED
import com.bitseed.magnetviewer.core.states.STATE_STOPPED
import com.bitseed.magnetviewer.core.states.STATE_VALIDATE
import java.io.File

/**
 * 启动恢复的**纯逻辑层**（不碰 libtorrent4j / android）。
 *
 * 为什么单独拆出来：恢复流程里只有「读 resume 字节 → 解码成 AddTorrentParams → addTorrent」
 * 这一步需要 native（宿主 JVM 加载不了 `.so`，单测里实例化原生对象必崩）。
 * 其余判断（来源可用吗 / 用不用 resume / 加完暂停还是续传 / 恢复成什么状态 / 目录安全吗）
 * 全是纯函数，放这里可以在 JVM 单测里逐分支覆盖（见 `RestorePlanTest`）。
 *
 * 与桌面 `core/persist.py: TaskPersistence.restore_task` 逐条对齐：
 * - info_hash 非法 → 该任务 FAILED（留可见状态，不抛异常、不阻断启动）；
 * - save_path 逃出受管范围 → 回落默认目录（[safeTaskSavePath]）；
 * - source 磁力链 → 用磁力链参数（或 resume 参数）；`.torrent` 且文件在 → 用种子文件；
 *   其余 → FAILED「来源不可用」；
 * - resume 缺失 = 正常态（元数据会重新拉）；resume **损坏** = 静默降级为全新加入；
 * - 持久化状态为 PAUSED/STOPPED/COMPLETED → 加完立刻暂停（`pause + unset auto_managed`）；
 *   其余 → 续传（`resume()`）。
 */

/** 恢复时用什么来源把任务加回会话。 */
enum class RestoreSource {
  /** 磁力链（`magnet:` 开头）——最常用。 */
  MAGNET,

  /** `.torrent` 文件且文件确实在。 */
  TORRENT_FILE,

  /** 来源不可用（不是磁力链、`source` 为空、`.torrent` 已被删） → 该任务 FAILED。 */
  UNAVAILABLE,
}

/** 一个任务怎么恢复（纯数据；原生部分由引擎按此执行）。 */
data class RestorePlan(
  val infoHash: String,
  val source: RestoreSource = RestoreSource.UNAVAILABLE,
  /** 来源原文（磁力链 / 种子文件路径）。 */
  val sourceText: String = "",
  /** 落盘目录（已消毒：逃出受管范围时回落默认目录）。 */
  val saveDir: File = File("."),
  /** 是否用 fastresume 重建（false = 元数据会重新拉，不是错误）。 */
  val useResume: Boolean = false,
  /** fastresume 存在但解不开（损坏）——静默降级全新加入。 */
  val resumeBroken: Boolean = false,
  /** 加完是否立刻暂停。 */
  val pauseAfterAdd: Boolean = false,
  /** 加完是否续传（`resume()`）。 */
  val resumeDownload: Boolean = false,
  /** 是否能恢复（false = 该任务 FAILED，[error] 有原因）。 */
  val valid: Boolean = false,
  val error: String = "",
  /** 给日志/UI 的一句话（恢复路径要说清楚）。 */
  val note: String = "",
)

/**
 * 规划的默认落盘目录：`<cacheDir>/.preview/<ih>`（与 `MagnetStorage.previewRoot` /
 * `BtEngine.addMagnet` 同一算法；ih 非法时归一到 `tmp`，不拼出穿越路径）。
 */
fun previewDirFor(cacheDir: File, infoHash: String): File {
  val ih = normalizeInfoHash(infoHash)
  val sub = if (ih == null) ".preview/tmp" else ".preview/" + safeRelPath(ih)
  return File(cacheDir, sub)
}

/** 状态是否属于「加完要暂停」那一档（桌面：`st in (PAUSED, STOPPED, COMPLETED)`）。 */
fun shouldPauseAfterRestore(state: String): Boolean =
  state.trim().uppercase() in setOf(STATE_PAUSED, STATE_STOPPED, STATE_COMPLETED)

/** 状态是否属于「加完要续传」那一档（桌面：`st not in (PAUSED, STOPPED, COMPLETED)`）。 */
fun shouldResumeDownload(state: String): Boolean = !shouldPauseAfterRestore(state)

/**
 * 恢复后的状态（桌面 `restore_task` 的状态派生）：
 * 持久化状态合法则保留；元数据已在 → `META_FETCH/QUEUED/VALIDATE` 提升为 `DOWNLOADING`；
 * 元数据还没到（无 resume）→ `META_FETCH`（**元数据会重新拉，不报错、不卡住**）。
 */
fun stateAfterRestore(persistedState: String, hasMetadata: Boolean, paused: Boolean): String {
  val st = persistedState.trim().uppercase()
  val base = if (st in DOWNLOAD_STATES) st else (if (hasMetadata) STATE_DOWNLOADING else STATE_META_FETCH)
  if (paused) return base // 暂停/停止/完成：原样保留
  if (!hasMetadata) return STATE_META_FETCH
  return if (base in setOf(STATE_META_FETCH, STATE_QUEUED, STATE_VALIDATE)) STATE_DOWNLOADING else base
}

/**
 * 规划单个任务的恢复。
 *
 * @param resumeBytesPresent 磁盘上是否存在 `<ih>.fastresume`
 * @param resumeDecoded      引擎侧是否成功把该文件解成 `AddTorrentParams`（native；JVM 单测里直接传布尔）
 * @param torrentFileExists  判定 `.torrent` 来源是否还在（默认查文件系统；单测可注入）
 */
fun planRestore(
  task: TaskRecord,
  cacheDir: File,
  resumeBytesPresent: Boolean,
  resumeDecoded: Boolean,
  torrentFileExists: (String) -> Boolean = { p -> File(p).isFile },
): RestorePlan {
  val ih = normalizeInfoHash(task.infoHash)
  if (ih == null) {
    return RestorePlan(
      infoHash = task.infoHash,
      valid = false,
      error = "重启恢复失败：任务缺少合法 info_hash",
      note = "info_hash 非法",
    )
  }
  val saveDir = safeTaskSavePath(cacheDir, previewDirFor(cacheDir, ih), task.savePath)
  val source = task.source.trim()
  val src: RestoreSource
  when {
    source.startsWith("magnet:") -> src = RestoreSource.MAGNET
    source.endsWith(".torrent") && torrentFileExists(source) -> src = RestoreSource.TORRENT_FILE
    else -> src = RestoreSource.UNAVAILABLE
  }
  if (src == RestoreSource.UNAVAILABLE) {
    return RestorePlan(
      infoHash = ih,
      source = src,
      sourceText = source,
      saveDir = saveDir,
      valid = false,
      error = "重启恢复失败：来源不可用",
      note = "来源不可用（source='${source.take(48)}'）",
    )
  }
  val useResume = resumeBytesPresent && resumeDecoded
  val broken = resumeBytesPresent && !resumeDecoded
  val pause = shouldPauseAfterRestore(task.state)
  val note =
    when {
      useResume -> "命中 fastresume（续传数据可用）"
      broken -> "fastresume 损坏，静默按全新加入（元数据会重新拉）"
      else -> "无 fastresume（元数据会重新拉）"
    }
  return RestorePlan(
    infoHash = ih,
    source = src,
    sourceText = source,
    saveDir = saveDir,
    useResume = useResume,
    resumeBroken = broken,
    pauseAfterAdd = pause,
    resumeDownload = !pause,
    valid = true,
    error = "",
    note = note,
  )
}

// ---------------------------------------------------------------- 删除（不变量 C）

/** 删除任务的动作清单。 */
data class DeletionPlan(
  val infoHash: String = "",
  val valid: Boolean = false,
  /** 是否从 `.tasks.json` 里摘掉这条记录。 */
  val removeTaskEntry: Boolean = false,
  /** 是否删除 `<ih>.fastresume`（清单没了，续传数据也没意义）。 */
  val removeResumeFile: Boolean = false,
  /**
   * 是否删除用户数据目录（`.preview/<ih>/`）。
   *
   * **默认 false**——删除任务绝不删用户下载的数据；只有调用方**显式**要求
   * （`deleteUserData = true`）且目录确实在应用私有目录内才会为 true。
   */
  val removeUserDataDir: Boolean = false,
  val userDataDir: String = "",
  val error: String = "",
)

/**
 * 计算删除任务时该做什么（**不变量：默认绝不删用户数据目录**）。
 *
 * 实现已统一到 [planRemoveTask]（同一套守卫：受管根包含性 + 目录名 == info_hash），
 * 本函数只是把结果适配成旧的 [DeletionPlan] 形状以避免调用方改动。
 */
fun planDeletion(
  infoHash: String,
  userDataDir: File,
  appRoot: File,
  deleteUserData: Boolean = false,
): DeletionPlan {
  val ih = normalizeInfoHash(infoHash)
  if (ih == null) {
    return DeletionPlan(infoHash = infoHash, error = "info_hash 非法，拒绝删除")
  }
  val p = planRemoveTask(ih, userDataDir.path, appRoot, deleteData = deleteUserData)
  return DeletionPlan(
    infoHash = ih,
    valid = p.valid,
    removeTaskEntry = p.removeEntry,
    removeResumeFile = p.removeResume,
    removeUserDataDir = p.deleteData,
    userDataDir = userDataDir.path,
    error = p.error,
  )
}

/** [applyDeletion] 的结果。 */
data class DeletionResult(
  val resumeDeleted: Boolean = false,
  val userDataDeleted: Boolean = false,
  /** 请求删除但**没删成功**（拒删 / 异常 / 嘴上说成功但目录还在）——与 [userDataDeleted] 互斥、如实上报。 */
  val userDataDeleteFailed: Boolean = false,
)

/**
 * 按 [plan] 执行删除（纯 IO，**单测可直接验证「默认保留用户数据」这条不变量**）。
 *
 * [userDataDir] 传进来只是为了让调用方拿到实际目录；真删与否完全由
 * `plan.removeUserDataDir` 决定（它是 `deleteUserData && 守卫通过` 的合取）。
 */
fun applyDeletion(
  plan: DeletionPlan, cacheDir: File?, userDataDir: File?,
  // 与 TaskOps.applyRemoveTask 同款接缝（默认 = 真实行为，生产调用点零改动）：测试可注入虚拟树，
  // 从而断言语义而不依赖"真实 rmtree 一定成功"（Windows 上它偶发失败）。
  deleteDir: (File) -> Boolean = { it.deleteRecursively() },
  dirExists: (File) -> Boolean = { it.exists() },
): DeletionResult {
  if (!plan.valid) return DeletionResult()
  val r =
    applyRemoveTask(
      RemovePlan(
        infoHash = plan.infoHash,
        valid = plan.valid,
        dataDir = userDataDir,
        deleteData = plan.removeUserDataDir,
        removeEntry = plan.removeTaskEntry,
        removeResume = plan.removeResumeFile,
      ),
      cacheDir,
      deleteDir = deleteDir,
      dirExists = dirExists,
    )
  return DeletionResult(resumeDeleted = r.resumeDeleted, userDataDeleted = r.dataDeleted,
                        userDataDeleteFailed = r.dataDeleteFailed)
}
