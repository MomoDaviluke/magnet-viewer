package com.bitseed.magnetviewer.ui.components

import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.material3.Button
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.ModalBottomSheet
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.bitseed.magnetviewer.theme.LocalPal
import com.bitseed.magnetviewer.theme.LocalShapes
import androidx.compose.material3.MaterialTheme
import androidx.compose.foundation.layout.heightIn

/**
 * 删除确认底部 Sheet（G-01 / 方案 B §4·§5）：
 *
 * - 两选件顺序固定 = [DeleteConfirmLogic.ORDER]（KEEP → DELETE），**默认焦点 = 保留**（安全档）；
 * - 两级视觉分级：KEEP = 中性（[LocalPal.bgInput] 底）、DELETE 用 [LocalPal.dangerSoft] 底 + [LocalPal.danger] 字；
 * - 之间 16dp 间隔（[DeleteConfirmLogic.GAP_DP])、确认外抛 `true`/`false`/`null`（null = 取消，不删除）。
 */
@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun DeleteConfirmSheet(
  taskName: String,
  onConfirm: (deleteData: Boolean?) -> Unit,
  onDismiss: () -> Unit,
) {
  val pal = LocalPal.current
  val shapes = LocalShapes.current
  var choice by remember { mutableStateOf<DeleteConfirmLogic.Choice?>(DeleteConfirmLogic.Choice.KEEP) }

  ModalBottomSheet(onDismissRequest = onDismiss) {
    Column(modifier = Modifier.fillMaxWidth().padding(18.dp)) {
      Text("移除任务", style = MaterialTheme.typography.titleMedium, color = pal.text)
      Spacer(Modifier.height(4.dp))
      Text(taskName, style = MaterialTheme.typography.bodySmall, color = pal.textMuted, maxLines = 1, overflow = TextOverflow.Ellipsis)
      Spacer(Modifier.height(18.dp))
      val ordered = DeleteConfirmLogic.ORDER
      ordered.forEachIndexed { i, c ->
        val dangerous = DeleteConfirmLogic.isDangerous(c)
        val label = if (c == DeleteConfirmLogic.Choice.KEEP) "移除（保留数据）" else "移除（删除数据）"
        val bg = if (dangerous) pal.dangerSoft else pal.bgInput
        val fg = if (dangerous) pal.danger else pal.text
        val borderColor = if (choice == c) pal.accent else pal.border
        Column(
          modifier =
            Modifier.fillMaxWidth()
              .clip(shapes.shapeButton)
              .background(bg)
              .border(1.dp, borderColor, shapes.shapeButton)
              .clickable { choice = c }
              .padding(horizontal = 12.dp, vertical = 12.dp)
        ) {
          Row(verticalAlignment = Alignment.CenterVertically) {
            Text(label, style = MaterialTheme.typography.bodySmall, color = fg, fontWeight = FontWeight.Medium, modifier = Modifier.weight(1f))
            if (choice == c) Text("✓", style = MaterialTheme.typography.bodySmall, color = pal.accent)
          }
        }
        if (i < ordered.size - 1) Spacer(Modifier.height(DeleteConfirmLogic.GAP_DP.dp))
      }
      Spacer(Modifier.height(18.dp))
      Row {
        Button(
          onClick = { onConfirm(DeleteConfirmLogic.confirm(choice)); onDismiss() },
          shape = shapes.shapeButton,
        ) { Text("确认移除", style = MaterialTheme.typography.bodySmall) }
        TextButton(
          modifier = Modifier.heightIn(min = 48.dp),
          onClick = { onConfirm(null); onDismiss() }) { Text("取消", style = MaterialTheme.typography.bodySmall, color = pal.accent) }
      }
    }
  }
}
