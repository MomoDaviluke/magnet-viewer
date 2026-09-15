package com.bitseed.magnetviewer.ui.components

import com.bitseed.magnetviewer.ui.magnet.TaskRow
import kotlin.math.roundToInt

/**
 * TaskCard 的**纯逻辑**（JVM 单测可直接断言，不依赖 Compose 运行时）：
 *
 * - **D-03 同一取整函数**：跳转条「跳转 81%」与提示「已跳转 82%」必须用同一个 [percentOf] 源。
 * - **C-02 百分比零选不显**：`percentText` 为空（选中字节 0：仅解析 / 优先级全 0）时返回 ""。
 * - **B-02 失败任务**：`FAILED` 时「转正/暂停/继续」无效，返回 [true] 让屏幕禁用这些操作。
 * - 完成态提示「完成不再影响速度」（优先级控制完成后禁用）。
 */
object TaskCardLogic {
  val FAILED = "FAILED"
  val COMPLETED = "COMPLETED"

  /**
   * 拖动条与提示两处共用的**同一取整**（f 0..1 → 0..100，四舍五入到最近的整数）。
   *
   * 坑：**必须在 Float 空间做大数运算**再取整。写成 `(fraction * 100.0)` 会先把 Float 提升成
   * Double，`0.815f` 的二进制表示（0.81499999…）被如实放大 → `81.4999…` → 取整成 81，
   * 与「四舍五入」直觉不符（单测 `percentOf_roundsConsistently` 就锁这条：0.815f 必须 = 82）。
   */
  fun percentOf(fraction: Float): Int =
    if (fraction.isNaN()) 0 else (fraction * 100f).roundToInt().coerceIn(0, 100)

  /** 百分比文本（空 = 选中字节为 0 / 不可知，UI 一律不显示「(N%)」）。 */
  fun percentText(row: TaskRow): String = if (row.percentText.isEmpty()) "" else row.percentText

  /** 状态是否 FAILED（B-02 禁用转正/暂停/继续，允许「重试」）。 */
  fun isFailed(state: String): Boolean = state.uppercase() == FAILED

  /** 「失败任务禁用 转正/暂停/继续」（B-02）。 */
  fun shouldDisableOps(state: String): Boolean = isFailed(state)

  /** 完成态提示文本（优先级完成态禁用 + 放 summary）。 */
  fun completionNote(state: String): String = if (state.uppercase() == COMPLETED) "完成不再影响速度" else ""

  /** 优先级完成态禁用（完成态不能改速）。 */
  fun priorityEnabled(state: String): Boolean = state.uppercase() != COMPLETED
}
