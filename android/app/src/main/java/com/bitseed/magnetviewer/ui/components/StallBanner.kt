package com.bitseed.magnetviewer.ui.components

import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.bitseed.magnetviewer.theme.LocalPal
import com.bitseed.magnetviewer.theme.LocalShapes

/** 下载停滞提示（`MagnetUiState.stallNote` 的人话渲染）。 */
@Composable
fun StallBanner(note: String?) {
  if (note.isNullOrEmpty()) return
  val pal = LocalPal.current
  val shapes = LocalShapes.current
  Column(
    modifier =
      Modifier.fillMaxWidth()
        .clip(shapes.shapeButton)
        .background(pal.altRow)
        .padding(horizontal = 10.dp, vertical = 8.dp)
  ) {
    Row(verticalAlignment = Alignment.CenterVertically) {
      Text("⚠", fontSize = 13.sp, color = pal.warn, modifier = Modifier.padding(end = 6.dp))
      Text(note, fontSize = 13.sp, color = pal.text, fontWeight = FontWeight.Medium, maxLines = 2)
    }
  }
}
