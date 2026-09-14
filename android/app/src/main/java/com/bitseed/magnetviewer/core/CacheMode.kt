package com.bitseed.magnetviewer.core

/**
 * 预览缓存两档模式（桌面 `core/cache_mode.py` 的逐字移植；值域即校验）。
 *
 * - [PREVIEW_CACHE_CONVERT]（默认）：关闭预览后**只**清 deadline / 还原锚点，
 *   不 pause、不清文件优先级——预览立刻转成正的持久下载（引擎按 file-priority
 *   全量缓存），是否「转正」由调用方（宿主）决定；
 * - [PREVIEW_CACHE_HOLD]：基线行为——停预览 = 冻结（清 deadline + 文件优先级全 0
 *   + 撤 auto_managed + pause）。
 */
const val PREVIEW_CACHE_CONVERT = "convert"
const val PREVIEW_CACHE_HOLD = "hold"

val PREVIEW_CACHE_MODES: List<String> = listOf(PREVIEW_CACHE_CONVERT, PREVIEW_CACHE_HOLD)

/**
 * 归一化 `preview_cache_mode` 的取值（trim + lowercase）。
 * 值域之外返回 null——调用方据此**忽略**本次设置（不改变现状），与桌面
 * 「值域即校验，不新增校验逻辑」一致。
 */
fun normalizePreviewCacheMode(mode: String?): String? {
  val v = mode?.trim()?.lowercase().orEmpty()
  return if (v.isNotEmpty() && v in PREVIEW_CACHE_MODES) v else null
}
