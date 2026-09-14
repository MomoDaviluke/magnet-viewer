package com.bitseed.magnetviewer.ui.settings

import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.ColumnScope
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Button
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Switch
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
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.ui.text.input.VisualTransformation
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.bitseed.magnetviewer.data.AppSettings
import com.bitseed.magnetviewer.data.CACHE_MODE_CONVERT_LABEL
import com.bitseed.magnetviewer.data.CACHE_MODE_HOLD_LABEL
import com.bitseed.magnetviewer.data.PROXY_TYPE_HTTP
import com.bitseed.magnetviewer.data.PROXY_TYPE_HTTP_LABEL
import com.bitseed.magnetviewer.data.PROXY_TYPE_NONE
import com.bitseed.magnetviewer.data.PROXY_TYPE_NONE_LABEL
import com.bitseed.magnetviewer.data.PROXY_TYPE_SOCKS5
import com.bitseed.magnetviewer.data.PROXY_TYPE_SOCKS5_LABEL
import com.bitseed.magnetviewer.data.THEME_DARK
import com.bitseed.magnetviewer.data.THEME_DARK_LABEL
import com.bitseed.magnetviewer.data.THEME_LIGHT
import com.bitseed.magnetviewer.data.THEME_LIGHT_LABEL
import com.bitseed.magnetviewer.data.THEME_SYSTEM
import com.bitseed.magnetviewer.data.THEME_SYSTEM_LABEL
import com.bitseed.magnetviewer.core.PREVIEW_CACHE_CONVERT
import com.bitseed.magnetviewer.core.PREVIEW_CACHE_HOLD
import com.bitseed.magnetviewer.theme.LocalPal

/**
 * 设置页（磁力链预览器的全部可调项）。
 *
 * 三条约定：
 * 1. **无状态**：只持有本地草稿（[initial]），点「保存并生效」才通过 [onSave] 交出去
 *    （宿主负责 DataStore 落盘 + 调引擎热更新接口）；「返回」不保存；
 * 2. 键名/值域与桌面 `core/config.py` 一致（见 `data/SettingsStore.kt`）；
 * 3. 颜色一律取 [LocalPal]（浅/深两套都来自 `theme/Color.kt` 的 Pal 常量），字号/留白沿用现状。
 */
@Composable
fun SettingsScreen(
  initial: AppSettings,
  note: String,
  cacheUsageText: String = "",
  cacheNote: String = "",
  onClearCache: () -> Unit = {},
  onSave: (AppSettings) -> Unit,
  onBack: () -> Unit,
  modifier: Modifier = Modifier,
) {
  val pal = LocalPal.current
  var draft by remember(initial) { mutableStateOf(initial) }
  // 数字输入框先按文本收，保存时再解析 + 归一（空串/非法 → 默认值）
  var portText by remember(initial) { mutableStateOf(initial.proxyPort.toString()) }
  var rateText by remember(initial) { mutableStateOf(initial.downloadRateLimit.toString()) }
  var timeoutText by remember(initial) { mutableStateOf(initial.metadataTimeout.toString()) }
  var cacheText by remember(initial) { mutableStateOf(initial.cacheLimitMb.toString()) }

  fun collected(): AppSettings =
    draft.copy(
      proxyPort = portText.trim().toIntOrNull() ?: draft.proxyPort,
      downloadRateLimit = rateText.trim().toIntOrNull() ?: draft.downloadRateLimit,
      metadataTimeout = timeoutText.trim().toIntOrNull() ?: draft.metadataTimeout,
      cacheLimitMb = cacheText.trim().toIntOrNull() ?: draft.cacheLimitMb,
    )

  Column(
    modifier = modifier.fillMaxSize().background(pal.bg).padding(16.dp).verticalScroll(rememberScrollState())
  ) {
    Row(verticalAlignment = Alignment.CenterVertically) {
      Text("设置", fontSize = 18.sp, fontWeight = FontWeight.SemiBold, color = pal.text, modifier = Modifier.weight(1f))
      TextButton(onClick = onBack) { Text("返回", fontSize = 13.sp, color = pal.accent) }
    }
    Text("保存即生效 · 键名与桌面版一致（DataStore 持久化）", fontSize = 12.sp, color = pal.textMuted)
    Spacer(Modifier.height(12.dp))

    // ---------------------------------------------------------------- 代理
    PanelCard {
      Text("代理", fontSize = 14.sp, fontWeight = FontWeight.Medium, color = pal.text)
      Spacer(Modifier.height(8.dp))
      ChoiceRow(
        options = listOf(PROXY_TYPE_NONE to PROXY_TYPE_NONE_LABEL, PROXY_TYPE_SOCKS5 to PROXY_TYPE_SOCKS5_LABEL, PROXY_TYPE_HTTP to PROXY_TYPE_HTTP_LABEL),
        selected = draft.proxyType,
        onSelect = { draft = draft.copy(proxyType = it) },
      )
      Spacer(Modifier.height(8.dp))
      Field("代理主机", draft.proxyHost, { draft = draft.copy(proxyHost = it) }, "127.0.0.1 / proxy.local")
      Field("端口", portText, { portText = it }, "1080", numeric = true)
      Field("用户名", draft.proxyUser, { draft = draft.copy(proxyUser = it) }, "可空")
      Field("密码", draft.proxyPass, { draft = draft.copy(proxyPass = it) }, "可空", password = true)
      Spacer(Modifier.height(4.dp))
      SwitchRow("peer 也走代理", "（隐私关键项；tracker 恒走代理）", draft.proxyPeer) { draft = draft.copy(proxyPeer = it) }
    }

    Spacer(Modifier.height(12.dp))

    // ---------------------------------------------------------------- 传输
    PanelCard {
      Text("传输", fontSize = 14.sp, fontWeight = FontWeight.Medium, color = pal.text)
      Spacer(Modifier.height(8.dp))
      Field("下载限速（KB/s，0 = 不限）", rateText, { rateText = it }, "0", numeric = true)
      Field("元数据超时（秒，默认 90）", timeoutText, { timeoutText = it }, "90", numeric = true)
    }

    Spacer(Modifier.height(12.dp))

    // ---------------------------------------------------------------- 预览缓存
    PanelCard {
      Text("预览缓存", fontSize = 14.sp, fontWeight = FontWeight.Medium, color = pal.text)
      Spacer(Modifier.height(8.dp))
      Field("缓存上限（MB，0 = 不限，默认 2048）", cacheText, { cacheText = it }, "2048", numeric = true)
      Spacer(Modifier.height(8.dp))
      // 当前占用（**目录实际字节**口径；「已下载」另算，两者不互换——桌面同款口径说明）
      Text(
        if (cacheUsageText.isNotEmpty()) "当前占用：$cacheUsageText" else "当前占用：统计中…",
        fontSize = 12.sp,
        color = pal.textMuted,
        maxLines = 3,
        overflow = TextOverflow.Ellipsis,
      )
      Spacer(Modifier.height(6.dp))
      Row(verticalAlignment = Alignment.CenterVertically) {
        Button(onClick = onClearCache, shape = RoundedCornerShape(8.dp)) {
          Text("立即清理缓存", fontSize = 13.sp)
        }
        Spacer(Modifier.width(10.dp))
        Text(
          cacheNote.ifEmpty { "清理 <缓存根>/.preview 下的非保护目录（活跃任务目录保留）" },
          modifier = Modifier.weight(1f),
          fontSize = 12.sp,
          color = pal.textMuted,
          maxLines = 3,
          overflow = TextOverflow.Ellipsis,
        )
      }
      Spacer(Modifier.height(8.dp))
      Text("关闭预览时（stopPreview 语义由它决定）", fontSize = 12.sp, color = pal.textMuted)
      Spacer(Modifier.height(6.dp))
      ChoiceRow(
        options = listOf(PREVIEW_CACHE_CONVERT to CACHE_MODE_CONVERT_LABEL, PREVIEW_CACHE_HOLD to CACHE_MODE_HOLD_LABEL),
        selected = draft.previewCacheMode,
        onSelect = { draft = draft.copy(previewCacheMode = it) },
      )
    }

    Spacer(Modifier.height(12.dp))

    // ---------------------------------------------------------------- 界面
    PanelCard {
      Text("界面", fontSize = 14.sp, fontWeight = FontWeight.Medium, color = pal.text)
      Spacer(Modifier.height(8.dp))
      ChoiceRow(
        options = listOf(THEME_LIGHT to THEME_LIGHT_LABEL, THEME_DARK to THEME_DARK_LABEL, THEME_SYSTEM to THEME_SYSTEM_LABEL),
        selected = draft.uiTheme,
        onSelect = { draft = draft.copy(uiTheme = it) },
      )
      Spacer(Modifier.height(8.dp))
      SwitchRow("运行日志", "（关闭后不再产运行日志）", draft.loggingEnabled) { draft = draft.copy(loggingEnabled = it) }
    }

    Spacer(Modifier.height(14.dp))
    Row(verticalAlignment = Alignment.CenterVertically) {
      Button(onClick = { onSave(collected()) }, shape = RoundedCornerShape(8.dp)) {
        Text("保存并生效", fontSize = 14.sp)
      }
      Spacer(Modifier.width(10.dp))
      Text(
        note.ifEmpty { "设置保存在应用内部存储；重启后仍在" },
        modifier = Modifier.weight(1f),
        fontSize = 12.sp,
        color = if (note.startsWith("已保存")) pal.ok else pal.textMuted,
        maxLines = 3,
        overflow = TextOverflow.Ellipsis,
      )
    }
    Spacer(Modifier.height(24.dp))
  }
}

@Composable
private fun Field(
  label: String,
  value: String,
  onChange: (String) -> Unit,
  placeholder: String,
  numeric: Boolean = false,
  password: Boolean = false,
) {
  val pal = LocalPal.current
  Column(modifier = Modifier.fillMaxWidth().padding(vertical = 4.dp)) {
    Text(label, fontSize = 12.sp, color = pal.textMuted)
    Spacer(Modifier.height(4.dp))
    OutlinedTextField(
      value = value,
      onValueChange = onChange,
      modifier = Modifier.fillMaxWidth(),
      singleLine = true,
      textStyle = TextStyle(fontSize = 14.sp, color = pal.text),
      visualTransformation = if (password) PasswordVisualTransformation() else VisualTransformation.None,
      placeholder = { Text(placeholder, fontSize = 13.sp, color = pal.textDim) },
    )
  }
}

@Composable
private fun SwitchRow(label: String, hint: String, checked: Boolean, onChange: (Boolean) -> Unit) {
  val pal = LocalPal.current
  Row(modifier = Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
    Column(modifier = Modifier.weight(1f)) {
      Text(label, fontSize = 14.sp, color = pal.text)
      if (hint.isNotEmpty()) Text(hint, fontSize = 12.sp, color = pal.textMuted)
    }
    Switch(checked = checked, onCheckedChange = onChange)
  }
}

/** 单选行（值 + 显示名）：选中的项用强调色边框 + 选中底色。 */
@Composable
private fun ChoiceRow(options: List<Pair<String, String>>, selected: String, onSelect: (String) -> Unit) {
  val pal = LocalPal.current
  Row(modifier = Modifier.fillMaxWidth()) {
    options.forEach { (value, label) ->
      val on = value == selected
      Box(
        modifier =
          Modifier.weight(1f)
            .padding(end = 6.dp)
            .clip(RoundedCornerShape(8.dp))
            .background(if (on) pal.bgSelected else pal.bgInput)
            .border(1.dp, if (on) pal.accent else pal.border, RoundedCornerShape(8.dp))
            .clickable { onSelect(value) }
            .padding(horizontal = 8.dp, vertical = 10.dp),
        contentAlignment = Alignment.Center,
      ) {
        Text(
          label,
          fontSize = 13.sp,
          color = if (on) pal.text else pal.textMuted,
          fontWeight = if (on) FontWeight.Medium else FontWeight.Normal,
          maxLines = 2,
          overflow = TextOverflow.Ellipsis,
        )
      }
    }
  }
}

@Composable
private fun PanelCard(modifier: Modifier = Modifier, content: @Composable ColumnScope.() -> Unit) {
  val pal = LocalPal.current
  Column(
    modifier =
      modifier
        .fillMaxWidth()
        .clip(RoundedCornerShape(10.dp))
        .background(pal.panel)
        .border(1.dp, pal.border, RoundedCornerShape(10.dp))
        .padding(12.dp),
    content = content,
  )
}
