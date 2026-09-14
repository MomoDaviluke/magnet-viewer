package com.bitseed.magnetviewer.core.model

/**
 * 一次元数据解析的完整结果（桌面 `core/models.py:ParseResult` 的字段级对齐）。
 *
 * 磁盘路径由 data 层按 `root/cache/.preview/<infoHash>/<file.path>` 拼装，
 * 这里不含任何平台路径，保持纯逻辑可单测。
 */
data class ParseResult(
  val infoHash: String,
  val name: String,
  val totalSize: Long,
  val pieceSize: Int,
  val numPieces: Int,
  val files: List<TorrentFile>,
  val trackers: List<String> = emptyList(),
  val comment: String = "",
  val createdBy: String = "",
  val source: String = "magnet",
) {
  /** 界面展示用文件列表（过滤 .pad 对齐填充文件）。 */
  val viewFiles: List<TorrentFile>
    get() = files.filter { !it.isPad }

  val videos: List<TorrentFile>
    get() = files.filter { it.isVideo && !it.isPad }

  val images: List<TorrentFile>
    get() = files.filter { it.isImage && !it.isPad }
}
