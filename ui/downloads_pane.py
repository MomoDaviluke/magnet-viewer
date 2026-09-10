"""下载管理页（三页签之「下载」）：任务列表 + 详情区。

控件层实现，不接 SessionManager——任务数据由外部轮询注入：
``DownloadsPane.set_tasks(list[dict])``，dict 字段遵循 plan/t1 的
DownloadTask 契约：info_hash / name / total_size / state / progress /
down_rate / eta / error / priority / save_path / selected_files。
（progress 为 0~1 小数，兼容 0~100 百分数；速度/进度为派生字段不落盘。）

用户动作一律走信号（携带任务 dict，由联调层接线到任务 API）：
pause/resume/remove/priority↑↓/opendir/openpreview。
"""
from __future__ import annotations

from PySide6.QtCore import Qt, QItemSelectionModel, Signal
from PySide6.QtGui import (QColor, QPalette, QStandardItem,
                           QStandardItemModel)
from PySide6.QtWidgets import (QApplication, QLabel, QMenu, QMessageBox,
                               QSplitter, QStackedWidget, QStyle,
                               QStyledItemDelegate, QStyleOptionProgressBar,
                               QTreeView, QVBoxLayout, QWidget)

from ui.theme import ACCENT, DANGER, OK, TEXT_DIM, TEXT_MUTED
from core.models import human_size

COL_NAME, COL_SIZE, COL_PROGRESS, COL_SPEED = 0, 1, 2, 3
COLUMNS = ["名称", "大小", "进度", "速度·ETA"]

# 状态 -> (emoji, 中文名, 前景色)：⏳白 / ⏸灰 / ✅绿 / ❌红 / 🌱做种蓝
STATE_META: dict = {
    "QUEUED":      ("⏳", "排队中", TEXT_MUTED),
    "META_FETCH":  ("⏳", "获取元数据", TEXT_MUTED),
    "VALIDATE":    ("⏳", "校验中", TEXT_MUTED),
    "DOWNLOADING": ("⏬", "下载中", ACCENT),
    "PAUSED":      ("⏸", "已暂停", TEXT_DIM),
    "STOPPED":     ("⏹", "已停止", TEXT_DIM),
    "COMPLETED":   ("✅", "已完成", OK),
    "SEEDING":     ("🌱", "做种中", ACCENT),
    "FAILED":      ("❌", "失败", DANGER),
    "DELETED":     ("🗑️", "已删除", TEXT_MUTED),
}
UNKNOWN_STATE = ("⏳", "未知", TEXT_MUTED)


def _state_meta(state: str) -> tuple:
    return STATE_META.get(str(state or "").upper(), UNKNOWN_STATE)


def _progress_value(task: dict) -> float:
    """任务进度归一化为 0~100（progress 支持 0~1 小数或 0~100 百分数）。"""
    try:
        p = float(task.get("progress", 0.0) or 0.0)
    except (TypeError, ValueError):
        return 0.0
    if p > 1.0:
        return max(0.0, min(100.0, p))
    return max(0.0, min(100.0, p * 100.0))


def format_eta(eta) -> str:
    """秒数 -> 可读剩余时间；未知/负数显示 —。"""
    if eta is None:
        return "—"
    try:
        s = float(eta)
    except (TypeError, ValueError):
        return "—"
    if s < 0:
        return "—"
    if s < 60:
        return f"{int(s)} 秒"
    m = int(s // 60)
    if m < 60:
        return f"{m} 分 {int(s % 60)} 秒"
    h = m // 60
    if h < 24:
        return f"{h} 小时 {m % 60} 分"
    return f"{h // 24} 天 {h % 24} 小时"


class ProgressDelegate(QStyledItemDelegate):
    """进度列：以系统进度条样式绘制，分块颜色随任务状态（绿=完成/灰=暂停/红=失败）。"""

    def paint(self, painter, option, index):
        if index.column() != COL_PROGRESS:
            super().paint(painter, option, index)
            return
        try:
            value = float(index.data(Qt.UserRole) or 0.0)
        except (TypeError, ValueError):
            value = 0.0
        task = index.siblingAtColumn(COL_NAME).data(Qt.UserRole) or {}
        state = str(task.get("state", "")).upper()

        opt = QStyleOptionProgressBar()
        opt.rect = option.rect.adjusted(6, 4, -6, -4)
        opt.minimum, opt.maximum = 0, 100
        opt.progress = int(max(0, min(100, value)))
        opt.text = f"{value:.0f}%"
        opt.textVisible = True
        opt.state = QStyle.State_Enabled
        pal = QPalette(option.palette)
        if state == "COMPLETED":
            pal.setBrush(QPalette.Highlight, QColor(OK))
        elif state in ("PAUSED", "STOPPED"):
            pal.setBrush(QPalette.Highlight, QColor(TEXT_DIM))
        elif state == "FAILED":
            pal.setBrush(QPalette.Highlight, QColor(DANGER))
        else:
            pal.setBrush(QPalette.Highlight, QColor(ACCENT))
        opt.palette = pal
        QApplication.style().drawControl(QStyle.CE_ProgressBar, opt, painter,
                                         self.parent())
        # 聚焦/选中背景仍由默认绘制负责：进度条外沿补绘制选中底色
        if option.state & QStyle.State_Selected:
            painter.fillRect(option.rect,
                             option.palette.highlight().color().lighter(160))


class DownloadsPane(QWidget):
    """三页签之「下载」组件：QSplitter = 上任务列表 + 下详情区。"""

    pause_requested = Signal(object)         # 暂停（携带任务 dict）
    resume_requested = Signal(object)        # 恢复（含失败重试）
    remove_requested = Signal(object)        # 删除任务
    priority_up_requested = Signal(object)   # 优先级 ↑
    priority_down_requested = Signal(object) # 优先级 ↓
    opendir_requested = Signal(object)       # 打开所在目录
    openpreview_requested = Signal(object)   # 打开预览

    def __init__(self, parent=None):
        super().__init__(parent)
        self._tasks: list[dict] = []

        self.tree = QTreeView(self)
        self.tree.setObjectName("taskList")   # 样式：ui/theme.py QTreeView#taskList
        self._model = QStandardItemModel(0, len(COLUMNS), self)
        self._model.setHorizontalHeaderLabels(COLUMNS)
        self.tree.setModel(self._model)
        self.tree.setItemDelegateForColumn(COL_PROGRESS, ProgressDelegate(self.tree))
        self.tree.setUniformRowHeights(True)
        self.tree.setSelectionMode(QTreeView.ExtendedSelection)
        self.tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._show_menu)
        self.tree.doubleClicked.connect(self._on_double_clicked)
        header = self.tree.header()
        header.resizeSection(COL_NAME, 320)
        header.resizeSection(COL_SIZE, 110)
        header.resizeSection(COL_PROGRESS, 150)
        header.setStretchLastSection(True)

        self.details = QLabel("（选择任务查看详情）")
        self.details.setObjectName("metaLabel")   # 样式：ui/theme.py #metaLabel
        self.details.setAlignment(Qt.AlignLeft | Qt.AlignTop)
        self.details.setWordWrap(True)
        self.details.setMinimumHeight(90)

        split = QSplitter(Qt.Vertical, self)
        split.addWidget(self.tree)
        split.addWidget(self.details)
        split.setStretchFactor(0, 3)
        split.setStretchFactor(1, 1)
        split.setSizes([360, 120])

        self._placeholder = QLabel("暂无下载任务 —— 解析后右键文件可「添加下载」")
        self._placeholder.setObjectName("emptyHint")   # 样式：#emptyHint
        self._placeholder.setAlignment(Qt.AlignCenter)

        self._stack = QStackedWidget(self)
        self._stack.addWidget(self._placeholder)   # 0：空态引导
        self._stack.addWidget(split)               # 1：任务列表 + 详情

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._stack)
        self.tree.selectionModel().selectionChanged.connect(self._update_details)

    # ---------- 外部接口（联调层调用） ----------

    def set_tasks(self, tasks: list[dict]):
        """全量刷新任务列表（进度/速度由外部轮询注入，重建行）。

        主窗口状态定时器每 700ms 调一次本方法。QStandardItemModel 重建会
        连带清掉选中/滚动位置——不恢复的话选中项每 0.7s 丢一次，详情区与
        右键菜单形同虚设（REVIEW-2026-09 P0-2）。因此：
        ① 右键菜单等弹窗打开期间跳过刷新（避免菜单引用的 index 失效）；
        ② 重建前记录选中任务的 info_hash 与滚动位置，重建后按 hash 恢复。
        """
        if QApplication.activePopupWidget() is not None:
            return
        self._tasks = list(tasks or [])
        sel_hash = (self.selected_task() or {}).get("info_hash")
        bar = self.tree.verticalScrollBar()
        scroll_pos = bar.value()

        self._model.removeRows(0, self._model.rowCount())
        for task in self._tasks:
            self._append_row(task)
        page = 1 if self._tasks else 0
        if self._stack.currentIndex() != page:
            self._stack.setCurrentIndex(page)

        if sel_hash:
            root = self._model.invisibleRootItem()
            for r in range(root.rowCount()):
                it = root.child(r, 0)
                t = it.data(Qt.UserRole) if it is not None else None
                if t and t.get("info_hash") == sel_hash:
                    idx = self._model.index(r, COL_NAME)
                    self.tree.selectionModel().select(
                        idx, QItemSelectionModel.SelectionFlag.Select
                        | QItemSelectionModel.SelectionFlag.Rows)
                    break
        # 无条件恢复：0 也是合法目标位置（顶部）——用 if scroll_pos 会在
        # 「用户本来就停在顶部」时跳过，重建后残留的非 0 位置就不被归位了
        bar.setValue(scroll_pos)
        self._update_details()

    def tasks(self) -> list[dict]:
        return list(self._tasks)

    def selected_task(self) -> dict | None:
        indexes = self.tree.selectionModel().selectedRows(COL_NAME)
        return indexes[0].data(Qt.UserRole) if indexes else None

    # ---------- 内部 ----------

    def _append_row(self, task: dict):
        emoji, state_cn, color = _state_meta(task.get("state"))
        title = task.get("name") or task.get(
            "info_hash", "")[:16] or "未命名"
        name_item = QStandardItem(f"{emoji} {title}")
        name_item.setEditable(False)
        name_item.setData(dict(task), Qt.UserRole)
        name_item.setForeground(QColor(color))
        tip = f"状态：{state_cn}（{emoji}）"
        if task.get("error"):
            tip += f"\n错误：{task['error']}"
        name_item.setToolTip(tip)

        size_item = QStandardItem(
            human_size(task["total_size"]) if task.get("total_size") else "—")
        size_item.setEditable(False)
        size_item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)

        pv = _progress_value(task)
        progress_item = QStandardItem(f"{pv:.0f}%")
        progress_item.setEditable(False)
        progress_item.setData(pv, Qt.UserRole)
        progress_item.setTextAlignment(Qt.AlignCenter)

        try:
            rate = float(task.get("down_rate") or 0.0)
        except (TypeError, ValueError):
            rate = 0.0
        speed_item = QStandardItem(
            f"{human_size(rate)}/s · {format_eta(task.get('eta'))}")
        speed_item.setEditable(False)
        speed_item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)

        self._model.appendRow([name_item, size_item, progress_item, speed_item])

    def _task_at(self, index):
        item = self._model.itemFromIndex(index.siblingAtColumn(COL_NAME))
        return item.data(Qt.UserRole) if item is not None else None

    def _show_menu(self, pos):
        index = self.tree.indexAt(pos)
        if not index.isValid():
            return
        task = self._task_at(index)
        if task is None:
            return
        state = str(task.get("state", "")).upper()

        menu = QMenu(self)
        act_pause = menu.addAction("⏸ 暂停")
        act_resume = menu.addAction("▶ 恢复")
        menu.addSeparator()
        act_prio_up = menu.addAction("优先级 ↑")
        act_prio_down = menu.addAction("优先级 ↓")
        menu.addSeparator()
        act_open_dir = menu.addAction("打开所在目录")
        act_open_preview = menu.addAction("打开预览")
        menu.addSeparator()
        act_remove = menu.addAction("🗑 删除任务")

        act_pause.setEnabled(state in ("QUEUED", "META_FETCH", "VALIDATE",
                                       "DOWNLOADING", "SEEDING"))
        act_resume.setEnabled(state in ("QUEUED", "PAUSED", "STOPPED", "FAILED"))
        prio = task.get("priority", 1)
        try:
            prio = int(prio)
        except (TypeError, ValueError):
            prio = 1
        act_prio_up.setEnabled(state in ("QUEUED", "META_FETCH", "VALIDATE",
                                         "DOWNLOADING", "PAUSED", "STOPPED")
                               and prio < 3)
        act_prio_down.setEnabled(state in ("QUEUED", "META_FETCH", "VALIDATE",
                                           "DOWNLOADING", "PAUSED", "STOPPED")
                                 and prio > 0)
        act_open_preview.setEnabled(state in ("COMPLETED", "DOWNLOADING",
                                              "PAUSED", "STOPPED", "SEEDING"))

        chosen = menu.exec(self.tree.viewport().mapToGlobal(pos))
        if chosen is act_pause:
            self.pause_requested.emit(task)
        elif chosen is act_resume:
            self.resume_requested.emit(task)
        elif chosen is act_remove:
            self.remove_requested.emit(task)
        elif chosen is act_prio_up:
            self.priority_up_requested.emit(task)
        elif chosen is act_prio_down:
            self.priority_down_requested.emit(task)
        elif chosen is act_open_dir:
            self.opendir_requested.emit(task)
        elif chosen is act_open_preview:
            self.openpreview_requested.emit(task)

    def _on_double_clicked(self, index):
        task = self._task_at(index)
        if task is None:
            return
        if str(task.get("state", "")).upper() == "FAILED":
            QMessageBox.warning(
                self, "下载失败",
                f"{task.get('name') or task.get('info_hash', '')}\n\n"
                f"{task.get('error') or '未知错误'}")

    def _update_details(self):
        task = self.selected_task()
        if task is None:
            self.details.setText("（选择任务查看详情）")
            return
        emoji, state_cn, _ = _state_meta(task.get("state"))
        lines = [
            f"状态：{emoji} {state_cn}",
            f"名称：{task.get('name') or '—'}",
            f"info_hash：{task.get('info_hash') or '—'}",
            f"优先级：{task.get('priority', 1)}",
            f"保存目录：{task.get('save_path') or '—'}",
            f"已选文件：{len(task.get('selected_files') or [])} 个",
        ]
        if task.get("error"):
            lines.append(f"错误：{task['error']}")
        self.details.setText("\n".join(lines))