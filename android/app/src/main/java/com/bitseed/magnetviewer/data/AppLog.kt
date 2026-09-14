package com.bitseed.magnetviewer.data

import android.util.Log

/**
 * **运行日志总开关**（设置项 `logging_enabled` 的执行点，修 I-02）。
 *
 * 背景：设置页有「运行日志（关闭后不再产运行日志）」开关，但它原先只做持久化与回读，
 * 没有任何执行点 —— 关掉后业务日志照常刷屏（QA-2026-09-14 的 I-02，实测关开关后仍出 21 行）。
 *
 * 设计（为什么是这样一个对象而不是给每个类加开关）：
 * - **单一开关 + 单一出口**：引擎/VM/前台服务/调度器全部经 [log] 出日志，第一行就早返回；
 * - **可测**：输出落点 [sink] 可替换（单测收集字符串断言「关掉后一条都不出」），
 *   默认落点仍是 logcat（tag 与旧实现一致 = `MagnetViewer`，`logcat -s MagnetViewer` 口径不变）；
 * - **绝不抛**：日志本身不能成为崩溃点（`android.util.Log` 在 JVM 单测里未 mock 会抛，
 *   这里一律接住）。
 *
 * 接线点：`MagnetViewModel.applySettings()`（设置流每次发射都会调）→ [syncFromSettings]；
 * 进程内默认开启（与桌面 `logging_enabled=True` 同）。
 */
object AppLog {

  /** logcat tag（与项目其余日志一致：`logcat -s MagnetViewer`）。 */
  const val TAG: String = "MagnetViewer"

  /** 总开关：设置流加载/保存时更新（见 [syncFromSettings]）。 */
  @Volatile var enabled: Boolean = true

  /** 默认落点：logcat（未 mock 的 JVM 单测会抛，已被接住）。 */
  val defaultSink: (String, String) -> Unit = { tag, message ->
    try {
      Log.i(tag, message)
    } catch (_: Throwable) {
      // android.util.Log 不可用（单测/异常环境）：不能因此崩溃
    }
  }

  /** 实际输出落点（单测替换为收集器）。 */
  @Volatile var sink: (String, String) -> Unit = defaultSink

  /** 普通信息日志（**所有业务日志出口的唯一入口**）。 */
  fun log(message: String) {
    if (!enabled) return
    try {
      sink(TAG, message)
    } catch (_: Throwable) {
      // 落点异常不影响主流程
    }
  }

  /** 警告级日志（同一开关；与 [log] 分开只为语义清晰）。 */
  fun warn(message: String) {
    if (!enabled) return
    try {
      sink(TAG, message)
    } catch (_: Throwable) {
      // 同上
    }
  }

  /** 用一份设置刷新开关（设置流的落点）。 */
  fun syncFromSettings(s: AppSettings) {
    enabled = s.loggingEnabled
  }
}
