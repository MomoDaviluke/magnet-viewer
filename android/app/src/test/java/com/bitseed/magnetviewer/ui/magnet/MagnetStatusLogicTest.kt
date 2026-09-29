package com.bitseed.magnetviewer.ui.magnet

import com.bitseed.magnetviewer.stream.StreamServer
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * **A-02 回归**：启动类一次性提示不许长期驻留状态区。
 *
 * QA-2026-09-14 实测（`.hermes/android-qa/QA-2026-09-14.md` A-02）：
 * 冷启动时的 `启动：磁盘上没有任务清单（首次启动）` 在**解析成功、有任务上屏之后仍留在状态区**，
 * 与「清单已落盘（…）：1 个任务 / 728 字节」等工程信息一起堆在状态行，永不消失。
 *
 * 处置（QA 给的两个选项里选「只在空态展示」）：把启动提示分成两类 ——
 * - **信息型**（`startupInfoOnly = true`）：磁盘状态 / 清单落盘回执 —— **只在还没有任务时展示**；
 * - **告警型**（`startupInfoOnly = false`）：`缓存根不可用` / `引擎恢复未完成（已跳过配额删除）`
 *   —— 这类必须**无条件常驻**，因为它解释的是「为什么这一会话不清理 / 没恢复」，
 *   藏起来会让用户看不懂行为（这也是本修复**不做**「有任务就一律隐藏」的原因）。
 */
class MagnetStatusLogicTest {

  @Test
  fun infoNote_isShownOnlyWhileThereIsNoTask() {
    assertTrue(
      "还没有任务（首次启动空态）→ 启动提示要看得见",
      shouldShowStartupNote(startupInfoOnly = true, taskCount = 0),
    )
    assertFalse(
      "已经有任务上屏 → 信息型启动提示让位（QA A-02：不许长期驻留）",
      shouldShowStartupNote(startupInfoOnly = true, taskCount = 1),
    )
    assertFalse(shouldShowStartupNote(startupInfoOnly = true, taskCount = 3))
  }

  @Test
  fun warningNote_isAlwaysShown() {
    assertTrue(
      "告警型（缓存根不可用 / 引擎恢复未完成）在有任务时也必须常驻",
      shouldShowStartupNote(startupInfoOnly = false, taskCount = 0),
    )
    assertTrue(shouldShowStartupNote(startupInfoOnly = false, taskCount = 2))
  }

  /**
   * **等待预算单调链护栏（2026-09-18 新增）**。
   *
   * 现场：安卓服务端等 30s、而 ExoPlayer 用默认 ~8s 读超时 ⇒ 客户端先放弃、
   * 服务端才回响应，「seek 后必然播放失败」，且报错只出现在播放器侧、
   * 与服务端日志对不上，极难排查。修复靠两侧数值对齐，**不是**靠某侧单点调参。
   *
   * 因此把这条链固化成断言：任一处倒挂（例如有人把读超时降到 10s、
   * 或把服务端等待提到 30s）都会立刻红，而不是等 QA 在设备上撞到。
   *
   * 链：`停滞(≈STALL_POLLS×pollMs) < 服务端等待 < 读超时`
   */
  @Test
  fun streamTimeoutBudget_isMonotonic() {
    val stallMs = StreamServer.STALL_POLLS * StreamServer.DEFAULT_POLL_MS
    assertTrue(
      "停滞提前返回（${stallMs}ms）必须显著短于服务端等待（" +
        "${StreamServer.DEFAULT_WAIT_TIMEOUT_MS}ms），否则「等不出来」会拖满整个超时",
      stallMs < StreamServer.DEFAULT_WAIT_TIMEOUT_MS / 4,
    )
    assertTrue(
      "服务端等待（${StreamServer.DEFAULT_WAIT_TIMEOUT_MS}ms）必须**短于**播放器读超时" +
        "（${STREAM_READ_TIMEOUT_MS}ms）—— 反了就回到「客户端先放弃」的老毛病",
      StreamServer.DEFAULT_WAIT_TIMEOUT_MS < STREAM_READ_TIMEOUT_MS,
    )
    assertTrue(
      "连接超时（${STREAM_CONNECT_TIMEOUT_MS}ms）应远短于读超时：回环连接不该等这么久",
      STREAM_CONNECT_TIMEOUT_MS < STREAM_READ_TIMEOUT_MS / 2,
    )
  }
}
