package com.bitseed.magnetviewer.ui.gallery

import com.bitseed.magnetviewer.core.model.IMAGE_EXTS
import com.bitseed.magnetviewer.core.model.TorrentFile
import com.bitseed.magnetviewer.core.model.extOfName
import java.util.Locale
import kotlin.math.roundToInt

/**
 * 画廊的**纯逻辑**（对齐桌面 `ui/gallery.py`）：索引/翻页边界、缩放夹取、图片判定、
 * 文案格式化。
 *
 * 为什么单独一个文件：这些规则全是「输入 → 输出」的纯函数，可以脱离 Compose /
 * 句柄 / 磁盘直接在 JVM 单测里穷举边界（首末张、NaN 缩放、无扩展名……）。
 * Compose 侧只负责画，不负责判断。
 */

/** 画廊里的一张图（纯数据；不含任何 Android/句柄类型）。 */
data class GalleryEntry(
  /** 清单里的文件下标（与 `TorrentFile.index` 相同，用作 key 与预览目标定位）。 */
  val index: Int,
  val name: String,
  /** 种子内相对路径（含根目录段；映射磁盘时用它）。 */
  val path: String,
  val size: Long,
)

/** 缩放下限（桌面 `ui/gallery.py`：`max(0.2, min(5.0, …))`）。 */
const val MIN_SCALE = 0.2f

/** 缩放上限。 */
const val MAX_SCALE = 5.0f

/** 放大/缩小步进系数（桌面 Ctrl+滚轮的两个常量：1.15 / 0.87）。 */
const val ZOOM_IN_FACTOR = 1.15f
const val ZOOM_OUT_FACTOR = 0.87f

/**
 * 图片文件名判定：扩展名在 [IMAGE_EXTS] 白名单里（大小写不敏感）。
 *
 * 复用 [extOfName]（逐字对齐 Python `os.path.splitext`）：`.hidden` 无扩展名、
 * `a.` 无扩展名、`.png` 不算、路径里的点不算。
 */
fun isImageFileName(name: String): Boolean = extOfName(name) in IMAGE_EXTS

/**
 * 从文件清单里挑出图片（**过滤 .pad**，保持清单顺序）。
 *
 * 对齐桌面 `ParseResult.images`：`[f for f in self.files if f.is_image and not f.is_pad]`。
 */
fun galleryEntries(files: List<TorrentFile>): List<GalleryEntry> =
  files.filter { it.isImage && !it.isPad }.map { GalleryEntry(it.index, it.name, it.path, it.size) }

/**
 * 索引夹取：返回 `0..count-1` 内的合法下标；`count <= 0`（没有图片）返回 **-1**。
 *
 * 桌面画廊没有显式的夹取（Qt 的行号天然在界内），但「从文件清单点开第 N 张」这条路
 * 会传进来任意下标，越界必须收住——否则大图区会去索引不存在的元素。
 */
fun clampIndex(index: Int, count: Int): Int {
  if (count <= 0) return -1
  if (index < 0) return 0
  if (index > count - 1) return count - 1
  return index
}

/**
 * 翻页：`current + delta` 后夹取。**首张再往前 / 末张再往后都停在原地**（不越界、
 * 不环比），与桌面 `_step` 的 `if 0 <= row < count` 同款（那里的越界表现是「不动」）。
 */
fun stepIndex(current: Int, delta: Int, count: Int): Int {
  if (count <= 0) return -1
  return clampIndex(clampIndex(current, count) + delta, count)
}

/**
 * 大图区下标解析：**负数一律表示「停在缩略图网格」**，不得被夹成 0。
 *
 * 踩过的坑：直接 `clampIndex(-1, 3)` 得 0，于是「点图片行打开画廊」会**直接跳进第一张大图**，
 * 而设计上应当先看到缩略图网格。这条规则固定在纯函数里，UI 与单测共用同一份判断。
 */
fun viewerPosition(position: Int, count: Int): Int = if (position < 0) -1 else clampIndex(position, count)

/** 缩放夹取：`[MIN_SCALE, MAX_SCALE]`；NaN/无穷按 1.0（重置）处理，绝不把 NaN 传给绘制。 */
fun clampScale(scale: Float): Float {
  if (scale.isNaN() || scale.isInfinite()) return 1.0f
  return scale.coerceIn(MIN_SCALE, MAX_SCALE)
}

/** 一次放大/缩小后的比例（先乘系数再夹取）。 */
fun applyZoom(scale: Float, zoomIn: Boolean): Float =
  clampScale(clampScale(scale) * if (zoomIn) ZOOM_IN_FACTOR else ZOOM_OUT_FACTOR)

/** 缩放比例的百分比文案（大图区「缩放 115%」）。 */
fun zoomPercent(scale: Float): Int = (clampScale(scale) * 100f).roundToInt()

/**
 * 大图区的信息行（**逐字对齐桌面 `_show_index` 的 info_label**）：
 * `f"{row + 1} / {len} · {name}（{size}）"`。
 */
fun galleryInfoText(position: Int, count: Int, name: String, sizeText: String): String {
  if (count <= 0) return "该资源中没有图片文件"
  val p = clampIndex(position, count)
  return String.format(Locale.ROOT, "%d / %d · %s（%s）", p + 1, count, name, sizeText)
}
