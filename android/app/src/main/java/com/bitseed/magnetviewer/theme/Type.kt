package com.bitseed.magnetviewer.theme

import androidx.compose.material3.Typography
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.sp

/**
 * 字号刻度（`.hermes/android-qa/polish/README.md` §4）：
 * 正文 15sp / 行高 22（新档位，替代旧的 14sp —— QA 第 5 条「正文 15sp 起」）；
 * 大号百分比 20sp / 26 tabular-nums（方案 B 的 KPI，仅给「百分比大数字」用，见 `ui/components/TaskCard.kt`）；
 * 次级 13/20、说明与工程信息 12/18、卡标题 16/22、页标题 20/26（新档位）。
 *
 * 注意：本注释里**不要**写「数字 + 星号 + 斜杠」这种组合（如 20sp*斜杠26 的粗体写法），
 * Kotlin 会把其中的「星号斜杠」当成注释结束符，后面的正文就变成非法代码（实测编译报
 * `Expecting a top level declaration`）。
 */
val Typography =
  Typography(
    bodyLarge = TextStyle(fontFamily = FontFamily.Default, fontWeight = FontWeight.Normal, fontSize = 15.sp, lineHeight = 22.sp),
    bodyMedium = TextStyle(fontFamily = FontFamily.Default, fontWeight = FontWeight.Normal, fontSize = 15.sp, lineHeight = 22.sp),
    bodySmall = TextStyle(fontFamily = FontFamily.Default, fontWeight = FontWeight.Normal, fontSize = 13.sp, lineHeight = 20.sp),
    labelLarge = TextStyle(fontFamily = FontFamily.Default, fontWeight = FontWeight.Medium, fontSize = 15.sp, lineHeight = 22.sp),
    labelMedium = TextStyle(fontFamily = FontFamily.Default, fontWeight = FontWeight.Normal, fontSize = 12.sp, lineHeight = 18.sp),
    titleLarge = TextStyle(fontFamily = FontFamily.Default, fontWeight = FontWeight.SemiBold, fontSize = 20.sp, lineHeight = 26.sp),
    titleMedium = TextStyle(fontFamily = FontFamily.Default, fontWeight = FontWeight.SemiBold, fontSize = 16.sp, lineHeight = 22.sp),
  )
