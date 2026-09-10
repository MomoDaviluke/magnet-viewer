"""内嵌视频播放器：QMediaPlayer 拉取本地流服务，边下边播。"""
from __future__ import annotations

import time

from PySide6.QtCore import QRectF, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QColor, QPainter
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtMultimediaWidgets import QVideoWidget
from PySide6.QtWidgets import (QHBoxLayout, QLabel, QProgressBar, QPushButton,
                               QSlider, QStyle, QStyleOptionSlider,
                               QVBoxLayout, QWidget)

from core.models import human_size
from ui.theme import SLIDER_SEGMENT, SP_MD


def fmt_time(ms: int) -> str:
    s = max(0, ms // 1000)
    return f"{s // 60:02d}:{s % 60:02d}"


# 开播等待阶段（plan/07 阶段 3）：门控需要「头部连续数据」+「尾部索引块
# （moov）」两者，分开提示让用户知道卡在哪一步；WAIT_BOTH 保留旧文案。
WAIT_BOTH = "both"
WAIT_DATA = "data"
WAIT_INDEX = "index"


def waiting_text(name: str, size: int, stage: str = WAIT_BOTH) -> str:
    """开播等待文案（纯函数，plan/07 阶段 3）。

    此前只有一句「等待数据与索引块就绪」，慢链路下用户无法分辨是缺头数据
    还是缺索引块。``stage``：``WAIT_DATA`` → 「等待数据就绪」；
    ``WAIT_INDEX`` → 「等待索引块就绪」；其余（含缺省 ``WAIT_BOTH``）→
    「等待数据与索引块就绪」（与旧文案逐字一致）。

    文案只做展示——开播门控判据（``ui/main_window._refresh_status``）与
    本函数彼此独立，绝不由文案反推门控。
    """
    what = {WAIT_DATA: "数据", WAIT_INDEX: "索引块"}.get(stage, "数据与索引块")
    return f"缓冲中，等待{what}就绪：{name}（{human_size(size)}）"


class BufferedSlider(QSlider):
    """带「已缓存分段」着色的进度条（半透明灰段 = 落盘可读区间）。

    分段由会话层按 piece 落盘状态合并而来（fetcher.buffered_segments_of_preview），
    以文件内字节区间传入、换算为 0~1 比例绘制在 groove 上；handle 最后重画一次，
    避免被分段盖住。半透明中灰在明暗主题下均可辨识。
    """

    def __init__(self, parent=None):
        super().__init__(Qt.Horizontal, parent)
        self._segments: list[tuple[float, float]] = []

    def set_segments(self, segs: list[tuple[int, int]], total: int):
        if total <= 0:
            self._segments = []
        else:
            self._segments = [(s / total, e / total)
                              for s, e in segs if e > s]
        self.update()

    def clear_segments(self):
        self._segments = []
        self.update()

    def paintEvent(self, ev):
        super().paintEvent(ev)
        if not self._segments:
            return
        opt = QStyleOptionSlider()
        self.initStyleOption(opt)
        groove = self.style().subControlRect(
            QStyle.ComplexControl.CC_Slider, opt,
            QStyle.SubControl.SC_SliderGroove, self)
        if groove.width() <= 0:
            return
        painter = QPainter(self)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(*SLIDER_SEGMENT))
        bar_h = max(4, groove.height() // 3)
        bar_y = groove.center().y() - bar_h / 2
        w = groove.width()
        for a, b in self._segments:
            x0 = groove.x() + a * w
            x1 = groove.x() + b * w
            painter.drawRect(QRectF(x0, bar_y, max(2.0, x1 - x0), bar_h))
        # 分段画在 groove 层，会盖住 handle —— 设置 subControls 只重画 handle
        opt2 = QStyleOptionSlider()
        self.initStyleOption(opt2)
        opt2.subControls = QStyle.SubControl.SC_SliderHandle
        self.style().drawComplexControl(
            QStyle.ComplexControl.CC_Slider, opt2, painter, self)
        painter.end()


class VideoPreviewWidget(QWidget):
    play_toggled = Signal(bool)
    seek_requested = Signal(int)  # 拖动进度条 → 请求从该字节位置继续下载（int 字节偏移）
    scrub_preview = Signal(int)   # 拖动过程中节流预取目标区间（不等松手，压卡顿）
    stream_failed = Signal()      # QMediaPlayer 打开媒体失败（数据仍在下载，可稍后重试）

    SCRUB_INTERVAL_MS = 300       # 拖动中两次预取的最小间隔

    def __init__(self, parent=None):
        super().__init__(parent)
        self.file_name = ""
        self._size = 0
        self._buffering = False
        self._jumping = False     # 跳转后、position 追上前：缓冲栏显示「跳转中」
        # 用户跳转状态：拖动与点击轨道共用一套防抖/回写抑制机制。
        # _drag_handled=True 表示本次防抖窗口由拖动发起（已即时跳转，
        # 防抖回调不再重复跳转）；_seek_target 为跳转目标位置，在 position
        # 真正追上之前不回写滑块，否则「拖过去又被弹回」。
        self._drag_handled = False
        self._seek_target: int | None = None
        self._seek_at = 0.0
        self._resume_ms: int | None = None   # 待恢复位置（自动重试 / 重新开播）
        self._errored = False                # 播放处于错误态：禁止拖动（拖了也没反应）
        self._wait_stage: str | None = None  # 开播等待阶段（阶段 3 文案去重）

        self.player = QMediaPlayer(self)
        self.audio = QAudioOutput(self)
        self.player.setAudioOutput(self.audio)
        self.video = QVideoWidget(self)
        self.player.setVideoOutput(self.video)

        self.title = QLabel("（未在播放）")
        # 标题样式统一走 QSS：#playerTitle（常态）/ #playerTitle[error="true"]
        # （错误态）——本文件不写色值、不写内联样式（theme_check R1/R2）
        self.title.setObjectName("playerTitle")
        self.title.setProperty("error", False)

        # 空态提示（#emptyHint）：视频页在「从未开播」时居中给一句引导，
        # 开播/等待即隐藏（hide 后 QVBoxLayout 不再占位，布局与改造前一致）
        self.empty_hint = QLabel("双击文件树中的视频文件即可边下边播")
        self.empty_hint.setObjectName("emptyHint")
        self.empty_hint.setAlignment(Qt.AlignCenter)

        self.buffer_bar = QProgressBar(self)
        self.buffer_bar.setObjectName("bufferBar")
        self.buffer_bar.setRange(0, 1000)
        self.buffer_bar.setFixedHeight(6)
        self.buffer_bar.setTextVisible(False)
        self.buffer_label = QLabel("缓冲 0.0% · 0 B/s")
        self.buffer_label.setObjectName("metaLabel")

        self.btn_play = QPushButton("暂停")
        self.btn_play.setObjectName("playButton")
        self.btn_play.setFixedSize(36, 36)
        self.btn_play.clicked.connect(self._toggle_play)
        self.slider = BufferedSlider(self)
        self._last_scrub = 0.0
        self.slider.setRange(0, 0)
        self.time_label = QLabel("00:00 / 00:00")
        self.time_label.setObjectName("metaLabel")
        self.volume = QSlider(Qt.Horizontal)
        self.volume.setRange(0, 100)
        self.volume.setValue(80)
        self.volume.setFixedWidth(100)
        self.audio.setVolume(0.8)
        self.volume.valueChanged.connect(lambda v: self.audio.setVolume(v / 100))

        ctrl = QHBoxLayout()
        ctrl.setSpacing(SP_MD)
        ctrl.addWidget(self.btn_play)
        ctrl.addWidget(self.slider, 1)
        ctrl.addWidget(self.time_label)
        ctrl.addWidget(QLabel("音量"))
        ctrl.addWidget(self.volume)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.title)
        layout.addWidget(self.empty_hint)
        layout.addWidget(self.video, 1)
        layout.addWidget(self.buffer_bar)
        layout.addWidget(self.buffer_label)
        layout.addLayout(ctrl)

        self.player.positionChanged.connect(self._on_position)
        self.player.durationChanged.connect(self._on_duration)
        self.player.mediaStatusChanged.connect(self._on_media_status)
        self.player.errorOccurred.connect(self._on_player_error)
        self.slider.sliderReleased.connect(self._on_seek)
        self.slider.sliderMoved.connect(self._on_slider_moved)
        # 点击轨道跳转不会触发 sliderPressed/Released，用「值显著偏离播放位置 +
        # 防抖」识别；等待期间暂停程序性滑块回写，避免跳转被播放位置覆盖。
        self._click_timer = QTimer(self)
        self._click_timer.setSingleShot(True)
        self._click_timer.setInterval(400)
        self._click_timer.timeout.connect(self._on_click_seek)
        self.slider.valueChanged.connect(self._on_value_changed)

    # ---------- 外部接口 ----------

    def _reset_seek_state(self):
        """换片/停止/等待前清空跳转状态。

        防抖定时器残留会让 400ms 后的回调把**旧跳转**作用到新文件上
        （实测：stop() 后仍发射了一次 seek_requested）。
        """
        self._click_timer.stop()
        self._drag_handled = False
        self._seek_target = None
        self._jumping = False

    def _set_play_btn(self, enabled: bool, playing: bool | None = None):
        """播放按钮三态：无源/等待期禁用，播放中/暂停分别显示「暂停/播放」。"""
        self.btn_play.setEnabled(enabled)
        if playing is None:
            self.btn_play.setText("播放")
        else:
            self.btn_play.setText("暂停" if playing else "播放")

    def _set_title_error(self, on: bool) -> None:
        """切换标题错误态（QSS 属性选择器 #playerTitle[error="true"]）。

        Qt 动态属性变化**不会**自动重算样式，必须 unpolish + polish 才会
        命中属性选择器（plan B2 明确要求）。
        """
        self.title.setProperty("error", bool(on))
        style = self.title.style()
        style.unpolish(self.title)
        style.polish(self.title)
        self.title.update()

    def show_error(self, message: str):
        """把错误显示到醒目的标题区（避免只写底部小字被用户忽略）。"""
        self._set_title_error(True)
        self.title.setText(f"⚠ {message}")
        self.buffer_label.setText(f"播放器错误：{message}")

    def _restore_title(self, text: str):
        self._set_title_error(False)
        self.title.setText(text)

    def set_waiting(self, name: str, size: int):
        """等待头部数据与索引块落盘（不开始播放，避免读到稀疏零数据/探测不到 moov）。"""
        self.file_name = name
        self._size = size
        self.player.stop()
        self.player.setSource(QUrl())
        self.slider.setRange(0, 0)
        self._reset_seek_state()
        self.slider.clear_segments()
        self._resume_ms = None
        self._wait_stage = WAIT_BOTH          # 阶段未知：合并文案（旧行为）
        self.empty_hint.setVisible(False)      # 有文件了：收起空态引导
        self._restore_title(waiting_text(name, size, WAIT_BOTH))
        self.buffer_label.setText("准备中…")
        self._set_play_btn(False)   # 等待期不可播放，避免点击无效却改文案

    def set_waiting_stage(self, stage: str):
        """开播等待期细化文案（plan/07 阶段 3）：等数据 / 等索引块。

        由主窗口门控轮询在缺料时下发（``_refresh_status``）；同一阶段重复
        调用**幂等**（不重复 setText，避免 700ms 轮询刷屏重绘）。仅在
        ``set_waiting`` 之后、``set_stream`` 之前的等待期有意义。
        """
        if self._wait_stage == stage:
            return
        self._wait_stage = stage
        self._restore_title(waiting_text(self.file_name, self._size, stage))

    def set_stream(self, url: str, name: str, size: int,
                   resume_ms: int | None = None):
        """开播（resume_ms 非空时：媒体就绪后跳回该位置）。

        自动重试必须带上出错位置，否则 `setSource` 会从头开始播——
        拖动进度条后数据未就绪而报错时，用户会看到「突然从头重播」。
        """
        self.file_name = name
        self._size = size
        self._wait_stage = None                # 开播：清等待阶段（文案不残留）
        self.empty_hint.setVisible(False)      # 开播：收起空态引导
        self.title.setText(f"正在流式播放：{name}（{human_size(size)}）")
        self.slider.setRange(0, 0)
        self._reset_seek_state()
        self.slider.clear_segments()
        self._resume_ms = resume_ms
        self._errored = False          # 重新开播：解除错误态的拖动禁用
        self.slider.setEnabled(True)
        self.player.setSource(QUrl(url))
        self.player.play()
        self._set_play_btn(True, True)

    def take_resume_ms(self) -> int | None:
        """取出「出错前的位置」供重试恢复；取走即清空，避免下次误用。"""
        ms, self._resume_ms = self._resume_ms, None
        return ms if ms and ms > 0 else None

    def update_buffer(self, progress: float, rate: int,
                      bg_note: str | None = None):
        """缓冲栏刷新。``bg_note``（阶段 D D3）：convert 档播放中的
        「后台缓存完整文件：xx%（播放位置优先）」注记，None/空 = 基线文案。"""
        self.buffer_bar.setValue(int(progress * 1000))
        if self._jumping:
            # 跳转反馈：数据未到位前明确告知「在缓冲目标位置」，
            # 避免用户把等待误读为无响应
            self.buffer_label.setText(
                f"跳转中，正在缓冲目标位置 · 缓冲 {progress * 100:.1f}%"
                + (f"  ／  {bg_note}" if bg_note else ""))
            return
        buffering = self._buffering or (progress < 0.999 and rate < 50 * 1024)
        self.buffer_label.setText(
            f"缓冲 {progress * 100:.1f}% · {human_size(rate)}/s"
            + ("  ／ 缓冲中…" if buffering else "")
            + (f"  ／  {bg_note}" if bg_note else ""))
        self._buffering = False  # 事件态只提示一次，文本由本方法统一渲染

    def update_segments(self, segs: list[tuple[int, int]]):
        """透传已缓存分段给进度条着色（无预览时调用方不调用）。"""
        self.slider.set_segments(segs, self._size)

    def stop(self):
        self.player.stop()
        self.player.setSource(QUrl())
        self.slider.setRange(0, 0)      # 与等待态一致：不留旧时长刻度
        self._reset_seek_state()
        self.slider.clear_segments()
        self._resume_ms = None
        self._errored = False
        self._wait_stage = None         # 清等待阶段（与标题一并复位）
        self.slider.setEnabled(True)
        self.empty_hint.setVisible(True)   # 回到「未选文件」：重新显示空态引导
        self._restore_title("（未在播放）")
        self.buffer_bar.setValue(0)
        self.buffer_label.setText("缓冲 0.0%")
        self._set_play_btn(False)

    # ---------- 内部 ----------

    def _on_player_error(self, error, message: str):
        if error == QMediaPlayer.NoError:
            return
        # 记录出错位置：自动重试要回到这里，而不是从头重播（拖动进度条后
        # 数据未就绪而报错时，从头重播的体感就是「进度条失灵」）。
        # 跳转中出错优先记跳转目标，否则记当前播放位置。
        self._resume_ms = (self._seek_target if self._seek_target is not None
                           else self.player.position())
        # 错误态下 setPosition 无效，滑块必须禁用：否则用户拖动毫无反应，
        # 观感就是「进度条失灵」，且他会反复拖、反复无响应。
        self._errored = True
        self.slider.setEnabled(False)
        self.show_error(message)
        self._set_play_btn(False)
        self.stream_failed.emit()

    def _toggle_play(self):
        if self.player.playbackState() == QMediaPlayer.PlayingState:
            self.player.pause()
            self._set_play_btn(True, False)
        else:
            self.player.play()
            self._set_play_btn(True, True)

    def _on_seek(self):
        """拖动结束：立即跳转，并开启统一的「抑制程序性回写」窗口。

        开启窗口是必要的：拖动释放后 QMediaPlayer 仍会先广播跳转前的旧
        位置，若立即回写滑块，用户会看到进度条被弹回原处。
        """
        if self.slider.maximum() <= 0:
            return
        v = self.slider.value()
        self._mark_seek(v)
        self._drag_handled = True   # 防抖回调不再重复跳转（避免双重 seek）
        self._click_timer.start()
        self.player.setPosition(v)
        self._emit_seek_byte(v)

    def _mark_seek(self, v: int):
        """记录跳转目标：position 追上之前不回写滑块。"""
        self._seek_target = v
        self._seek_at = time.monotonic()
        self._jumping = True

    def _on_slider_moved(self, v: int):
        """拖动中节流预取：松手**前**就开始点播目标区间。

        这是拖动卡顿的最大优化点——seek 到未下载区的等待主体是
        「下载 1~2 个 piece」的物理时间，预取把这段下载提前到拖动过程
        中完成，体感卡顿趋近于零。节流避免拖动路径上请求风暴；
        request_range 自带 60 块上限，误预取的带宽代价可控。
        """
        if self._size <= 0 or self.slider.maximum() <= 0:
            return
        now = time.monotonic()
        if now - self._last_scrub < self.SCRUB_INTERVAL_MS / 1000:
            return
        self._last_scrub = now
        dur = self.player.duration()
        if dur > 0:
            self.scrub_preview.emit(int(v / dur * self._size))
        else:
            mx = self.slider.maximum()
            if mx > 0:
                self.scrub_preview.emit(int(v / mx * self._size))

    def _on_value_changed(self, _v):
        """区分程序性回写与用户点击轨道：显著偏离播放位置才视为点击。"""
        if (self.slider.isSliderDown() or self.slider.maximum() <= 0
                or self._size <= 0 or self._click_timer.isActive()):
            # 防抖窗口内一律忽略：拖动释放后也会补发一次 valueChanged，
            # 若在此重置 _drag_handled，会导致 400ms 后二次 setPosition。
            return
        if abs(self.slider.value() - self.player.position()) > 2000:
            self._drag_handled = False
            self._click_timer.start()

    def _on_click_seek(self):
        """防抖窗口结束：只处理「点击轨道」，拖动已在 _on_seek 处理过。"""
        if self._drag_handled:
            self._drag_handled = False
            return
        if self.slider.isSliderDown():
            return
        v = self.slider.value()
        if abs(v - self.player.position()) <= 2000:
            return  # 已被程序性回写纠正，不是用户点击
        self._mark_seek(v)
        self.player.setPosition(v)
        self._emit_seek_byte(v)

    def _emit_seek_byte(self, position_ms: int):
        dur = self.player.duration()
        if dur <= 0:
            # 时长尚未广播时用滑块范围兜底：否则跳转发生了却没人通知调度器
            # （正常路径 _on_duration 会同步两者，这里是防御性兜底）
            dur = self.slider.maximum()
        if dur > 0 and self._size > 0:
            self.seek_requested.emit(int(position_ms / dur * self._size))

    def _update_time(self, pos: int):
        self.time_label.setText(
            f"{fmt_time(pos)} / {fmt_time(self.player.duration())}")

    def _on_position(self, pos: int):
        if self._click_timer.isActive():
            # 点击轨道的防抖窗口内不回写，否则跳转会被播放位置覆盖
            if (self._seek_target is not None
                    and abs(pos - self._seek_target) <= 2000):
                self._seek_target = None   # 窗口内就已追上：立即解除抑制
                self._jumping = False
            self._update_time(pos)
            return
        if self._seek_target is not None:
            # 跳转尚未生效（seek 是异步的、未下载区间还要等数据）：
            # 此时回写旧位置会让进度条「弹回去」，等 position 追上目标
            # 再恢复回写；超过 15s 仍未追上则放弃等待（避免永久不刷新）。
            if (abs(pos - self._seek_target) <= 2000
                    or time.monotonic() - self._seek_at > 15):
                self._seek_target = None
                self._jumping = False   # 追上/放弃：缓冲栏恢复常规文案
            else:
                self._update_time(pos)
                return
        if not self.slider.isSliderDown():
            self.slider.setValue(pos)
        self._update_time(pos)

    def _on_duration(self, dur: int):
        self.slider.setRange(0, max(0, dur))

    def _on_media_status(self, status):
        # 只记录状态，文本统一由 update_buffer 渲染，避免重复追加「缓冲中…」
        self._buffering = status in (QMediaPlayer.LoadingMedia,
                                     QMediaPlayer.BufferingMedia,
                                     QMediaPlayer.StalledMedia)
        # 自动重试的续播：必须等媒体真正就绪（Loaded/Buffered）再 setPosition，
        # LoadingMedia 阶段跳转会被后端忽略。
        if self._resume_ms and status in (QMediaPlayer.LoadedMedia,
                                          QMediaPlayer.BufferedMedia):
            ms, self._resume_ms = self._resume_ms, None
            self._mark_seek(ms)          # 恢复期间同样抑制回写，避免弹回
            self.player.setPosition(ms)
            self._emit_seek_byte(ms)     # 让调度器跟着跳到该位置补数据
