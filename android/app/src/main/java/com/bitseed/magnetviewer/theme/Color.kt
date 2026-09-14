package com.bitseed.magnetviewer.theme

import androidx.compose.ui.graphics.Color

/**
 * 桌面 `ui/theme.py:37-58` 的 LIGHT 色板，逐值对齐（本 demo 只做浅色）。
 *
 * 统一放 [Pal] 对象里：避免与 Compose 的 `Text` / `Surface` 等名字冲突
 * （Kotlin 对同名 import 直接报 "Conflicting import"）。
 */
object Pal {
  val bg = Color(0xFFF4F5F7) // 窗口底
  val panel = Color(0xFFFFFFFF) // 面板 / 卡片
  val bgInput = Color(0xFFFFFFFF) // 输入 / 下拉
  val bgHover = Color(0xFFEEF0F3) // 悬停
  val bgPressed = Color(0xFFE6E9EE) // 按下
  val bgSelected = Color(0xFFE7EFFD) // 选中行
  val border = Color(0xFFE1E4E9) // 常规边框
  val borderStrong = Color(0xFFC9CFD8) // 分组 / 聚焦前边框
  val text = Color(0xFF1F2328) // 主文字
  val textMuted = Color(0xFF5B6472) // 次级
  val textDim = Color(0xFF8A929F) // 三级
  val accent = Color(0xFF2563EB) // 强调
  val accentHover = Color(0xFF3B78F0)
  val accentPressed = Color(0xFF1D4FD7)
  val ok = Color(0xFF1A7F47) // 完成 / 做种
  val warn = Color(0xFF9A6700) // 警告 / 限速
  val danger = Color(0xFFC0392B) // 失败
  val altRow = Color(0xFFF3F5F8) // 交替行
}
