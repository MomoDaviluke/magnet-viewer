package com.bitseed.magnetviewer.ui.components

import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxHeight
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.material3.Button
import androidx.compose.material3.ButtonDefaults
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.bitseed.magnetviewer.theme.LocalPal
import com.bitseed.magnetviewer.theme.LocalShapes
import com.bitseed.magnetviewer.ui.magnet.TaskRow

/**
 * **TaskCard**（方案 B「一任务一卡」）：
 *
 * 大卡内主信息 = 大号百分比（20sp tabular，空 = 不显）+ 进度条 + 状态徽章 + 优先级（完成态禁用 +
 * 提示「完成不再影响速度」）+ 详情折叠（`expanded: Boolean`，默认收起，只显 12sp dim 摘要）+ 操作
 * （主操作 = tonal 蓝字）。失败态禁用「转正/暂停/继续」给「重试」（B-02）。
 */
@Composable
fun TaskCard(
  row: TaskRow,
  onFocus: (String) -> Unit = {},
  onPause: (String) -> Unit = {},
  onResume: (String) -> Unit = {},
  onPromote: (String) -> Unit = {},
  onRemove: (String) -> Unit = {},
  onPriority: (String, Int) -> Unit = { _, _ -> },
  /** 详情折叠开关（B-04 收开发者文案 + 工程信息 一起进此处）。 */
  onToggleDetail: (String) -> Unit = {},
  /** 详情是否展开（默认收起）。 */
  detailOpen: Boolean = false,
) {
  val pal = LocalPal.current
  val shapes = LocalShapes.current
  val failed = TaskCardLogic.isFailed(row.state)
  val completionNote = TaskCardLogic.completionNote(row.state)
  val percentText = TaskCardLogic.percentText(row)

  Column(
    modifier =
      Modifier.fillMaxWidth()
        .clip(shapes.shapeCard)
        .background(pal.panel)
        .clickable { onFocus(row.infoHash) }
        .padding(16.dp)
  ) {
    Row(verticalAlignment = Alignment.CenterVertically) {
      Column(modifier = Modifier.weight(1f)) {
        Row(verticalAlignment = Alignment.CenterVertically) {
          Text(
            (if (row.focused) "▶ " else "") + row.name,
            modifier = Modifier.weight(1f),
            fontSize = 15.sp,
            fontWeight = FontWeight.SemiBold,
            color = pal.text,
            maxLines = 1,
            overflow = TextOverflow.Ellipsis,
          )
          StatusChip(row.state)
        }
        Spacer(Modifier.height(4.dp))
        // 大号百分比 / 次级行
        Row(verticalAlignment = Alignment.CenterVertically) {
          if (percentText.isNotEmpty()) {
            Text(
              percentText,
              fontSize = 20.sp,
              fontWeight = FontWeight.SemiBold,
              color = pal.text,
            )
            Spacer(Modifier.width(8.dp))
          }
          Text(
            "总大小 ${row.totalSizeText} · 已下载 ${row.downloadedText}" +
              (if (row.rateText.isNotEmpty()) " · ${row.rateText}" else ""),
            fontSize = 13.sp,
            color = pal.textMuted,
            maxLines = 1,
            overflow = TextOverflow.Ellipsis,
          )
        }
      }
      // 主操作：预览视频 = tonal（每卡仅一个主色实心为「解析」，在输入区）
    }
    if (percentText.isNotEmpty()) {
      Spacer(Modifier.height(8.dp))
      ProgressBar(fraction = row.progressPercent / 100f)
      Spacer(Modifier.height(6.dp))
    }
    // 完成提示 / 错误条
    if (completionNote.isNotEmpty()) {
      Text(completionNote, fontSize = 12.sp, color = pal.textMuted)
    }
    if (row.error.isNotEmpty()) {
      ErrorBanner(text = row.error)
    }
    if (row.note.isNotEmpty()) {
      Text(row.note, fontSize = 12.sp, color = pal.ok)
    }
    // 优先级（完成态禁用 + 提示）
    Row(verticalAlignment = Alignment.CenterVertically) {
      Text("优先级", fontSize = 12.sp, color = pal.textDim)
      for (p in 0..3) {
        TextButton(
          onClick = { onPriority(row.infoHash, p) },
          enabled = TaskCardLogic.priorityEnabled(row.state) || p != row.priority,
        ) {
          Text(
            "P$p",
            fontSize = 12.sp,
            color = if (p == row.priority) pal.accent else pal.textMuted,
            fontWeight = if (p == row.priority) FontWeight.SemiBold else FontWeight.Normal,
          )
        }
      }
      if (TaskCardLogic.completionNote(row.state).isNotEmpty()) {
        Text("完成不再影响速度", fontSize = 12.sp, color = pal.textDim)
      }
    }
    // 操作（失败态禁用 转正/暂停/继续，给重试）
    Row(verticalAlignment = Alignment.CenterVertically) {
      if (failed) {
        TextButton(onClick = { onFocus(row.infoHash) }) {
          Text("重试", fontSize = 13.sp, color = pal.accent, fontWeight = FontWeight.Medium)
        }
      } else {
        TextButton(
          onClick = { onPromote(row.infoHash) },
          enabled = !TaskCardLogic.shouldDisableOps(row.state),
        ) { Text(if (row.savePath.contains("/downloads/")) "已转正" else "转正", fontSize = 13.sp, color = pal.accent) }
        TextButton(
          onClick = { onPause(row.infoHash) },
          enabled = !TaskCardLogic.shouldDisableOps(row.state),
        ) { Text("暂停", fontSize = 13.sp, color = pal.accent) }
        TextButton(
          onClick = { onResume(row.infoHash) },
          enabled = !TaskCardLogic.shouldDisableOps(row.state),
        ) { Text("继续", fontSize = 13.sp, color = pal.accent) }
      }
      TextButton(onClick = { onRemove(row.infoHash) }) {
        Text("移除", fontSize = 13.sp, color = pal.danger)
      }
      TextButton(onClick = { onToggleDetail(row.infoHash) }) {
        Text(if (detailOpen) "收起详情" else "详情", fontSize = 13.sp, color = pal.accent)
      }
    }
    // 详情折叠（工程信息收这里，默认收起）
    if (detailOpen) {
      Column(modifier = Modifier.fillMaxWidth().background(pal.altRow, shapes.shapeCard).padding(12.dp)) {
        Text("已下载：${row.downloadedText} · 状态 ${row.state} · 保存到 ${row.savePath}", fontSize = 12.sp, color = pal.textDim)
        if (row.files.isNotEmpty()) {
          Spacer(Modifier.height(4.dp))
          row.files.forEach { f ->
            Text("${f.name} · ${f.sizeText}" + (if (f.isVideo) " · 视频" else ""), fontSize = 12.sp, color = pal.text, maxLines = 1, overflow = TextOverflow.Ellipsis)
          }
        }
      }
    }
  }
}

/** 状态徽章（用 okSoft / dangerSoft / altRow 底）。 */
@Composable
private fun StatusChip(state: String) {
  val pal = LocalPal.current
  val shapes = LocalShapes.current
  val (bg, fg) =
    when (state.uppercase()) {
      "DOWNLOADING", "已下载" -> pal.altRow to pal.textMuted
      "COMPLETED" -> pal.okSoft to pal.ok
      "FAILED" -> pal.dangerSoft to pal.danger
      else -> pal.altRow to pal.textMuted
    }
  Column(modifier = Modifier.clip(shapes.shapeChip).background(bg).padding(horizontal = 10.dp, vertical = 4.dp)) {
    Text(state, fontSize = 12.sp, color = fg, fontWeight = FontWeight.Medium, maxLines = 1)
  }
}

/** 错误横幅（浅红底 + 圆角 12sp）。 */
@Composable
private fun ErrorBanner(text: String) {
  val pal = LocalPal.current
  val shapes = LocalShapes.current
  Column(
    modifier = Modifier.fillMaxWidth().clip(shapes.shapeButton).background(pal.dangerSoft).padding(horizontal = 8.dp, vertical = 6.dp)
  ) {
    Text(text, fontSize = 12.sp, color = pal.danger, maxLines = 2, overflow = TextOverflow.Ellipsis)
  }
}

/** 细进度条（共享给 TaskCard / PreviewPane）。 */
@Composable
private fun ProgressBar(fraction: Float) {
  val pal = LocalPal.current
  val shapes = LocalShapes.current
  Column(
    modifier =
      Modifier.fillMaxWidth().height(6.dp).clip(shapes.shapeChip).background(pal.border)
  ) {
    Column(
      modifier = Modifier.fillMaxHeight().fillMaxWidth(fraction.coerceIn(0f, 1f)).background(pal.accent)
    ) {}
  }
}
