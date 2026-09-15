package com.bitseed.magnetviewer.ui.magnet

/**
 * 主界面**状态区的展示判据**（纯函数，JVM 可测；`MagnetScreen` 只读结果，不自己做判断）。
 *
 * 现状背景（QA-2026-09-14 的 A-02 / B-04）：状态区曾经把「用户要看的」和「工程日志」堆在一起，
 * 而且**启动类一次性提示在有任务之后仍然长期驻留**（`启动：磁盘上没有任务清单（首次启动）`
 * 在解析成功后还挂在状态行上，跟 `清单已落盘（…）：1 个任务 / 728 字节` 挤在一起）。
 */

/**
 * **启动类提示该不该显示**（A-02 的落点）。
 *
 * 启动提示分两类，判据不同：
 * - **信息型**（`startupInfoOnly = true`：磁盘上有没有清单、恢复了几个任务、清单已落盘回执）
 *   → **只在还没有任务时展示**。有任务上屏后它的使命就结束了，继续挂着只是噪音
 *   （QA 原话：这条一次性提示在有任务后仍长期留在状态区）。
 * - **告警型**（`startupInfoOnly = false`：`启动恢复跳过：缓存根不可用`、
 *   `… · 引擎恢复未完成（已跳过配额删除，详见日志）`）
 *   → **无条件常驻**。这类解释的是「为什么这一会话不清理 / 没有恢复」，藏起来用户会看不懂行为。
 *
 * 因此本函数**不是**「有任务就一律隐藏」——只有信息型才让位。
 *
 * @param startupInfoOnly 该提示是否只是信息型（由 VM 在写入 `restoreNote` 时显式标注）
 * @param taskCount 当前屏幕上的任务条数（`MagnetUiState.taskRows.size`）
 */
fun shouldShowStartupNote(startupInfoOnly: Boolean, taskCount: Int): Boolean =
  !startupInfoOnly || taskCount == 0
