package com.bitseed.magnetviewer

import androidx.compose.foundation.layout.safeDrawingPadding
import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import androidx.navigation3.runtime.entryProvider
import androidx.navigation3.runtime.rememberNavBackStack
import androidx.navigation3.ui.NavDisplay
import com.bitseed.magnetviewer.ui.magnet.MagnetScreen

/**
 * 应用导航：目前只有一个入口（磁力链预览主界面）。
 * 模板自带的 `MainScreen` / `MainScreenViewModel` 保留给既有单测，不在这里使用。
 */
@Composable
fun MainNavigation() {
  val backStack = rememberNavBackStack(Main)

  NavDisplay(
    backStack = backStack,
    onBack = { backStack.removeLastOrNull() },
    entryProvider =
      entryProvider {
        entry<Main> { MagnetScreen(modifier = Modifier.safeDrawingPadding()) }
      },
  )
}
