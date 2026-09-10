"""设置对话框：界面 / 网络与代理 / 缓存与预览 / 下载（四个分组）。

布局约定（v3 打磨轮，用户反馈「还是没改好」后按控制方定位的三条根因修）：
- 四个 ``QGroupBox`` 区块，每块一个 ``QGridLayout``：两列 —— 标签列
  （``setColumnMinimumWidth(0, LABEL_COL_MIN)`` + 右对齐）与控件列
  （``setColumnStretch(1, 1)``）。**四组用同一套列宽**，标签列天然对齐
  （v2 每组独立 ``QFormLayout``，列宽各算各的，四组标签参差不齐）；
  复选框 / 说明行跨两列（``columnSpan=2``），不再留空标签列。
- 组内间距 / 内边距 / 卡片间距全部走 ``ui.theme`` 的间距 token（SP_*），无魔法数：
  卡片间距 12、组内行列间距 8、组内上下内边距 12。
- 内容整体放进 ``QScrollArea``：内容自然高度超过屏幕时自动出滚动条；
- 底部按钮行右下角对齐、间距 12；按钮文案中文（保存 / 取消）；
- 路径类输入框设完文本后 ``setCursorPosition(0)``（看得到路径起点）并挂
  ``setToolTip(完整路径)``（随内容更新，长路径被截断时可悬停看全）；
- 「代理类型 = 不使用代理（直连）」时，主机 / 端口 / 账号 / 密码 / Peer 五个
  控件联动禁用（灰显），切回 SOCKS5/HTTP 自动恢复可用 —— 直连下这些字段
  本来就无意义，可编辑只会误导。
"""
from __future__ import annotations

import os

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox, QDialog,
                               QDialogButtonBox, QFileDialog, QGridLayout,
                               QFrame, QGroupBox, QHBoxLayout, QLabel,
                               QLineEdit, QMessageBox, QPushButton,
                               QScrollArea, QSpinBox, QVBoxLayout, QWidget)

from core.cache_guard import (CACHE_MARKER, clear_cache_contents,
                              ensure_cache_dir, guard_ok_for_cleanup)
from core.cache_mode import PREVIEW_CACHE_MODES
from core.config import AppConfig
from ui.theme import (SP_LG, SP_MD, SP_SM, THEME_MODES, apply_theme)

PROXY_LABELS = [("none", "不使用代理（直连）"),
                ("socks5", "SOCKS5"),
                ("http", "HTTP")]

# 需要代理才可编辑的控件（直连时五个一起禁用）
PROXY_FIELDS = ("proxy_host", "proxy_port", "proxy_user", "proxy_pass",
                "proxy_peer")

# 阶段 D D2（plan/06 阶段 B 裁掉的 UI）：预览缓存模式下拉的显示文案。
# 值域逐字取 core.cache_mode.PREVIEW_CACHE_MODES——顺序即下拉顺序，
# 新增第三档必先改常量，UI 永不自行发明取值。
CACHE_MODE_LABELS = {
    "convert": "关闭预览后继续缓存（推荐）",
    "hold": "关闭预览即暂停",
}

# 双主题（用户拍板：「浅色为主、深色保留可切」）：界面主题下拉的显示文案。
# 值域逐字取 ui.theme.THEME_MODES——顺序即下拉顺序，新增档位必先改常量，
# UI 永不自行发明取值。中英对照写清，避免用户看不懂档位差异。
THEME_LABELS = {
    "light": "浅色 Light（默认）",
    "dark": "深色 Dark",
    "system": "跟随系统 System（随 Windows 深浅色自动切换）",
}

# ---- 布局 token（全部取自 ui.theme 的 8px 网格，禁止魔法数）----
GRID_SPACING = SP_SM                    # 组内行 / 列间距 = 8
GRID_MARGINS = (SP_MD, SP_MD, SP_MD, SP_MD)   # 组内上下左右内边距 = 12
LABEL_COL_MIN = 112                     # 标签列最小宽（四组一致 → 列宽对齐）
CARD_SPACING = SP_MD                    # 四张分组卡片之间的间距 = 12
BUTTON_SPACING = SP_MD                  # 右下角保存 / 取消间距 = 12


def _grid() -> QGridLayout:
    """四个分组共用的网格规格（列宽 / 间距 / 边距 / 拉伸一次定义）。

    ``setColumnMinimumWidth(0, LABEL_COL_MIN)`` 让**四组标签列等宽**
    （v2 每组各算各的 QFormLayout，标签列参差不齐）；``setColumnStretch(1, 1)``
    让控件列吃掉剩余宽度；复选框/说明行用 ``columnSpan=2`` 跨两列。
    """
    g = QGridLayout()
    g.setSpacing(GRID_SPACING)
    g.setContentsMargins(*GRID_MARGINS)
    g.setColumnMinimumWidth(0, LABEL_COL_MIN)
    g.setColumnStretch(1, 1)
    return g


def _add_row(grid: QGridLayout, row: int, label: str, field) -> QLabel:
    """一条「标签 + 控件」行：标签右对齐落在标签列，控件落在控件列。"""
    lab = QLabel(label)
    lab.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
    grid.addWidget(lab, row, 0)
    grid.addWidget(field, row, 1)
    return lab


def _add_span(grid: QGridLayout, row: int, widget) -> None:
    """复选框 / 说明行：跨两列（不留空标签列，也就不会有灰带残留）。"""
    grid.addWidget(widget, row, 0, 1, 2)


def _note(text: str) -> QLabel:
    """次级说明文案（样式：ui/theme.py QLabel#fieldNote）。"""
    label = QLabel(text)
    label.setObjectName("fieldNote")
    label.setWordWrap(True)
    return label


def _path_edit(text: str, placeholder: str) -> QLineEdit:
    """路径类输入框：初始光标停在**开头** + 悬停显示完整路径。

    长路径在输入框里会被截断（光标停在开头只能看到前半段），
    tooltip 随 ``textChanged`` 实时更新成完整值，悬停即可看全。
    """
    edit = QLineEdit(text)
    edit.setPlaceholderText(placeholder)
    edit.setCursorPosition(0)
    edit.setToolTip(text)
    edit.textChanged.connect(edit.setToolTip)
    return edit


def _path_row(edit: QLineEdit, button: QPushButton) -> QWidget:
    """路径行容器：输入框 + 浏览… 按钮。

    容器是普通 ``QWidget``——全局 ``QWidget {{ background: BG }}`` 会把它涂成
    窗口灰底（贴在白卡片上就是一条灰带），故命名 ``#formRow`` 由 QSS 声明透明
    （见 ui/theme.py）。
    """
    holder = QWidget()
    holder.setObjectName("formRow")
    lay = QHBoxLayout(holder)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(SP_SM)
    lay.addWidget(edit, 1)
    lay.addWidget(button)
    return holder


class SettingsDialog(QDialog):
    def __init__(self, cfg: AppConfig, cache_dir: str, on_clear_cache=None,
                 keep_dirs_get=None, parent=None):
        super().__init__(parent)
        self.cfg = cfg
        self.cache_dir = cache_dir
        self._on_clear_cache = on_clear_cache  # 由主窗口注入：停止预览并清缓存
        # 阶段 C C1：活任务落盘目录提供器（主窗口注入 session.protected_dirs
        # 闭包——UI 不直闯 core 内部）。对话框清理编辑框当前值时，命中的
        # 目录连同内容整体跳过，防 convert 转正任务（住 .preview/<ih>）被
        # 手动清理误删。None = 基线行为（无活任务复核）。
        self._keep_dirs_get = keep_dirs_get
        self.setWindowTitle("设置")
        self.setMinimumWidth(600)

        g_ui, g_net, g_cache, g_dl = self._build_groups()

        note = _note("提示：代理、超时、限速、日志开关与界面主题保存后立即生效；"
                     "缓存目录、默认下载目录与并发数修改需重启程序；"
                     "预览缓存上限在下次切换预览文件时生效；"
                     "预览缓存模式在下次关闭预览时生效。设置持久化于本机"
                     "（Windows 注册表 Bitseed\\MagnetViewer）。")

        self.btn_clear = QPushButton("立即清理缓存")
        self.btn_clear.clicked.connect(self._clear_cache)

        # 内容容器（QScrollArea 的 widget）：高度 = 完整内容自然高度，
        # 屏幕装得下就不出滚动条；装不下才滚。
        self._content = QWidget()
        cl = QVBoxLayout(self._content)
        cl.setContentsMargins(0, 0, 0, 0)
        cl.setSpacing(CARD_SPACING)
        for group in (g_ui, g_net, g_cache, g_dl):
            cl.addWidget(group)
        cl.addWidget(note)
        bottom = QHBoxLayout()
        bottom.addWidget(self.btn_clear)
        bottom.addStretch(1)
        cl.addLayout(bottom)
        cl.addStretch(1)

        scroll = QScrollArea()
        scroll.setWidget(self._content)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._scroll = scroll

        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        self.btn_save = buttons.button(QDialogButtonBox.Save)
        self.btn_cancel = buttons.button(QDialogButtonBox.Cancel)
        if self.btn_save is not None:
            self.btn_save.setText("保存")
            self.btn_save.setObjectName("primary")  # 主按钮样式：ui/theme.py #primary
        if self.btn_cancel is not None:
            self.btn_cancel.setText("取消")
        box_layout = buttons.layout()
        if box_layout is not None:
            box_layout.setSpacing(BUTTON_SPACING)
        self._buttons = buttons

        layout = QVBoxLayout(self)
        layout.setContentsMargins(SP_LG, SP_LG, SP_LG, SP_LG)
        layout.setSpacing(CARD_SPACING)
        layout.addWidget(scroll, 1)
        # 右下角：保存 / 取消（间距 12，右对齐）
        layout.addWidget(buttons, 0, Qt.AlignRight)
        # 代理字段启用态联动（初值即按当前代理类型算好）
        self._sync_proxy_state()
        self.resize(max(600, self.sizeHint().width()), self._fit_height())

    # ---------- 分组构造 ----------

    def _build_groups(self) -> tuple:
        """四个分组区块（界面 / 网络与代理 / 缓存与预览 / 下载）。"""
        return self._group_ui(), self._group_network(), \
            self._group_cache(), self._group_download()

    def _group_ui(self) -> QGroupBox:
        box = QGroupBox("界面")
        grid = _grid()

        # 双主题：界面主题下拉（值域 = ui.theme.THEME_MODES；保存即热切换）
        self.theme = QComboBox()
        for v in THEME_MODES:
            self.theme.addItem(THEME_LABELS[v], v)
        _theme = str(self.cfg.get("ui_theme") or "light").strip().lower()
        _theme_idx = self.theme.findData(_theme)
        self.theme.setCurrentIndex(_theme_idx if _theme_idx >= 0 else 0)
        self.theme.setToolTip(
            "浅色 Light：亮色界面（默认）；\n"
            "深色 Dark：暗色界面（视频场景观感）；\n"
            "跟随系统 System：随 Windows 应用深浅色自动切换。\n"
            "**选择后点保存立即生效**，无需重启。")
        _add_row(grid, 0, "界面主题", self.theme)
        _add_span(grid, 1, _note("界面主题保存后立即生效（无需重启）："
                                 "「跟随系统」按 Windows 浅色/深色设置自动选择。"))
        box.setLayout(grid)
        return box

    def _group_network(self) -> QGroupBox:
        box = QGroupBox("网络与代理")
        grid = _grid()

        self.proxy_type = QComboBox()
        for value, label in PROXY_LABELS:
            self.proxy_type.addItem(label, value)
        _values = [v for v, _ in PROXY_LABELS]
        _cur = self.cfg.get("proxy_type")
        self.proxy_type.setCurrentIndex(
            max(0, _values.index(_cur) if _cur in _values else 0))
        _add_row(grid, 0, "代理类型", self.proxy_type)

        self.proxy_host = QLineEdit(str(self.cfg.get("proxy_host")))
        self.proxy_host.setPlaceholderText("例如 127.0.0.1")
        _add_row(grid, 1, "代理主机", self.proxy_host)

        self.proxy_port = QSpinBox()
        self.proxy_port.setRange(1, 65535)
        self.proxy_port.setValue(int(self.cfg.get("proxy_port")))
        _add_row(grid, 2, "代理端口", self.proxy_port)

        self.proxy_user = QLineEdit(str(self.cfg.get("proxy_user")))
        self.proxy_user.setPlaceholderText("可留空")
        _add_row(grid, 3, "账号（可空）", self.proxy_user)
        self.proxy_pass = QLineEdit(str(self.cfg.get("proxy_pass")))
        self.proxy_pass.setPlaceholderText("可留空")
        self.proxy_pass.setEchoMode(QLineEdit.Password)   # 密码掩码（不回显明文）
        _add_row(grid, 4, "密码（可空）", self.proxy_pass)

        self.proxy_peer = QCheckBox("Peer 连接也走代理（保护 IP 隐私，推荐勾选）")
        self.proxy_peer.setChecked(bool(self.cfg.get("proxy_peer")))
        _add_span(grid, 5, self.proxy_peer)

        # 代理类型联动：直连时上面五个字段无意义 → 联动禁用（见 _sync_proxy_state）
        self.proxy_type.currentIndexChanged.connect(self._sync_proxy_state)

        self.timeout = QSpinBox()
        self.timeout.setRange(30, 600)
        self.timeout.setValue(int(self.cfg.get("metadata_timeout")))
        self.timeout.setSuffix(" 秒")
        _add_row(grid, 6, "磁力链元数据超时", self.timeout)
        box.setLayout(grid)
        return box

    def _group_cache(self) -> QGroupBox:
        box = QGroupBox("缓存与预览")
        grid = _grid()

        self.cache_edit = _path_edit(
            str(self.cfg.get("cache_dir") or self.cache_dir),
            "留空 = 系统临时目录/magnet_viewer_cache（重启生效）")
        btn = QPushButton("浏览…")
        btn.clicked.connect(self._pick_dir)
        _add_row(grid, 0, "缓存目录", _path_row(self.cache_edit, btn))

        self.clear_on_exit = QCheckBox("退出时清理预览缓存")
        self.clear_on_exit.setChecked(bool(self.cfg.get("clear_cache_on_exit")))
        _add_span(grid, 1, self.clear_on_exit)

        # 阶段 A A4 + 审查 Minor 10（文案诚实化）：关窗收尾已异步化——给用户
        # 一句预期说明即可，**不要**把「清理缓存要数秒」写成已证事实：实测
        # 200MB 缓存 rmtree 仅约 15ms；退出时的几秒来自 session.shutdown
        # （2s join + 最多 3s fastresume drain）+ 会话析构 + server.shutdown
        # ≈0.5s，与缓存大小基本无关。只承诺「有进度显示」，不承诺耗时时长。
        _add_span(grid, 2, _note("勾选后退出时会清理预览缓存"
                                 "（退出界面会显示进度）"))

        self.cache_limit = QSpinBox()
        self.cache_limit.setRange(0, 1048576)
        self.cache_limit.setSuffix(" MB")
        self.cache_limit.setSpecialValueText("不限制")   # 0 值显示"不限制"
        self.cache_limit.setValue(int(self.cfg.get("cache_limit_mb") or 0))
        self.cache_limit.setToolTip(
            "预览缓存超过此值时，自动按最久未活跃顺序清理旧预览数据；\n"
            "已下载文件（downloads/）不受影响。切换预览文件时生效。")
        _add_row(grid, 3, "预览缓存上限", self.cache_limit)

        # 阶段 D D2：预览缓存模式（convert=关预览自动转正继续缓存 / hold=冻结）
        self.cache_mode = QComboBox()
        for v in PREVIEW_CACHE_MODES:
            self.cache_mode.addItem(CACHE_MODE_LABELS[v], v)
        _mode = str(self.cfg.get("preview_cache_mode"))
        _idx = self.cache_mode.findData(_mode)
        self.cache_mode.setCurrentIndex(_idx if _idx >= 0 else 0)
        self.cache_mode.setToolTip(
            "关闭预览（停止预览按钮/切换文件）时，该文件的缓存任务：\n"
            "继续缓存 = 自动转为持久下载任务，全量缓存到预览目录（推荐）；\n"
            "即暂停 = 冻结现有进度，恢复下载需手动添加任务。\n"
            "下次关闭预览时生效。")
        _add_row(grid, 4, "预览缓存模式", self.cache_mode)
        box.setLayout(grid)
        return box

    def _group_download(self) -> QGroupBox:
        box = QGroupBox("下载")
        grid = _grid()

        self.concurrency = QSpinBox()
        self.concurrency.setRange(1, 16)
        self.concurrency.setValue(int(self.cfg.get("default_concurrency")))
        _add_row(grid, 0, "默认并发下载数", self.concurrency)

        self.rate_limit = QSpinBox()
        self.rate_limit.setRange(0, 1048576)
        self.rate_limit.setSuffix(" KB/s")
        self.rate_limit.setSpecialValueText("不限")       # 0 值显示"不限"
        self.rate_limit.setValue(int(self.cfg.get("download_rate_limit") or 0))
        _add_row(grid, 1, "下载限速", self.rate_limit)

        self.download_edit = _path_edit(str(self.cfg.get("download_dir")),
                                        "留空 = 缓存目录/downloads")
        btn_dl = QPushButton("浏览…")
        btn_dl.clicked.connect(self._pick_download_dir)
        _add_row(grid, 2, "默认下载目录",
                 _path_row(self.download_edit, btn_dl))

        self.seed_after = QCheckBox("任务完成后继续做种")
        self.seed_after.setChecked(bool(self.cfg.get("seed_after_complete")))
        _add_span(grid, 3, self.seed_after)

        self.logging_enabled = QCheckBox(
            "启用运行日志（写入系统临时目录，用于排查问题）")
        self.logging_enabled.setChecked(bool(self.cfg.get("logging_enabled")))
        self.logging_enabled.setToolTip(
            "日志文件：%TEMP%\\magnet_viewer_logs\\magnet-viewer.log\n"
            "（单文件 1 MB，保留 3 份）。开关保存后立即生效。")
        _add_span(grid, 4, self.logging_enabled)
        box.setLayout(grid)
        return box

    # ---------- 联动 ----------

    def proxy_fields(self) -> tuple:
        """需要代理才可编辑的五个控件（直连时统一禁用；测试/门禁直接读它）。"""
        return (self.proxy_host, self.proxy_port, self.proxy_user,
                self.proxy_pass, self.proxy_peer)

    def _sync_proxy_state(self, *_args) -> None:
        """代理类型联动：非直连才允许编辑主机 / 端口 / 账号 / 密码 / Peer。

        ``"none"``（不使用代理）时五个控件一起置灰——直连下这些字段本就
        无意义，可编辑只会让人以为设置生效了。取值只认 ``currentData()``
        （= PROXY_LABELS 的 value，与 core.config 存的值域逐字一致）。
        """
        enabled = self.proxy_type.currentData() != "none"
        for widget in self.proxy_fields():
            widget.setEnabled(enabled)

    # ---------- 尺寸 ----------

    def content_height(self) -> int:
        """整块内容的自然高度（分组 + 提示 + 清理行）。

        ``sizeHint()`` 被 QScrollArea 上限截断，量不出真实内容高度；截图与
        「屏幕装得下就不滚动」的自适应窗口都要这个真实值，故单独暴露。
        """
        return self._content.sizeHint().height()

    def full_height(self) -> int:
        """内容 + 底部按钮行的完整自然高度（不按屏幕封顶；截图/自适应用）。"""
        return (self.content_height() + self._buttons.sizeHint().height()
                + self.layout().spacing()
                + self.layout().contentsMargins().top()
                + self.layout().contentsMargins().bottom())

    def _fit_height(self) -> int:
        """窗口高度：内容装得下就全展开，超过屏幕可用高度则封顶（随后可滚）。"""
        want = self.full_height()
        screen = QApplication.primaryScreen()
        if screen is not None:
            avail = int(screen.availableGeometry().height() * 0.92)
            return max(360, min(want, avail))
        return want

    # ---------- 内部 ----------

    def _pick_dir(self):
        d = QFileDialog.getExistingDirectory(self, "选择缓存目录")
        if d:
            self.cache_edit.setText(d)
            self.cache_edit.setCursorPosition(0)   # 路径显示开头

    def _pick_download_dir(self):
        d = QFileDialog.getExistingDirectory(self, "选择默认下载目录")
        if d:
            self.download_edit.setText(d)
            self.download_edit.setCursorPosition(0)   # 路径显示开头

    def _clear_cache(self):
        """「立即清理缓存」：所见即所得——清理编辑框当前值（含未保存的改动）。

        守卫：非受管缓存目录（无标记文件/高风险目录）拒绝清理并明确提示。
        内容删除统一走 `clear_cache_contents()` 的保留名单——downloads/
        （用户下载数据）、.tasks.json（任务清单）、.resume/（续传数据）
        绝不参与清理（历史缺陷：此处曾再以 `_rmtree_quiet` 无名单清空
        整个目录，误删用户下载数据，P0-1）。阶段 C C1：清理前经注入的
        `keep_dirs_get()` 复核活任务落盘目录（convert 转正任务住
        .preview/<ih>，连同内容整体跳过）；未注入 = 基线行为。
        """
        target = (self.cache_edit.text().strip() or self.cache_dir)
        if not guard_ok_for_cleanup(target):
            QMessageBox.warning(
                self, "拒绝清理",
                f"「{target}」不是受管的缓存目录（缺少标记文件 {CACHE_MARKER}，"
                f"或属于磁盘根/用户数据目录），已取消清理，防止误删。")
            return
        if self._on_clear_cache is not None:
            self._on_clear_cache()   # 停预览等准备工作（针对当前会话目录）
        try:
            keep = set(self._keep_dirs_get() or ()) if (
                self._keep_dirs_get is not None) else set()
        except Exception:
            keep = set()             # 名单故障退回基线，绝不阻断清理
        removed = clear_cache_contents(target, keep_dirs=keep)
        os.makedirs(target, exist_ok=True)
        QMessageBox.information(self, "清理完成",
                                f"缓存已清理{'（' + str(removed) + ' 项）' if removed else ''}")

    def _save(self):
        # 代理配置校验：选了代理类型但主机为空 → 会静默回落直连（隐私勾选失效）
        if (self.proxy_type.currentData() in ("socks5", "http")
                and not self.proxy_host.text().strip()):
            QMessageBox.warning(
                self, "代理配置不完整",
                "已选择代理类型但未填写代理主机。保存后实际会绕过代理直连，"
                "「Peer 连接走代理」将形同虚设。请填写主机（例如 127.0.0.1），"
                "或把代理类型改为「不使用代理」。")
            return
        # 缓存目录保存前做守卫校验：盘符根/用户数据目录直接拒绝保存
        cache_path = self.cache_edit.text().strip()
        try:
            if cache_path:
                ensure_cache_dir(cache_path)
        except ValueError as e:
            QMessageBox.warning(self, "缓存目录无效", str(e))
            return
        self.cfg.set("proxy_type", self.proxy_type.currentData())
        self.cfg.set("proxy_host", self.proxy_host.text().strip())
        self.cfg.set("proxy_port", self.proxy_port.value())
        self.cfg.set("proxy_user", self.proxy_user.text())
        self.cfg.set("proxy_pass", self.proxy_pass.text())
        self.cfg.set("proxy_peer", self.proxy_peer.isChecked())
        self.cfg.set("metadata_timeout", self.timeout.value())
        self.cfg.set("cache_dir", cache_path)
        self.cfg.set("clear_cache_on_exit", self.clear_on_exit.isChecked())
        self.cfg.set("default_concurrency", self.concurrency.value())
        self.cfg.set("download_dir", self.download_edit.text().strip())
        self.cfg.set("seed_after_complete", self.seed_after.isChecked())
        self.cfg.set("download_rate_limit", self.rate_limit.value())
        self.cfg.set("cache_limit_mb", self.cache_limit.value())
        self.cfg.set("preview_cache_mode", self.cache_mode.currentData())
        self.cfg.set("logging_enabled", self.logging_enabled.isChecked())
        # 双主题：写配置 + **保存即热切换**（重建 QSS 挂回 QApplication，
        # 已存在控件由 Qt re-polish + theme._repaint_open_widgets 立即换色，
        # 无需重启；system 档在此刻解析为实际深浅）。
        mode = self.theme.currentData() or "light"
        self.cfg.set("ui_theme", mode)
        apply_theme(QApplication.instance(), str(mode))
        self.accept()
