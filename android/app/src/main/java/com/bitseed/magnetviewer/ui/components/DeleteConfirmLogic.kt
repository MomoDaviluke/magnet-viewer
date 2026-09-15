package com.bitseed.magnetviewer.ui.components

/**
 * 删除确认 Sheet 的**纯逻辑**（JVM 单测可直接断言）：
 *
 * - 两个选件顺序固定：**保留数据**（安全，无图标）在前、**删除数据**（危险，`dangerSoft` 底）在后，
 *   之间 16dp 间隔（G-01 / 方案 B §5）；
 * - 默认焦点 = 保留数据（防止误触危险档）；
 * - `confirm()` 只在明确点了其中一个后返回非 null。
 */
object DeleteConfirmLogic {
  /** 两个选件的固定顺序（第一个 = 默认焦点/安全档）。 */
  enum class Choice { KEEP, DELETE }

  /** 选件顺序（默认焦点在上 = KEEP）。 */
  val ORDER = listOf(Choice.KEEP, Choice.DELETE)

  /**
   * 选件视觉级：KEEP = 中性（无危险色）、DELETE = `dangerSoft` 底。
   * G-01：两个移除按钮做**视觉分级**（不是平级)。
   */
  fun isDangerous(choice: Choice): Boolean = choice == Choice.DELETE

  /** 层间间隔（G-01：危险与保留 之间 16dp；用 dp 常量进组件，这里给断言源）。 */
  val GAP_DP = 16

  /**
   * 确认的结果（`null` = 未选择/取消，不触发删除）。
   * 删除类永远以外层取消为主，**focus 默认在 KEEP**。
   */
  fun confirm(choice: Choice?): Boolean? =
    when (choice) {
      null -> null
      Choice.KEEP -> false
      Choice.DELETE -> true
    }
}
