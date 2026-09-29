package com.bitseed.magnetviewer.theme

import androidx.compose.runtime.staticCompositionLocalOf
import androidx.compose.ui.unit.Dp
import androidx.compose.ui.unit.dp

/**
 * 间距令牌（审计 2026-09-29 §4.2）：4dp 网格，五档。
 *
 * 背景：迁移前 padding/Spacer 混用 4/6/8/10/12/16 六种值（grep 计数
 * 8dp×30、4dp×17、6dp×16、12dp×12、16dp×10、10dp×8），6 与 10 不在
 * 网格上。规则：
 * - [xs]=4   紧凑元素间距（图标与文字、行内分隔）
 * - [sm]=8   组件间默认间距（迁移前 6dp 的一律归并到这里）
 * - [md]=12  卡片内区块间距（迁移前 10dp 的一律归并到这里）
 * - [lg]=16  卡片内边距 / 页面边距
 * - [xl]=24  区块间大分隔
 *
 * 与 [UiShapes]、[Typography] 同级，经 [LocalSpacing] 下发。
 */
data class Spacing(
  val xs: Dp = 4.dp,
  val sm: Dp = 8.dp,
  val md: Dp = 12.dp,
  val lg: Dp = 16.dp,
  val xl: Dp = 24.dp,
)

/** 当前生效的间距令牌（Light/Dark 共用一套）。 */
val LocalSpacing = staticCompositionLocalOf { Spacing() }
