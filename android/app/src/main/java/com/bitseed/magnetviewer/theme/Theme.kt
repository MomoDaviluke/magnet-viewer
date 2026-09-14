package com.bitseed.magnetviewer.theme

import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.darkColorScheme
import androidx.compose.material3.lightColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.runtime.CompositionLocalProvider
import androidx.compose.runtime.staticCompositionLocalOf
import androidx.compose.foundation.isSystemInDarkTheme
import com.bitseed.magnetviewer.data.THEME_LIGHT
import com.bitseed.magnetviewer.data.isDarkTheme

/**
 * 一套界面色板（字段名与 [Pal] 一一对应）。
 *
 * - [Palette.Light] 就是桌面 `ui/theme.py` LIGHT 字典（[Pal]，逐值对齐）；
 * - [Palette.Dark] **不新造色值**：只是把 [Pal] 里已有的 18 个色值按深色语义重新分配
 *   （窗口底取最深的 `text`、面板取 `textMuted`、文字取 `bg`、边框取 `textDim`…），
 *   强调色/成功/警告/危险四色直接沿用。
 */
data class Palette(
  val bg: androidx.compose.ui.graphics.Color,
  val panel: androidx.compose.ui.graphics.Color,
  val bgInput: androidx.compose.ui.graphics.Color,
  val bgHover: androidx.compose.ui.graphics.Color,
  val bgPressed: androidx.compose.ui.graphics.Color,
  val bgSelected: androidx.compose.ui.graphics.Color,
  val border: androidx.compose.ui.graphics.Color,
  val borderStrong: androidx.compose.ui.graphics.Color,
  val text: androidx.compose.ui.graphics.Color,
  val textMuted: androidx.compose.ui.graphics.Color,
  val textDim: androidx.compose.ui.graphics.Color,
  val accent: androidx.compose.ui.graphics.Color,
  val accentHover: androidx.compose.ui.graphics.Color,
  val accentPressed: androidx.compose.ui.graphics.Color,
  val ok: androidx.compose.ui.graphics.Color,
  val warn: androidx.compose.ui.graphics.Color,
  val danger: androidx.compose.ui.graphics.Color,
  val altRow: androidx.compose.ui.graphics.Color,
) {
  companion object {
    /** 浅色：桌面 LIGHT 字典原值（不新造）。 */
    val Light =
      Palette(
        bg = Pal.bg,
        panel = Pal.panel,
        bgInput = Pal.bgInput,
        bgHover = Pal.bgHover,
        bgPressed = Pal.bgPressed,
        bgSelected = Pal.bgSelected,
        border = Pal.border,
        borderStrong = Pal.borderStrong,
        text = Pal.text,
        textMuted = Pal.textMuted,
        textDim = Pal.textDim,
        accent = Pal.accent,
        accentHover = Pal.accentHover,
        accentPressed = Pal.accentPressed,
        ok = Pal.ok,
        warn = Pal.warn,
        danger = Pal.danger,
        altRow = Pal.altRow,
      )

    /** 深色：同一批色值换角色（窗口底=Pal.text、面板=Pal.textMuted、文字=Pal.bg…）。 */
    val Dark =
      Palette(
        bg = Pal.text,
        panel = Pal.textMuted,
        bgInput = Pal.text,
        bgHover = Pal.textMuted,
        bgPressed = Pal.textDim,
        bgSelected = Pal.accentPressed,
        border = Pal.textDim,
        borderStrong = Pal.borderStrong,
        text = Pal.bg,
        textMuted = Pal.borderStrong,
        textDim = Pal.textDim,
        accent = Pal.accentHover,
        accentHover = Pal.accent,
        accentPressed = Pal.accentPressed,
        ok = Pal.ok,
        warn = Pal.warn,
        danger = Pal.danger,
        altRow = Pal.textMuted,
      )
  }
}

/** 当前生效的色板：浅色 [Palette.Light] / 深色 [Palette.Dark]（由 [MagnetViewerTheme] 提供）。 */
val LocalPal = staticCompositionLocalOf { Palette.Light }

/** 浅色主题的 Material 配色（色值全部来自 [Pal]）。 */
private val LightColors =
  lightColorScheme(
    primary = Pal.accent,
    onPrimary = Pal.panel,
    primaryContainer = Pal.bgSelected,
    onPrimaryContainer = Pal.text,
    secondary = Pal.textMuted,
    onSecondary = Pal.panel,
    background = Pal.bg,
    onBackground = Pal.text,
    surface = Pal.panel,
    onSurface = Pal.text,
    surfaceVariant = Pal.bgHover,
    onSurfaceVariant = Pal.textMuted,
    outline = Pal.border,
    outlineVariant = Pal.borderStrong,
    error = Pal.danger,
    onError = Pal.panel,
  )

/** 深色主题的 Material 配色（同样只用 [Pal] 里的色值，按深色语义换角色）。 */
private val DarkColors =
  darkColorScheme(
    primary = Pal.accentHover,
    onPrimary = Pal.bg,
    primaryContainer = Pal.accentPressed,
    onPrimaryContainer = Pal.bg,
    secondary = Pal.borderStrong,
    onSecondary = Pal.text,
    background = Pal.text,
    onBackground = Pal.bg,
    surface = Pal.text,
    onSurface = Pal.bg,
    surfaceVariant = Pal.textMuted,
    onSurfaceVariant = Pal.borderStrong,
    outline = Pal.textDim,
    outlineVariant = Pal.textMuted,
    error = Pal.danger,
    onError = Pal.bg,
  )

/**
 * 应用主题：按 [themeMode]（`light` / `dark` / `system`）选择配色并下发 [LocalPal]。
 *
 * 圆角 8（控件）/ 10（卡片）、正文 14sp 见 [Typography] 与各屏幕；字号/留白不随主题变。
 */
@Composable
fun MagnetViewerTheme(themeMode: String = THEME_LIGHT, content: @Composable () -> Unit) {
  val dark = isDarkTheme(themeMode, isSystemInDarkTheme())
  CompositionLocalProvider(LocalPal provides if (dark) Palette.Dark else Palette.Light) {
    MaterialTheme(colorScheme = if (dark) DarkColors else LightColors, typography = Typography, content = content)
  }
}
