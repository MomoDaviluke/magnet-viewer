package com.bitseed.magnetviewer.ui.magnet

import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.material3.Icon
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.bitseed.magnetviewer.theme.LocalPal
import com.bitseed.magnetviewer.theme.LocalShapes

/** 空态（方案 B §2）。中心 88px 图标（主色 tonal 底）+ 标题 + 两行人话 + 「示例磁力链」次按钮。 */
@Composable
fun EmptyState(onSample: () -> Unit = {}, sampleLabel: String = "示例磁力链") {
  val pal = LocalPal.current
  val shapes = LocalShapes.current
  Column(
    modifier = Modifier.fillMaxSize().padding(16.dp),
    horizontalAlignment = Alignment.CenterHorizontally,
    verticalArrangement = Arrangement.Center,
  ) {
    Column(
      modifier = Modifier.clip(shapes.shapeChip).background(pal.bgSelected).size(88.dp),
      horizontalAlignment = Alignment.CenterHorizontally,
      verticalArrangement = Arrangement.Center,
    ) {
      Text("🧲", fontSize = 32.sp, color = pal.accent)
    }
    Spacer(Modifier.height(16.dp))
    Text("还没有任务", fontSize = 20.sp, fontWeight = FontWeight.SemiBold, color = pal.text)
    Spacer(Modifier.height(8.dp))
    Text(
      "粘贴磁力链，点「解析」开始",
      fontSize = 15.sp,
      color = pal.textMuted,
      textAlign = TextAlign.Center,
    )
    Text("先看完整文件清单，不下载本体", fontSize = 13.sp, color = pal.textDim, textAlign = TextAlign.Center)
    Spacer(Modifier.height(12.dp))
    OutlinedButton(onClick = onSample, shape = shapes.shapeButton) {
      Text(sampleLabel, fontSize = 13.sp, color = pal.accent)
    }
  }
}
