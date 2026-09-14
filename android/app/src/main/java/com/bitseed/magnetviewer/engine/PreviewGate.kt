package com.bitseed.magnetviewer.engine

/**
 * 开播门控的文案与状态（**D-01 的修复落点**；纯函数，JVM 可测）。
 *
 * 现场（QA-2026-09-14）：慢源（做种端上传限速 300 KB/s）点「预览」→ 30s 门控超时 →
 * 播放区纯黑、文件行的「预览」按钮消失、状态行仍写「预览中」；而做种端一直在上传，
 * 最终把 31,567,624 字节全部下完 —— 源并不慢到「无做种」，文案却写
 * 「索引块/头部数据等待超时（可能无做种）」，误导用户。
 */

/** 开播门控超时（毫秒；30s 与桌面 `main_window` 的等待口径一致）。 */
const val PREVIEW_GATE_TIMEOUT_MS: Long = 30_000L

/**
 * 门控超时的原因文案 —— **按 peer 数区分**：
 * - `peers > 0`：已经连上做种端，只是 30s 内没等到入口数据 → 「源较慢」（**不许**说「可能无做种」）；
 * - `peers == 0`：一个 peer 都没有 → 「可能无做种」是诚实的。
 */
fun previewGateTimeoutNote(peers: Int): String =
  if (peers > 0) {
    "索引块/头部数据等待超时（源较慢：已连上 $peers 个 peer，30 秒内未收到入口数据）"
  } else {
    "索引块/头部数据等待超时（peer 0：可能无做种）"
  }

/** 门控超时后的状态行文案（**不得**再写「预览中」）。 */
fun previewGateFailedStatus(peers: Int): String = "预览未开始：" + previewGateTimeoutNote(peers) + "，可点「重试」再试一次"
