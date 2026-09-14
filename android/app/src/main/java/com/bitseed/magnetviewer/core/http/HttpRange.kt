package com.bitseed.magnetviewer.core.http

import java.util.Locale

/**
 * HTTP Range 头解析（纯函数，可单测）。
 *
 * 与桌面 `core/stream_server.py` 的口径一致：只取第一个区间（`,` 之后忽略）、
 * `bytes=-N` 按逻辑总长换算后缀、解析失败退化为「无 Range」。
 */
sealed interface RangeSpec {
  /** 没有 Range 头或无法解析：按「无 Range」处理。 */
  data object None : RangeSpec

  /** 可满足区间（闭区间）。[openEnded] 表示请求写成了 `bytes=a-`（到文件尾，无上界）。 */
  data class Bounded(val start: Long, val endInclusive: Long, val openEnded: Boolean) : RangeSpec

  /** 请求超出文件范围（首个字节 >= 逻辑总长）。 */
  data class Unsatisfiable(val logicalSize: Long) : RangeSpec
}

private fun parseLongOrNull(s: String): Long? = s.trim().toLongOrNull()

fun parseRangeHeader(header: String?, logicalSize: Long): RangeSpec {
  if (header == null) return RangeSpec.None
  val h = header.trim()
  if (!h.startsWith("bytes=", ignoreCase = true)) return RangeSpec.None
  val first = h.substring(6).split(',').firstOrNull()?.trim().orEmpty()
  if (first.isEmpty()) return RangeSpec.None
  val dash = first.indexOf('-')
  if (dash < 0) return RangeSpec.None
  val a = first.substring(0, dash)
  val b = first.substring(dash + 1)
  if (a.isEmpty() && b.isEmpty()) return RangeSpec.None
  // bytes=-N（尾缀 N 字节）：相对逻辑总长
  if (a.isEmpty()) {
    val n = parseLongOrNull(b) ?: return RangeSpec.None
    val start = maxOf(0L, logicalSize - n)
    val end = logicalSize - 1
    if (end < 0 || start > end) return RangeSpec.Unsatisfiable(logicalSize)
    return RangeSpec.Bounded(start, end, openEnded = false)
  }
  val start = parseLongOrNull(a) ?: return RangeSpec.None
  if (start >= logicalSize || logicalSize <= 0) return RangeSpec.Unsatisfiable(logicalSize)
  if (b.isEmpty()) return RangeSpec.Bounded(start, logicalSize - 1, openEnded = true)
  val rawEnd = parseLongOrNull(b) ?: return RangeSpec.None
  val end = minOf(rawEnd, logicalSize - 1)
  if (end < start) return RangeSpec.Unsatisfiable(logicalSize)
  return RangeSpec.Bounded(start, end, openEnded = false)
}

/** `Content-Range` 头（206）文本。 */
fun contentRangeHeader(start: Long, endInclusive: Long, logicalSize: Long): String =
  "bytes $start-$endInclusive/$logicalSize"

/** `Content-Range` 头（416）文本：总长恒为逻辑文件大小。 */
fun unsatisfiedRangeHeader(logicalSize: Long): String = "bytes */$logicalSize"

/** 按扩展名给 Content-Type（播放器不依赖它，但保持正确性）。 */
fun contentTypeFor(name: String): String {
  val i = name.lastIndexOf('.')
  if (i <= 0) return "application/octet-stream"
  return when (name.substring(i).lowercase(Locale.ROOT)) {
    ".mp4", ".m4v", ".m4a" -> "video/mp4"
    ".mov" -> "video/quicktime"
    ".mkv" -> "video/x-matroska"
    ".webm" -> "video/webm"
    ".ts" -> "video/mp2t"
    ".avi" -> "video/x-msvideo"
    ".flv" -> "video/x-flv"
    ".wmv" -> "video/x-ms-wmv"
    ".mpg", ".mpeg" -> "video/mpeg"
    ".rmvb" -> "application/vnd.rn-realmedia-vbr"
    ".3gp" -> "video/3gpp"
    ".jpg", ".jpeg" -> "image/jpeg"
    ".png" -> "image/png"
    ".gif" -> "image/gif"
    ".webp" -> "image/webp"
    ".bmp" -> "image/bmp"
    else -> "application/octet-stream"
  }
}
