package com.bitseed.magnetviewer.engine

import java.util.Locale
import kotlin.math.roundToInt

/**
 * 一个文件的**下载读数**（引擎 → UI；全是普通类型，句柄不出本包）。
 *
 * 口径与桌面 `status["file_progress"][f.index]` 完全一致：**已下载的字节数**
 * （`TorrentHandle.fileProgress()` 的第 index 项），不是磁盘占用、也不是分块位图。
 * 桌面画廊的判据就是 `progress[f.index] >= f.size`，这里把它固化成 [complete]。
 */
data class FileDownloadReading(
  val index: Int,
  val name: String,
  val size: Long,
  val downloadedBytes: Long,
) {
  /** 下载比例 0..1（大小未知按 0）。 */
  val fraction: Double
    get() = fileDownloadFraction(downloadedBytes, size)

  /** 是否下载完成（桌面 `progress[idx] >= f.size`）。 */
  val complete: Boolean
    get() = isFileComplete(downloadedBytes, size)
}

/**
 * 下载比例 0..1（**纯函数**，JVM 可测）。
 *
 * - `size <= 0`（大小未知/空文件）→ 0.0；
 * - `downloadedBytes` 可能**略大于** size（与相邻文件共享的分块、pad 文件）→ 截到 1.0。
 */
fun fileDownloadFraction(downloadedBytes: Long, size: Long): Double {
  if (size <= 0L) return 0.0
  val got = downloadedBytes.coerceAtLeast(0L)
  return minOf(1.0, got.toDouble() / size.toDouble())
}

/** 是否下载完成：`size <= 0` 一律 false（空文件不当作「已下载」，与桌面判据一致）。 */
fun isFileComplete(downloadedBytes: Long, size: Long): Boolean = size > 0L && downloadedBytes >= size

/**
 * 图片在画廊里的状态（**纯函数派生**，见 [deriveImageState]）。
 *
 * UI 只需要这三种状态，不需要知道句柄/位图/流服务的任何细节：
 * 该派生只依赖「已下载字节 + 文件大小 + 本地文件是否已完整落盘」三个可注入事实，
 * 于是可以在 JVM 单测里穷举（不需要真句柄、不需要真磁盘）。
 */
enum class ImageDownloadState {
  /** 已下载完成（可直接解码显示）。 */
  READY,

  /** 下载中（有读数但未达标 → 显示占位 + 进度）。 */
  DOWNLOADING,

  /** 未开始（读数为 0）。 */
  PENDING,
}

/**
 * **未下载图片的下载状态派生**（纯函数；画廊网格与 大图区 都用它）。
 *
 * 判据（与桌面 `ui/gallery.py:_poll_completed` 同口径）：
 * 1. `onDiskComplete == true`（已确认「引擎读数达标 + 磁盘上文件在」）→ [ImageDownloadState.READY]；
 * 2. 否则引擎读数达标（`downloadedBytes >= size`）→ [ImageDownloadState.READY]
 *    （桌面 `progress[f.index] >= f.size`）；
 * 3. 否则有下载读数（`downloadedBytes > 0`）→ [ImageDownloadState.DOWNLOADING]（带进度）；
 * 4. 两者皆无 → [ImageDownloadState.PENDING]（未开始）。
 *
 * ⚠️ **`onDiskComplete` 绝不能由「文件长度 ≥ 声明大小」得出**：libtorrent 打开文件时按全长
 * 预分配（稀疏文件），长度从下载的第一刻就等于全长。实测踩过：用长度判，大图在 0 字节时
 * 就被标成「已下载完成」，解码拿到空内容反复失败（表现为「已下载但一直显示不出来」）。
 * 正确做法在调用方（VM）：长度只用来确认**文件存在**，是否下完看引擎读数。
 */
fun deriveImageState(
  downloadedBytes: Long,
  size: Long,
  onDiskComplete: Boolean,
): ImageDownloadState =
  when {
    onDiskComplete -> ImageDownloadState.READY
    isFileComplete(downloadedBytes, size) -> ImageDownloadState.READY
    downloadedBytes > 0L -> ImageDownloadState.DOWNLOADING
    else -> ImageDownloadState.PENDING
  }

/** 状态的中文文案（画廊占位格 / 状态栏共用，避免两处措辞漂移）。 */
fun imageStateText(state: ImageDownloadState, fraction: Double): String =
  when (state) {
    ImageDownloadState.READY -> "已下载"
    ImageDownloadState.DOWNLOADING ->
      String.format(Locale.ROOT, "下载中 %d%%", (fraction * 100.0).roundToInt().coerceIn(0, 99))
    ImageDownloadState.PENDING -> "未下载 0%"
  }
