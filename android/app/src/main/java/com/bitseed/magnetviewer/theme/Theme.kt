package com.bitseed.magnetviewer.theme

import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.lightColorScheme
import androidx.compose.runtime.Composable

/**
 * 浅色主题（本 demo 固定浅色，色值来自桌面 `ui/theme.py` 的 LIGHT 字典）。
 * 圆角 8（控件）/ 10（卡片）、正文 14sp 见 [Typography] 与各屏幕。
 */
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

@Composable
fun MagnetViewerTheme(content: @Composable () -> Unit) {
  MaterialTheme(colorScheme = LightColors, typography = Typography, content = content)
}
