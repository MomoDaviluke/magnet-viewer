"""添加下载确认对话框：名称 / 总大小 / 保存目录 / 优先级 / 完成后做种。

纯控件层：不触 SessionManager。调用方在 ``exec() == Accepted`` 后读取
``save_path() / priority() / seed_after_complete()`` 创建任务。
样式仿 settings_dialog（QFormLayout + Save/Cancel + 浏览… 选择目录行）。
"""
from __future__ import annotations

from ui.theme import SP_LG, SP_MD, SP_XL
import os

from PySide6.QtWidgets import (QCheckBox, QDialog, QDialogButtonBox,
                               QFileDialog, QFormLayout, QHBoxLayout, QLabel,
                               QLineEdit, QMessageBox, QPushButton, QSpinBox,
                               QVBoxLayout)

from core.config import AppConfig
from core.models import human_size


class AddDownloadDialog(QDialog):
    """添加下载任务的确认对话框。

    cfg 用于读取默认设置（download_dir / seed_after_complete）。
    name / total_size 为已解析的种子信息；default_save_dir 为调用方
    传入的默认保存位置（主窗口一般传 缓存目录/downloads）。
    """

    def __init__(self, cfg: AppConfig, name: str, total_size: int,
                 *, default_save_dir: str = "", parent=None):
        super().__init__(parent)
        self.cfg = cfg
        self.setWindowTitle("添加下载任务")
        self.setMinimumWidth(480)

        form = QFormLayout()
        form.setSpacing(SP_MD)
        form.setContentsMargins(SP_XL, SP_LG, SP_XL, SP_LG)

        self.name_label = QLabel(str(name) or "（未命名）")
        self.name_label.setWordWrap(True)
        form.addRow("名称", self.name_label)

        self.size_label = QLabel(human_size(total_size or 0))
        form.addRow("总大小", self.size_label)

        row = QHBoxLayout()
        self.save_edit = QLineEdit(
            str(cfg.get("download_dir") or default_save_dir or ""))
        self.save_edit.setPlaceholderText("留空 = 自动（info_hash）；限单层目录名")
        btn_browse = QPushButton("浏览…")
        btn_browse.clicked.connect(self._pick_dir)
        row.addWidget(self.save_edit, 1)
        row.addWidget(btn_browse)
        form.addRow("保存子目录", row)

        # 注意：控件名不能叫 self.priority —— 那会遮蔽下方 priority() 方法，
        # 调用方 dlg.priority() 变成调用 QSpinBox 实例 → TypeError
        # （「添加下载」100% 失败，REVIEW-2026-09 P0-1）
        self.priority_spin = QSpinBox()
        self.priority_spin.setRange(0, 3)
        self.priority_spin.setValue(1)
        self.priority_spin.setToolTip("0 = 不下载，3 = 最高；预览始终为最高优先级")
        form.addRow("优先级", self.priority_spin)

        self.seed_check = QCheckBox("完成后继续做种（保持上传）")
        self.seed_check.setChecked(bool(cfg.get("seed_after_complete")))
        form.addRow("", self.seed_check)

        note = QLabel("提示：保存子目录是下载根目录下的单层目录名"
                      "（可留空自动使用 info_hash）；优先级 0 = 不下载、"
                      "3 = 最高；预览始终为最高优先级。")
        note.setObjectName("fieldNote")   # 样式：ui/theme.py #fieldNote
        note.setWordWrap(True)

        buttons = QDialogButtonBox(QDialogButtonBox.Save
                                   | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._try_accept)
        buttons.rejected.connect(self.reject)
        btn_save = buttons.button(QDialogButtonBox.Save)
        if btn_save is not None:
            btn_save.setObjectName("primary")   # 主按钮样式：ui/theme.py #primary

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(note)
        layout.addWidget(buttons)

    # ---------- 结果读取（Accepted 后调用） ----------

    def save_path(self) -> str:
        """保存子目录名（去除首尾空白与斜杠；空串 = 自动用 info_hash）。"""
        return self.save_edit.text().strip().strip("/\\")

    def priority(self) -> int:
        return self.priority_spin.value()

    def seed_after_complete(self) -> bool:
        return self.seed_check.isChecked()

    # ---------- 内部 ----------

    def _pick_dir(self):
        d = QFileDialog.getExistingDirectory(self, "选择下载保存目录")
        if d:
            # 只取目录名作为子目录（与 add_task save_subdir 的单层语义对齐）
            self.save_edit.setText(os.path.basename(d.rstrip("/\\")))

    def _try_accept(self):
        p = self.save_path()
        if p:
            # 单层干净相对段校验：拒绝路径分隔符/父级穿越/盘符
            if ("/" in p or "\\" in p or p in (".", "..")
                    or ".." in p.split("/") or ".." in p.split("\\")
                    or len(p) == 2 and p[1] == ":"):
                QMessageBox.warning(
                    self, "保存子目录无效",
                    f"「{p}」不是有效的单层目录名：不允许路径分隔符、"
                    f"上级目录（..）或盘符。\n请只填一个目录名"
                    f"（例如 MyDownload），或留空自动使用 info_hash。")
                return
        self.accept()