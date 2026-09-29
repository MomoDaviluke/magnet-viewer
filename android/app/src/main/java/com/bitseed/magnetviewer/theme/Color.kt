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

  // ---- 方案 B（卡片重构）新增 5 个色值（`.hermes/android-qa/polish/README.md` §3）----
  /** 页底色（层 : 页底 < 卡 #FFFFFF < 卡内 inset [altRow]）：`Pal.bg` 与白卡只差 4% 明度，再深一档才立得住。 */
  val surfaceSunken = Color(0xFFEDEFF3)
  /** tonal（浅色下 8% 主色底）按钮的**按下档**：bgSelected 作默认底时，需要更深一档给 pressed。 */
  val tonalPressed = Color(0xFFD8E4FA)
  /** 危险容器底（错误条 / 删除类选项底）：Pal 只有饱和红 `danger`，缺 errorContainer 语义。 */
  val dangerSoft = Color(0xFFFBEAE8)
  /** 危险按下档（删除类按钮 pressed）。 */
  val dangerPressed = Color(0xFFA32C1F)
  /** 成功容器底（已完成 / 播放中芯片底）：[ok] 是深绿文字色，不能当底。 */
  val okSoft = Color(0xFFE9F4EE)
  /** 视频区（恒黑底）占位文字：黑底上需要浅色，且深浅主题都不反转明度。 */
  val videoPlaceholder = Color(0xFFD0D4DA)
}

/**
 * 深色主题专属色值（浅色 [Pal] 之外新增，不复用浅色值换角色）。
 *
 * 背景（审计 P0 三连）：深色原实现把浅色值按角色重新映射，导致
 * `okSoft == ok`（「已完成」徽章绿字绿底）、`dangerSoft` 与 `danger`
 * 对比度约 1.3:1（红字红底）、`altRow == panel`（斑马纹失效）。
 * 深色下 ok/danger 语义反转为「暗底 + 亮字」，故取独立色值。
 */
object DarkPal {
  /** 成功容器底（暗绿）。 */
  val okSoft = Color(0xFF17352A)
  /** 成功文字（亮绿）：深色底上可读。 */
  val ok = Color(0xFF6EE7A0)
  /** 危险容器底（暗红）。 */
  val dangerSoft = Color(0xFF3B1D18)
  /** 危险文字（亮红）：深色底上可读。 */
  val danger = Color(0xFFFF8A75)
  /** 危险按下档（当前无直接消费者，保持语义完整）。 */
  val dangerPressed = Color(0xFF5A2A22)
  /** 交替行 / 卡内 inset（与 panel 0xFF5B6472 错开一档）。 */
  val altRow = Color(0xFF525B68)
  /** 视频区（恒黑底）占位文字：深色下稍柔和一档，仍在黑底上保持可读。 */
  val videoPlaceholder = Color(0xFF9AA3AE)
}
