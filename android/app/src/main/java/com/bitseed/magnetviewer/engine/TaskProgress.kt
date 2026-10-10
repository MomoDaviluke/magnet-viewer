package com.bitseed.magnetviewer.engine

import com.bitseed.magnetviewer.core.model.ParseResult
import com.bitseed.magnetviewer.core.model.TorrentFile
import com.bitseed.magnetviewer.core.states.STATE_DOWNLOADING
import com.bitseed.magnetviewer.core.states.STATE_PAUSED

/**
 * 任务进度 / 状态语义的纯函数层（**J-01 + C-02 的修复落点**，JVM 可测、不碰句柄）。
 *
 * 为什么单独成文件：完成判定与百分比口径原先散在 `BtEngine.taskSnapshots()`（要句柄，
 * JVM 测不了）与 UI 的 `TaskSnapshot.progress`（只有「/ 总大小」一种口径）里，
 * 于是既出了「仅解析任务点继续立刻 COMPLETED」（J-01）又出了
 * 「已下载 0 B（100%）」（C-02）。把判据收成纯函数后，两个 bug 都能被单测钉住。
 *
 * 2026-09-15 复审追加：J-01 的守卫只挡了「没有选中文件」，**没挡「选中了但没下完」**——
 * 真磁力链实测 `（下载 1511277003 字节 / 选中 3591733632 字节）` 仍被判 COMPLETED
 * （预览暂停 / hold 档停止把文件优先级全置 0 → libtorrent `isFinished()` 再次虚真）。
 * 于是完成判定补上 [pickedDoneBytes]（选中集合的已落字节）作为**分子**，与 [selectedBytes]
 * 同一集合、同一夹取规则。
 */

/**
 * **「选中文件」这一集合的唯一解析入口**（J-01 / 假完成 修复的公共分母与分子）。
 *
 * 三种形态（与调度/下载语义一一对应）：
 * - `download = false` 且没勾选任何文件（**仅解析**任务：文件优先级全 0）→ 空集
 *   —— 没有要下载的东西，分母不成立（进度不可知，UI 不显示百分比）；
 * - 勾选了文件 → 只算这些（转正/预览都是「只下这一个/这几个文件」）；
 * - `download = true` 且勾选为空 = **全选**（`activateTask` 的语义）→ 全部非 `.pad` 文件。
 *
 * ⚠️ **`.pad` 文件（libtorrent 的对齐填充）永远不进这个集合**：它既不该进分母，也不该进分子
 * —— 否则「已下载/选中」两个口径会各算各的（这正是 C-02 与假完成的同源病根）。
 */
private fun pickedFiles(result: ParseResult?, selected: List<String>, download: Boolean): List<TorrentFile> {
  val view = result?.viewFiles ?: return emptyList()
  if (selected.isEmpty() && !download) return emptyList()
  return if (selected.isEmpty()) view else view.filter { it.path in selected }
}

/**
 * 「选中文件」的字节总数 —— 界面百分比的**分母**（桌面口径：`已下载 / 选中字节`）。
 */
fun selectedBytes(result: ParseResult?, selected: List<String>, download: Boolean): Long =
  pickedFiles(result, selected, download).sumOf { it.size }

/**
 * 「选中文件**已落盘**的字节」—— 完成判定的**分子**（与 [selectedBytes] 同一集合、同一夹取规则）。
 *
 * 逐文件取值并**按文件大小夹取**：
 * - 读数缺失（`fileProgress` 为 null / 数组比文件数短）→ 该文件按 `0`，**保守**（宁可晚判完成）；
 * - 读数为负 → 按 `0`；
 * - 读数**超过**文件大小（与相邻文件共享分块、`.pad` 相邻）→ 夹到 `size`，
 *   于是「全部选中文件都下完」时分子**恒等于**分母，不会因为夹取方向把完成判丢。
 *
 * @param fileProgress `TorrentHandle.fileProgress()`（按**文件索引**取值的字节数组）
 */
fun pickedDoneBytes(
  result: ParseResult?,
  selected: List<String>,
  download: Boolean,
  fileProgress: LongArray?,
): Long =
  pickedFiles(result, selected, download).sumOf { f ->
    val got = fileProgress?.getOrNull(f.index) ?: 0L
    got.coerceIn(0L, f.size)
  }

/**
 * 进度（0..1）= **已下载 / 选中字节**；分母为 0（没有选中文件）→ `null`（不可知）。
 *
 * `null` 与 `0.0` 是两回事：前者是「没有百分比可显示」，后者是「已选中但一个字节都没下到」。
 */
fun progressOf(downloadedBytes: Long, selectedBytes: Long): Double? {
  if (selectedBytes <= 0L) return null
  val got = if (downloadedBytes < 0L) 0L else downloadedBytes
  return minOf(1.0, got.toDouble() / selectedBytes.toDouble())
}

/**
 * 任务是否该判为**已完成**。
 *
 * 两个已知的「虚真」来源，必须同时挡住：
 * 1. **没有选中文件**：libtorrent 对「没有任何想要的数据」的种子 `isFinished()` 是
 *    **vacuous true**（只解析任务的优先级全 0 → want 0 → 立刻"完成"）。挡法 = `download` **且** 分母 > 0。
 * 2. **选中了但没下完**（2026-09-15 复审新增，真磁力链实测 `下载 1511277003 / 选中 3591733632` 被判完成）：
 *    预览「暂停」/ hold 档「停止预览」会把**全部文件优先级置 0** → wanted = 0 →
 *    libtorrent 的 `isFinished()` **再次虚真**，而此时任务仍是 `DOWNLOADING`、字节一个没多。
 *    挡法 = **分子 ≥ 分母**（[pickedDoneBytes] ≥ [selectedBytes]，同一选中集合、逐文件夹取）。
 *
 * 注意 `libtorrentFinished` 是**必要不充分**条件：它只说明「想要的都到了」，不说明
 * 「我们想让用户拿到的那些到了」。两者必须同时成立。
 */
fun isTaskCompleted(
  libtorrentFinished: Boolean,
  download: Boolean,
  selectedBytes: Long,
  doneBytes: Long,
): Boolean = libtorrentFinished && download && selectedBytes > 0L && doneBytes >= selectedBytes

/**
 *「继续」之后任务该显示成什么状态。
 *
 * 仅解析且没选任何文件时**不得**进入 [STATE_DOWNLOADING]——那条路的下场就是被
 * `isTaskCompleted` 之前的老逻辑立刻判成 COMPLETED。此时保持 [STATE_PAUSED]
 * 并给用户一句人话提示（[PARSE_ONLY_NO_SELECTION_NOTE]）。
 */
fun stateAfterResume(download: Boolean, selected: List<String>): String =
  if (download || selected.isNotEmpty()) STATE_DOWNLOADING else STATE_PAUSED

/**
 * fastresume **周期脏写**的目标筛选（C41，对齐桌面 `core/session.py:823-827`
 * `resume_sweep`）：只有「下载型 + 正在下载中」的任务进周期脏写名单——
 * 暂停/完成/失败态没有新进度，等元数据的任务 result 未就绪（`requestResumeDataFor`
 * 的 C44 守卫同样会挡下，这里按桌面 sweep 的口径先行筛掉）。
 */
fun resumeSweepNeeded(download: Boolean, state: String): Boolean =
  download && state == STATE_DOWNLOADING

/** 「仅解析任务：未选择要下载的文件」的用户提示（VM 暴露给屏幕的字段见 `MagnetUiState.parseOnlyNote`）。 */
const val PARSE_ONLY_NO_SELECTION_NOTE: String =
  "仅解析任务：未选择要下载的文件（点「转正」或预览某个文件才会开始下载）"
