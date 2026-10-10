package com.bitseed.magnetviewer.engine

/**
 * 开播门控的文案与状态（**D-01 的修复落点**；纯函数，JVM 可测）。
 *
 * 现场（QA-2026-09-14）：慢源（做种端上传限速 300 KB/s）点「预览」→ 30s 门控超时 →
 * 播放区纯黑、文件行的「预览」按钮消失、状态行仍写「预览中」；而做种端一直在上传，
 * 最终把 31,567,624 字节全部下完 —— 源并不慢到「无做种」，文案却写
 * 「索引块/头部数据等待超时（可能无做种）」，误导用户。
 */

/**
 * 开播门控超时（毫秒；C22 对齐桌面 `ui/stream_host.py:167` `GATE_WAIT_TIMEOUT_MS = 60s`）。
 *
 * 旧值 30s 的注释声称「与桌面 main_window 的等待口径一致」已过时（桌面现为 60s）：
 * 安卓更早放弃 → 慢链路上失败率升高。判据同步对齐桌面 `stream_host.py:256-258`：
 * 连续前缀 ≥ `min(1MB, 文件大小)`（不再 `> 0` 就放行）+ 尾部入口就绪。
 */
const val PREVIEW_GATE_TIMEOUT_MS: Long = 60_000L

/** 开播门控的「头部连续前缀」就绪阈值（字节上限；实际取 `min(此值, 文件大小)`，对齐桌面 stream_host.py:257）。 */
const val PREVIEW_GATE_CONTIG_BYTES: Long = 1024L * 1024L

/**
 * 门控超时的原因文案 —— **按 peer 数区分**：
 * - `peers > 0`：已经连上做种端，只是超时窗口内没等到入口数据 → 「源较慢」（**不许**说「可能无做种」）；
 * - `peers == 0`：一个 peer 都没有 → 「可能无做种」是诚实的。
 */
fun previewGateTimeoutNote(peers: Int, timeoutSeconds: Int = (PREVIEW_GATE_TIMEOUT_MS / 1000L).toInt()): String =
  if (peers > 0) {
    "索引块/头部数据等待超时（源较慢：已连上 $peers 个 peer，${timeoutSeconds} 秒内未收到入口数据）"
  } else {
    "索引块/头部数据等待超时（peer 0：可能无做种）"
  }

/** 门控超时后的状态行文案（**不得**再写「预览中」）。 */
fun previewGateFailedStatus(peers: Int): String = "预览未开始：" + previewGateTimeoutNote(peers) + "，可点「重试」再试一次"
