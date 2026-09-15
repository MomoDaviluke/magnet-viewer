package com.bitseed.magnetviewer.engine

/**
 * 下载停滞看门狗（**C-01 的修复落点**；纯逻辑 + 可注入时钟，JVM 可测）。
 *
 * 现场（QA-2026-09-14）：图片夹具下到 30% 后做种端 `peers=0 rate=0`（环境侧「连上即掉线」），
 * UI 连 dump 四次都只有 `下载中 30%…` —— 用户看不出「源掉了」还是「网慢」，只能干等。
 *
 * 判据（只在**真的没戏**时才说话，避免狼来了）：
 * - 任务处于**下载中/元数据中**（由调用方判定后作为 [sample] 的 `active`）；
 * - **连续 [thresholdSeconds] 秒零字节增长**；
 * - **且 peer = 0**（有 peer 只是慢，不报「掉线」）。
 *
 * 字节一旦增长即复位并撤掉提示（「恢复增长后自动消失」）。
 *
 * **采样对象（N-2，2026-09-15 设备实测新增）**：计时必须跟着「看的是哪个任务」走。
 * 现场：移除旧任务后**重新解析一个新任务**，新任务刚 added（META_FETCH、0 字节），
 * 状态区却已写着 `已 155 秒无数据增长（peer 0）…` —— 计时是从**上一个任务**最后一次字节增长
 * 算起的（两个任务都是 0 字节，`downloadedBytes` 没变化，所以旧逻辑不复位）。
 * 因此 [sample] 多一个 `subject`（infoHash）：**对象一变立刻复位**，新对象从 0 起表。
 */
class StallWatch(
  private val thresholdSeconds: Long = DEFAULT_THRESHOLD_SECONDS,
  private val clock: () -> Long = { System.currentTimeMillis() },
) {

  private var lastBytes: Long = -1L
  private var lastGrowthMs: Long = 0L
  private var current: String? = null
  private var subject: String = ""

  /**
   * 采一次样。返回**当前该显示的提示**（null = 不提示）。
   *
   * @param active 是否处于「下载中/元数据中」（暂停/完成/失败一律 false，且会复位计时）
   * @param downloadedBytes 已下载字节（显示口径：`fileProgress` 求和）
   * @param peers 当前 peer 数
   * @param subject 本次采样针对的对象标识（infoHash）。**与上次不同 → 先复位再起表**
   *   （默认 `""` = 调用方不区分对象，保持旧行为）
   */
  fun sample(active: Boolean, downloadedBytes: Long, peers: Int, subject: String = ""): String? {
    if (subject != this.subject) {
      // 换任务：上一个对象的停滞秒数对新对象毫无意义（N-2）
      this.subject = subject
      reset()
    }
    if (!active) {
      reset()
      return null
    }
    val now = clock()
    if (downloadedBytes != lastBytes) {
      // 有新数据：复位计时并撤掉提示
      lastBytes = downloadedBytes
      lastGrowthMs = now
      current = null
      return null
    }
    if (lastGrowthMs == 0L) {
      // 第一次采样：只起表，不判定
      lastGrowthMs = now
      return null
    }
    val stalledSec = (now - lastGrowthMs) / 1000
    if (peers == 0 && stalledSec >= thresholdSeconds) {
      current = "已 $stalledSec 秒无数据增长（peer 0），源可能已掉线，正在重试"
    } else {
      current = null
    }
    return current
  }

  /** 复位（不处于下载态 / 换了采样对象时调用；`subject` 不清，由 [sample] 维护）。 */
  fun reset() {
    lastBytes = -1L
    lastGrowthMs = 0L
    current = null
  }

  companion object {
    /** 默认阈值：20 秒（QA 报告建议值）。 */
    const val DEFAULT_THRESHOLD_SECONDS: Long = 20L
  }
}
