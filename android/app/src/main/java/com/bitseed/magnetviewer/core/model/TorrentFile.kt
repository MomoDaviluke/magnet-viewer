package com.bitseed.magnetviewer.core.model

import java.util.Locale

/** 桌面 `core/models.py:VIDEO_EXTS` 的同义词集。 */
val VIDEO_EXTS: Set<String> =
  setOf(
    ".mp4",
    ".mkv",
    ".avi",
    ".mov",
    ".flv",
    ".wmv",
    ".webm",
    ".ts",
    ".m4v",
    ".rmvb",
    ".mpg",
    ".mpeg",
  )

/** 桌面 `core/models.py:IMAGE_EXTS` 的同义词集。 */
val IMAGE_EXTS: Set<String> = setOf(".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".svg")

/**
 * 扩展名（含点，小写），语义对齐 Python `os.path.splitext(name)[1]`：
 * 前导点不算扩展名（`.hidden` → ""），末尾点不算扩展名（`a.` → ""）。
 */
fun extOfName(name: String): String {
  val i = name.lastIndexOf('.')
  if (i <= 0 || i == name.length - 1) return ""
  val e = name.substring(i)
  if (e.contains('/') || e.contains('\\')) return ""
  return e.lowercase(Locale.ROOT)
}

/** 桌面 `core/models.py:human_size` 的逐字移植（1024 进制、B 档取整、其余 1 位小数）。 */
fun humanSize(n: Double): String {
  var v = n
  for (unit in listOf("B", "KB", "MB", "GB", "TB")) {
    if (v < 1024) {
      return if (unit == "B") "${v.toLong()} B" else String.format(Locale.ROOT, "%.1f %s", v, unit)
    }
    v /= 1024
  }
  return String.format(Locale.ROOT, "%.1f PB", v)
}

fun humanSize(n: Long): String = humanSize(n.toDouble())

/**
 * 桌面 `core/models.py:safe_rel_path` 的逐字移植：把种子里的路径段净化成安全相对路径。
 *
 * 逐级丢弃 `.` / `..` / 空段，剥离盘符与根前缀，段内分隔符与 Windows 非法字符换成下划线；
 * 全部丢光时返回 `unnamed`。
 */
fun safeRelPath(vararg segments: String): String {
  val out = ArrayList<String>()
  for (seg in segments) {
    for (raw in seg.replace('\\', '/').split('/')) {
      var part = raw.trim().trim('\u0000')
      if (part.isEmpty() || part == "." || part == "..") continue
      // 剥离盘符（C:）与设备前缀
      if (part.length > 1 && part[1] == ':') part = part.substring(2)
      part = part.replace(':', '_')
      val sb = StringBuilder(part.length)
      for (ch in part) sb.append(if (ch == '<' || ch == '>' || ch == '"' || ch == '|' || ch == '?' || ch == '*') '_' else ch)
      part = sb.toString()
      if (part.isNotEmpty() && part != "." && part != "..") out.add(part)
    }
  }
  return if (out.isEmpty()) "unnamed" else out.joinToString("/")
}

/**
 * 种子中的一个文件（桌面 `core/models.py:TorrentFile` 的字段级对齐）。
 *
 * [path] 为包含种子根名的完整相对路径（`/` 分隔），可直接映射到磁盘缓存目录。
 * [startPiece]/[endPiece] 为闭区间，[offset] 是文件首字节在种子数据流中的偏移。
 */
data class TorrentFile(
  val index: Int,
  val path: String,
  val size: Long,
  val offset: Long,
  val startPiece: Int,
  val endPiece: Int,
) {
  val name: String
    get() = path.substringAfterLast('/')

  val ext: String
    get() = extOfName(name)

  val isVideo: Boolean
    get() = ext in VIDEO_EXTS

  val isImage: Boolean
    get() = ext in IMAGE_EXTS

  /** libtorrent 生成的 piece 对齐填充文件，展示时应过滤。 */
  val isPad: Boolean
    get() = "/.pad/" in "/$path"

  val isPreviewable: Boolean
    get() = (isVideo || isImage) && !isPad
}
