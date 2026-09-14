package com.bitseed.magnetviewer.engine

import com.bitseed.magnetviewer.core.model.ParseResult
import com.bitseed.magnetviewer.core.states.STATE_DOWNLOADING
import com.bitseed.magnetviewer.core.states.STATE_PAUSED

/**
 * 任务进度 / 状态语义的纯函数层（**J-01 + C-02 的修复落点**，JVM 可测、不碰句柄）。
 *
 * 为什么单独成文件：完成判定与百分比口径原先散在 `BtEngine.taskSnapshots()`（要句柄，
 * JVM 测不了）与 UI 的 `TaskSnapshot.progress`（只有「/ 总大小」一种口径）里，
 * 于是既出了「仅解析任务点继续立刻 COMPLETED」（J-01）又出了
 * 「已下载 0 B（100%）」（C-02）。把判据收成纯函数后，两个 bug 都能被单测钉住。
 */

/**
 * 「选中文件」的字节总数 —— 界面百分比的**分母**（桌面口径：`已下载 / 选中字节`）。
 *
 * 三种形态（与调度/下载语义一一对应）：
 * - `download = false` 且没勾选任何文件（**仅解析**任务：文件优先级全 0）→ `0`
 *   —— 没有要下载的东西，分母不成立（进度不可知，UI 不显示百分比）；
 * - 勾选了文件 → 只算这些（转正/预览都是「只下这一个/这几个文件」）；
 * - `download = true` 且勾选为空 = **全选**（`activateTask` 的语义）→ 全部非 `.pad` 文件。
 */
fun selectedBytes(result: ParseResult?, selected: List<String>, download: Boolean): Long {
  val view = result?.viewFiles ?: return 0L
  if (selected.isEmpty() && !download) return 0L
  val picked = if (selected.isEmpty()) view else view.filter { it.path in selected }
  return picked.sumOf { it.size }
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
 * 关键：libtorrent 对「**没有任何想要的数据**」的种子 `isFinished()` 是 **vacuous true**
 * （只解析任务的优先级全 0 → want 0 → 立刻「完成」）。所以完成判定必须同时满足
 * 「真的要数据」（下载型任务 **且** 选中字节 > 0），否则点一下「继续」就会得到
 * 「COMPLETED，下载 0 字节」（QA J-01）。
 */
fun isTaskCompleted(libtorrentFinished: Boolean, download: Boolean, selectedBytes: Long): Boolean =
  libtorrentFinished && download && selectedBytes > 0L

/**
 *「继续」之后任务该显示成什么状态。
 *
 * 仅解析且没选任何文件时**不得**进入 [STATE_DOWNLOADING]——那条路的下场就是被
 * `isTaskCompleted` 之前的老逻辑立刻判成 COMPLETED。此时保持 [STATE_PAUSED]
 * 并给用户一句人话提示（[PARSE_ONLY_NO_SELECTION_NOTE]）。
 */
fun stateAfterResume(download: Boolean, selected: List<String>): String =
  if (download || selected.isNotEmpty()) STATE_DOWNLOADING else STATE_PAUSED

/** 「仅解析任务：未选择要下载的文件」的用户提示（VM 暴露给屏幕的字段见 `MagnetUiState.parseOnlyNote`）。 */
const val PARSE_ONLY_NO_SELECTION_NOTE: String =
  "仅解析任务：未选择要下载的文件（点「转正」或预览某个文件才会开始下载）"
