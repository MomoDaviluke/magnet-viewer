"""底部状态面板：会话状态 / 做种 / 速度 / 缓冲。"""
from __future__ import annotations

from ui.theme import TEXT_MUTED
from PySide6.QtWidgets import QHBoxLayout, QLabel, QWidget

from core.models import human_size


class StatusPanel(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.state = QLabel("空闲 —— 粘贴磁力链或打开 .torrent 文件开始解析")
        self.peers = QLabel("做种 -  连接 -")
        self.speed = QLabel("速度 0 B/s")
        self.total_speed = QLabel("")
        self.buffer = QLabel("")

        for lbl in (self.peers, self.speed, self.total_speed, self.buffer):
            lbl.setStyleSheet(f"color:{TEXT_MUTED};")
        self.total_speed.hide()  # 无任务时不占位

        row = QHBoxLayout(self)
        row.setContentsMargins(8, 4, 8, 4)
        row.addWidget(self.state, 1)
        row.addWidget(self.buffer)
        row.addWidget(self.total_speed)
        row.addWidget(self.speed)
        row.addWidget(self.peers)

    def update_status(self, status: dict | None, total_download_rate=None):
        """刷新状态栏；total_download_rate 缺省时尝试从 status 字典读取。

        兼容旧调用（仅 status）：不传总速度时若 status 里也没有
        total_download_rate 键，则隐藏「总下载速度」标签，不干扰预览单任务。
        """
        if status is None:
            return
        self.peers.setText(f"做种 {status['num_seeds']}  连接 {status['num_peers']}")
        self.speed.setText(f"速度 {human_size(status['download_rate'])}/s")
        if status.get("preview_file") is not None:
            self.buffer.setText(f"预览缓冲 {status['buffer'] * 100:.1f}%")
        else:
            self.buffer.setText("")
        if total_download_rate is None:
            total_download_rate = status.get("total_download_rate")
        if total_download_rate is not None:
            self.total_speed.setText(
                f"总下载速度 {human_size(total_download_rate)}/s")
            self.total_speed.show()
        else:
            self.total_speed.hide()

    def set_state(self, text: str):
        self.state.setText(text)
