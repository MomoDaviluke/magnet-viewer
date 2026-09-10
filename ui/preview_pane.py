"""预览容器：在视频播放器与图片画廊之间切换，并提供统一的停止入口。"""
from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (QHBoxLayout, QLabel, QPushButton,
                               QStackedWidget, QVBoxLayout, QWidget)

from core.models import TorrentFile, human_size
from ui.gallery import GalleryWidget
from ui.preview_player import VideoPreviewWidget

PAGE_VIDEO, PAGE_GALLERY = 0, 1


class PreviewPane(QWidget):
    stop_requested = Signal()       # 用户点击「停止预览」
    to_download_requested = Signal()  # 用户点击「转为下载」（当前种子转下载任务）

    def __init__(self, parent=None):
        super().__init__(parent)
        self.video = VideoPreviewWidget(self)
        self.gallery = GalleryWidget(self)
        self.stack = QStackedWidget(self)
        self.stack.addWidget(self.video)      # PAGE_VIDEO
        self.stack.addWidget(self.gallery)    # PAGE_GALLERY

        self.title = QLabel("（未选择文件）")
        # 样式统一由 ui/theme.py 的 #playerTitle 给（本文件不写内联样式）
        self.title.setObjectName("playerTitle")
        self.btn_to_download = QPushButton("转为下载")
        self.btn_to_download.setFixedWidth(88)
        self.btn_to_download.setToolTip(
            "把当前预览的种子转为下载任务：已下载分块直接复用，零额外下载")
        self.btn_to_download.setEnabled(False)
        self.btn_to_download.clicked.connect(self.to_download_requested.emit)
        self.btn_stop = QPushButton("停止预览")
        self.btn_stop.setFixedWidth(88)
        self.btn_stop.setEnabled(False)
        self.btn_stop.clicked.connect(self.stop_requested.emit)

        bar = QHBoxLayout()
        bar.addWidget(self.title, 1)
        bar.addWidget(self.btn_to_download)
        bar.addWidget(self.btn_stop)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addLayout(bar)
        layout.addWidget(self.stack, 1)

    # ---------- 切换 ----------

    def show_video(self, f: TorrentFile):
        self.title.setText(f"边下边播：{f.name}（{human_size(f.size)}）")
        self.btn_stop.setEnabled(True)
        self.btn_to_download.setEnabled(True)
        self.stack.setCurrentIndex(PAGE_VIDEO)

    def show_gallery(self, f: TorrentFile | None = None):
        self.title.setText("图片画廊")
        self.btn_stop.setEnabled(True)
        self.btn_to_download.setEnabled(True)
        if f is not None:
            self.gallery.show_file(f)
        self.stack.setCurrentIndex(PAGE_GALLERY)

    def reset(self):
        """停止预览后的界面复位。"""
        self.video.stop()
        self.title.setText("（未选择文件）")
        self.btn_stop.setEnabled(False)
        self.btn_to_download.setEnabled(False)
