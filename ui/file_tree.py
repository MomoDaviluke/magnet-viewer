"""文件树视图：目录层级 + 大小/占比 + 双击预览 + 右键添加下载。"""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QStandardItem, QStandardItemModel
from PySide6.QtWidgets import QLabel, QMenu, QTreeView

from core.models import ParseResult, TorrentFile, human_size


class FileTreeWidget(QTreeView):
    file_activated = Signal(object)          # TorrentFile（双击预览）
    add_download_requested = Signal(object)  # TorrentFile（右键添加下载）

    COL_NAME, COL_SIZE, COL_RATIO = 0, 1, 2

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("fileTree")     # 样式：ui/theme.py QTreeView#fileTree
        self._model = QStandardItemModel(0, 3, self)
        self._model.setHorizontalHeaderLabels(["名称", "大小", "占比"])
        self.setModel(self._model)
        self.setUniformRowHeights(True)
        self.setExpandsOnDoubleClick(False)
        # 空态覆盖层（QSS #emptyHint）：树内没有行时居中提示如何开始
        self._empty_hint = QLabel("粘贴磁力链后点「解析」", self.viewport())
        self._empty_hint.setObjectName("emptyHint")
        self._empty_hint.setAlignment(Qt.AlignCenter)
        self.doubleClicked.connect(self._on_double_clicked)
        self.setContextMenuPolicy(Qt.CustomContextMenu)
        self.customContextMenuRequested.connect(self._show_context_menu)
        self.setIndentation(16)
        header = self.header()
        header.resizeSection(0, 480)
        header.resizeSection(1, 110)
        header.setStretchLastSection(True)
        self._sync_empty_hint()

    @staticmethod
    def _make_row(text: str, size_text: str, ratio_text: str) -> list:
        """构造完整三列行：名称 / 大小 / 占比。"""
        name_item = QStandardItem(text)
        name_item.setEditable(False)
        size_item = QStandardItem(size_text)
        size_item.setEditable(False)
        size_item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
        ratio_item = QStandardItem(ratio_text)
        ratio_item.setEditable(False)
        ratio_item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
        return [name_item, size_item, ratio_item]

    def populate(self, result: ParseResult):
        self._model.removeRows(0, self._model.rowCount())
        dir_items: dict[str, QStandardItem] = {}
        root = self._model.invisibleRootItem()
        total = result.total_size or 1

        # 单文件种子的 path 不含分隔符，split("/") 后自然平铺，无需额外判定
        for f in result.view_files:
            parts = f.path.split("/")
            parent = root
            acc = ""
            for seg in parts[:-1]:
                acc = f"{acc}/{seg}" if acc else seg
                if acc not in dir_items:
                    dsize = sum(x.size for x in result.view_files
                                if x.path.startswith(acc + "/"))
                    row = self._make_row(
                        f"[目录] {seg}", human_size(dsize),
                        f"{dsize / total * 100:.1f}%")
                    parent.appendRow(row)          # 关键：目录行必须挂到父节点
                    dir_items[acc] = row[0]
                parent = dir_items[acc]
            icon = "🎬" if f.is_video else ("🖼️" if f.is_image else "📄")
            row = self._make_row(f"{icon} {f.name}", human_size(f.size),
                                 f"{f.size / total * 100:.1f}%")
            row[0].setData(f, Qt.UserRole)
            parent.appendRow(row)

        # 注意：多文件种子常见 root/季集/文件 三级结构，仅 expandToDepth(0)
        # 会让二级目录保持折叠 —— 用户会看到"文件树里没有文件"。
        if len(result.view_files) <= 3000:
            self.expandAll()
        else:  # 超大种子避免一次性展开卡顿
            self.expandToDepth(2)
        self._sync_empty_hint()

    # ---------- 空态 ----------

    def _sync_empty_hint(self):
        """文件树无行时显示居中提示（QSS #emptyHint）。

        覆盖层挂在 viewport 上并铺满视口；解析结果填入后随行存在自动隐藏。
        """
        if self._model.rowCount() == 0:
            self._empty_hint.setGeometry(self.viewport().rect())
            self._empty_hint.show()
            self._empty_hint.raise_()
        else:
            self._empty_hint.hide()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._sync_empty_hint()

    def _on_double_clicked(self, index):
        item = self._model.itemFromIndex(index.siblingAtColumn(0))
        if item is None:
            return
        f = item.data(Qt.UserRole)
        if isinstance(f, TorrentFile) and f.is_previewable:
            self.file_activated.emit(f)

    # ---------- 右键菜单 ----------

    def _file_at(self, pos) -> TorrentFile | None:
        index = self.indexAt(pos)
        if not index.isValid():
            return None
        item = self._model.itemFromIndex(index.siblingAtColumn(0))
        f = item.data(Qt.UserRole) if item is not None else None
        return f if isinstance(f, TorrentFile) else None

    def _show_context_menu(self, pos):
        f = self._file_at(pos)
        if f is None or not f.is_previewable:
            return
        menu = QMenu(self)
        act = menu.addAction("⬇ 添加下载（整个种子）")
        if menu.exec(self.viewport().mapToGlobal(pos)) is act:
            self.add_download_requested.emit(f)

    # ---------- 定位 ----------

    def select_file(self, f: TorrentFile) -> bool:
        """定位并选中文件树中指定文件（供「打开预览」从下载页跳转）。"""
        for row in range(self._model.rowCount()):
            item = self._model.item(row, 0)
            if item is None:
                continue
            if item.data(Qt.UserRole) is f:
                idx = self._model.indexFromItem(item)
                self.setCurrentIndex(idx)
                self.scrollTo(idx)
                return True
        # 文件可能挂在深层目录：递归查找
        return self._select_recursive(self._model.invisibleRootItem(), f)

    def _select_recursive(self, parent, f: TorrentFile) -> bool:
        for row in range(parent.rowCount()):
            item = parent.child(row, 0)
            if item is None:
                continue
            if item.data(Qt.UserRole) is f:
                idx = self._model.indexFromItem(item)
                self.setCurrentIndex(idx)
                self.scrollTo(idx)
                return True
            if item.hasChildren() and self._select_recursive(item, f):
                return True
        return False
