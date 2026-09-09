"""主窗口：输入解析 + 文件树 ⇄ 预览（视频/图片）+ 下载管理三页签。"""
from __future__ import annotations

import os
import tempfile

from PySide6.QtCore import (QObject, QTimer, QStringListModel, Qt, QUrl,
                            Signal)
from PySide6.QtGui import QDesktopServices, QDragEnterEvent, QDropEvent
from PySide6.QtWidgets import (QCompleter, QFileDialog, QHBoxLayout,
                               QInputDialog, QLabel, QLineEdit, QMainWindow,
                               QMessageBox, QPushButton, QTabWidget,
                               QVBoxLayout, QWidget)

from core.cache_guard import (clear_cache_contents, ensure_cache_dir,
                              guard_ok_for_cleanup)
from core.cache_quota import dir_size_bytes, enforce_preview_limit
from core.config import AppConfig
from core.fetcher import SessionManager
from core import logutil
from core.logutil import log_warning
from core.models import (ParseResult, PieceMap, TorrentFile, disk_root,
                         file_disk_path, human_size)
from core.stream_server import StreamServer
from ui.add_download_dialog import AddDownloadDialog
from ui.downloads_pane import DownloadsPane
from ui.file_tree import FileTreeWidget
from ui.preview_pane import PreviewPane
from ui.settings_dialog import SettingsDialog
from ui.status_panel import StatusPanel
from ui.theme import TEXT_MUTED

TAB_FILES, TAB_PREVIEW, TAB_DOWNLOADS = 0, 1, 2


def _clear_preview_cache(cache_dir: str) -> int:
    """只清预览缓存内容，保留 downloads/ 与任务持久化文件。

    统一走 core.cache_guard.clear_cache_contents 的保留名单（决策 D8/D9）；
    返回删除的条目数。注意：设置对话框「立即清理」曾在此之后再无名单清空
    整个目录、误删用户下载数据（P0-1）——所有清理入口都必须经此函数。
    """
    return clear_cache_contents(cache_dir)


class _Bridge(QObject):
    """把 core 后台线程的回调转成 Qt 信号（跨线程安全）。"""
    metadata_ready = Signal(object)
    resolve_failed = Signal(str)


def stream_rel(result: ParseResult | None, cache_dir: str, f: TorrentFile) -> str:
    """文件在缓存根目录下的服务相对路径（含任务隔离子目录，D7）。

    数据按 save_subdir（.preview/<ih> 或 downloads/<ih>）落盘，流服务
    base_dir 是 cache_dir，url_for 必须带上该前缀才能命中真实文件；
    save_subdir 为绝对路径（download_dir 在缓存目录之外）时返回绝对
    落盘路径，由流服务 base_dirs（含 download_dir）越根放行。
    """
    sub = getattr(result, "save_subdir", "") if result else ""
    if not sub:
        return f.path
    root = disk_root(result.cache_dir or cache_dir, sub)
    if os.path.isabs(root):
        return os.path.join(root, *f.path.split("/"))
    prefix = [s for s in sub.replace("\\", "/").split("/") if s]
    return "/".join(prefix + [f.path])


def task_id(task: dict) -> str:
    """任务唯一键：id 优先，回落 info_hash（下载页契约字段）。"""
    tid = task.get("id") or task.get("info_hash") or ""
    return str(tid)


class StreamCallbacks:
    """流服务回调集（HTTP 线程入口，REVIEW-2026-09 P2-3 独立成类）。

    与主窗口共享 ``path_to_file`` / ``map_miss_warned`` 两个可变容器
    （主线程写、HTTP 线程读——GIL 下单操作原子，与 session.handle_alert
    的 best-effort 同级）；``session`` 为只读门面。
    """

    def __init__(self, session, path_to_file: dict, map_miss_warned: set,
                 preview_file_getter=None):
        self.session = session
        self.path_to_file = path_to_file
        self.map_miss_warned = map_miss_warned
        self._preview_file_getter = preview_file_getter or (lambda: None)

    def pieces_map(self, disk_path: str):
        """流服务回调：返回该文件的分块映射（按已下载分块判定可读区间）。

        优先级：①下载任务文件（按磁盘路径反查任务句柄，分块级可用）；
        ②已解析预览/画廊文件（path_to_file 映射）。两者都查不到 → None
        =「无法判定可用性」→ 流服务回 503（2026-09-08 起：旧逻辑把查不到
        当「已完成的受管文件按静态整文件服务」，但反查失败 ≠ 已下载完，
        下载中文件反查失败会被喂稀疏零数据，实证见 REVIEW-2026-09.md
        P0-4；恪守 P1-8 语义：不可判定绝不降级为整文件可用）。
        """
        pm = self.session.piece_map_for_path(disk_path)
        if pm is not None:
            return pm
        key = os.path.normpath(disk_path)
        f = self.path_to_file.get(key)
        if f is None:
            # 未命中 → 不可判定，流服务回 503 让客户端稍后重试。若该文件
            # 仍在下载中而被当整文件服务，会把未下载的稀疏零数据喂给播放器
            # （历史 P3-2 缺陷的同一机制）。此告警必须留痕。
            if key not in self.map_miss_warned:
                self.map_miss_warned.add(key)
                log_warning("main_window.pieces_map.miss",
                            f"分块映射未命中：{disk_path} —— 将按不可用处理"
                            f"（503），不再降级为整文件服务")
            return None
        pl = self.session.piece_length()
        if not pl:
            return None
        return PieceMap(piece_length=pl, offset=f.offset,
                        start_piece=f.start_piece, end_piece=f.end_piece,
                        size=f.size, have=self.session.have_piece)

    def demand_range(self, disk_path: str, start: int, end_excl: int):
        """流服务回调：播放器要读的字节尚未下载 → 立刻改下载这段。

        下载任务文件先按路径触发任务级补拉；其次预览文件走调度器。
        """
        if self.session.demand_for_path(disk_path, start, end_excl):
            return
        f = self.path_to_file.get(os.path.normpath(disk_path))
        if f is None or f is not self._preview_file_getter():
            return
        self.session.scheduler.request_range(start, end_excl)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("磁力链实时解析查看器 Magnet Viewer")
        self.resize(1040, 700)
        self._setup_core()
        # ---- UI ----
        self._build_ui()
        self._init_history()
        self.setAcceptDrops(True)  # 拖拽 .torrent 文件 / magnet 文本进窗口直接解析
        self._wire_signals()
        self._setup_timers()

    def _setup_core(self):
        """core 侧装配：缓存目录 → 会话 → 日志 → 流服务回调（P2-3 分段）。"""
        self.cfg = AppConfig()
        configured = str(self.cfg.get("cache_dir") or "")
        self.cache_dir = (configured or
                          os.path.join(tempfile.gettempdir(),
                                       "magnet_viewer_cache"))
        try:
            ensure_cache_dir(self.cache_dir)
        except ValueError as e:
            # 配置了高风险目录（盘符根/用户数据目录）：回退默认并写回设置
            log_warning("main.cache_dir", f"{e}，回退默认缓存目录")
            self.cache_dir = os.path.join(tempfile.gettempdir(),
                                          "magnet_viewer_cache")
            ensure_cache_dir(self.cache_dir)
            self.cfg.set("cache_dir", "")

        # 设置面板的「默认下载目录」「默认并发下载数」必须在此接入：
        # 此前 SessionManager 只收 cache_dir，两个设置项保存后完全不生效（P0-3）。
        # download_dir 空串回落 None（默认 cache_dir/downloads）；download_dir
        # 与并发数在会话启动时固定，修改后需重启程序生效（提示见设置面板）。
        self.session = SessionManager(
            self.cache_dir,
            download_dir=str(self.cfg.get("download_dir") or "").strip() or None,
            active_downloads=int(self.cfg.get("default_concurrency") or 3))
        self.session.start(proxy=self.cfg.proxy(),
                           metadata_timeout=self.cfg.get("metadata_timeout"))
        # 会话级限速 + 日志开关：启动即按配置应用（P2-18 此前承诺了
        # logging_enabled 但从未接线——docstring 与实现脱节）
        self.session.apply_rate_limit(int(self.cfg.get("download_rate_limit") or 0))
        _log_on = bool(self.cfg.get("logging_enabled"))
        logutil.set_enabled(_log_on)
        if _log_on:
            logutil.setup()
        self.bridge = _Bridge()
        self.session.on_metadata = self.bridge.metadata_ready.emit
        self.session.on_error = self.bridge.resolve_failed.emit

        self._path_to_file: dict[str, TorrentFile] = {}  # 磁盘路径 -> 文件
        # 分块映射未命中告警节流：_pieces_map 每个 HTTP 请求都会调用，
        # 同一路径只告警一次，避免日志被刷屏
        self._map_miss_warned: set[str] = set()
        # 流服务回调集（HTTP 线程入口独立成类，P2-3）：与主窗口共享
        # path_to_file / map_miss_warned 两个容器，preview_file 用 getter 取
        self.stream_cb = StreamCallbacks(
            self.session, self._path_to_file, self._map_miss_warned,
            preview_file_getter=lambda: self._preview_file)
        # 流服务多根：download_dir 配置在缓存目录之外时，下载任务文件的
        # 分块级可用性判定/按需补拉仍须可服务（url_for 携带绝对落盘路径）。
        self.server = StreamServer(
            self.cache_dir, pieces_cb=self.stream_cb.pieces_map,
            demand_cb=self.stream_cb.demand_range,
            bases=[self.session.download_dir])
        self.server.start()

        self.result: ParseResult | None = None
        self._preview_file: TorrentFile | None = None
        self._pending_video: tuple[TorrentFile, str] | None = None  # 等待首批数据
        self._stream_url: str | None = None   # 当前视频流地址（失败重试用）
        self._stream_attempts = 0
        self._last_source: str = ""           # 最近一次解析来源（转下载用）

    def _wire_signals(self):
        """信号接线：bridge / 文件树 / 预览页 / 下载页（P2-3 分段）。"""
        self.bridge.metadata_ready.connect(self._on_metadata)
        self.bridge.resolve_failed.connect(self._on_error)
        self.tree.file_activated.connect(self._open_preview)
        self.tree.add_download_requested.connect(
            lambda f: self._confirm_add_task(self._last_source))
        self.preview.stop_requested.connect(self._stop_preview)
        self.preview.to_download_requested.connect(self._preview_to_download)
        self.preview.video.seek_requested.connect(self._on_seek)
        self.preview.video.scrub_preview.connect(self._on_scrub_preview)
        self.preview.video.stream_failed.connect(self._on_stream_failed)
        self.preview.gallery.file_requested.connect(self._on_gallery_file)
        # 下载页信号接线
        self.downloads.pause_requested.connect(self._task_pause)
        self.downloads.resume_requested.connect(self._task_resume)
        self.downloads.remove_requested.connect(self._task_remove)
        self.downloads.priority_up_requested.connect(
            lambda t: self._task_priority(t, +1))
        self.downloads.priority_down_requested.connect(
            lambda t: self._task_priority(t, -1))
        self.downloads.opendir_requested.connect(self._task_open_dir)
        self.downloads.openpreview_requested.connect(self._task_open_preview)

    def _setup_timers(self):
        """定时器：状态轮询（700ms）与缓存占用（30s）。"""
        self._status_timer = QTimer(self)
        self._status_timer.setInterval(700)
        self._status_timer.timeout.connect(self._refresh_status)
        self._status_timer.start()

        # 缓存占用显示：低频刷新（预览目录文件数少，统计毫秒级）
        self._cache_timer = QTimer(self)
        self._cache_timer.setInterval(30_000)
        self._cache_timer.timeout.connect(self._refresh_cache_usage)
        self._cache_timer.start()
        self._refresh_cache_usage()

    # ---------- 拖拽与历史 ----------

    def _init_history(self):
        """磁力链/种子路径输入历史：输入框自动补全（最近 15 条，置顶去重）。"""
        self._recent_model = QStringListModel(self.cfg.recent(), self)
        completer = QCompleter(self._recent_model, self)
        completer.setCaseSensitivity(Qt.CaseInsensitive)
        self.input.setCompleter(completer)

    def dragEnterEvent(self, event: QDragEnterEvent):
        md = event.mimeData()
        if md.hasUrls() or (md.hasText()
                            and md.text().strip().lower().startswith("magnet:")):
            event.acceptProposedAction()

    def dropEvent(self, event: QDropEvent):
        md = event.mimeData()
        if md.hasUrls():
            for url in md.urls():
                path = url.toLocalFile()
                if path.lower().endswith(".torrent"):
                    self.input.setText(path)
                    self._resolve(path)
                    return
        if md.hasText() and md.text().strip().lower().startswith("magnet:"):
            text = md.text().strip()
            self.input.setText(text)
            self._resolve(text)

    # ---------- UI 构建 ----------

    def _build_ui(self):
        central = QWidget(self)
        root = QVBoxLayout(central)

        # 顶栏
        bar = QHBoxLayout()
        self.input = QLineEdit()
        self.input.setPlaceholderText("粘贴 magnet:?xt=urn:btih:... 磁力链接，或点击右侧按钮选择 .torrent 文件")
        self.btn_open = QPushButton("打开种子文件…")
        self.btn_resolve = QPushButton("解析")
        self.btn_resolve.setObjectName("primary")
        self.btn_open.clicked.connect(self._pick_torrent)
        self.btn_resolve.clicked.connect(self._resolve_input)
        self.input.returnPressed.connect(self._resolve_input)
        bar.addWidget(self.input, 1)
        bar.addWidget(self.btn_open)
        bar.addWidget(self.btn_resolve)
        self.btn_add_download = QPushButton("添加下载")
        self.btn_add_download.setToolTip(
            "把磁力链 / .torrent 添加为下载任务（可输入框粘贴后直接使用）")
        self.btn_add_download.clicked.connect(self._add_download_flow)
        bar.addWidget(self.btn_add_download)
        self.btn_settings = QPushButton("设置")
        self.btn_settings.setFixedWidth(56)
        self.btn_settings.clicked.connect(self._open_settings)
        bar.addWidget(self.btn_settings)
        root.addLayout(bar)

        self.hint = QLabel()
        # 模板保存原始指引文案：保存设置时只替换超时数字，不截断其他部分
        self._hint_template = ("解析只获取文件清单（不下载资源本体）；"
                               "双击视频/图片文件即可在「预览」页边下边播或浏览。"
                               "磁力链元数据获取超时 {} 秒。")
        self.hint.setText(
            self._hint_template.format(int(self.session.metadata_timeout)))
        self.hint.setStyleSheet(f"color:{TEXT_MUTED}; font-size:12px;")
        root.addWidget(self.hint)

        # 页签
        self.tabs = QTabWidget()
        self.tree = FileTreeWidget()
        self.preview = PreviewPane()
        self.downloads = DownloadsPane()
        self.tabs.addTab(self.tree, "文件列表")
        self.tabs.addTab(self.preview, "预览")
        self.tabs.addTab(self.downloads, "下载")
        self.tabs.setTabEnabled(TAB_PREVIEW, False)
        root.addWidget(self.tabs, 1)

        # 状态栏
        self.status_panel = StatusPanel()
        root.addWidget(self.status_panel)
        self.setCentralWidget(central)

    def _open_settings(self):
        dlg = SettingsDialog(self.cfg, self.cache_dir,
                             on_clear_cache=self._clear_cache_now, parent=self)
        if dlg.exec() == SettingsDialog.DialogCode.Accepted:
            # 代理与超时立即生效（缓存目录重启生效）
            self.session.apply_proxy(self.cfg.proxy())
            self.session.apply_metadata_timeout(
                float(self.cfg.get("metadata_timeout")))
            # 限速与日志开关：保存即热更新，无需重启
            self.session.apply_rate_limit(int(self.cfg.get("download_rate_limit") or 0))
            logutil.set_enabled(bool(self.cfg.get("logging_enabled")))
            self.hint.setText(
                self._hint_template.format(int(self.session.metadata_timeout)))
            self.status_panel.set_state("设置已保存")
            self._refresh_cache_usage()

    def _clear_cache_now(self):
        """设置对话框「立即清理缓存」：先停预览，再只清预览缓存内容。

        决策 D8：downloads/（用户下载数据）与任务持久化文件（.tasks.json /
        .resume）不在清理范围；守卫校验不变（受管标记）。
        """
        self._stop_preview()
        if not guard_ok_for_cleanup(self.cache_dir):
            log_warning("main.clear_cache_now",
                        f"拒绝清理非受管缓存目录：{self.cache_dir}")
            return -1
        try:
            cleanup = _clear_preview_cache(self.cache_dir)
        except Exception as e:
            log_warning("main.clear_cache_now", f"清理失败：{e}")
            return -1
        return cleanup

    # ---------- 动作 ----------

    def _pick_torrent(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "选择种子文件", "", "种子文件 (*.torrent)")
        if path:
            self.input.setText(path)
            self._resolve(path)

    def _resolve_input(self):
        text = self.input.text().strip()
        if text:
            self._resolve(text)

    def _resolve(self, source: str):
        self.status_panel.set_state("解析中…（加入 DHT 网络获取元数据）")
        self.preview.reset()
        self.session.stop_preview()
        self._preview_file = None
        self._pending_video = None
        self._stream_url = None
        self._stream_attempts = 0
        self._last_source = source
        self.tabs.setTabEnabled(TAB_PREVIEW, False)
        self._recent_model.setStringList(self.cfg.push_recent(source))
        try:
            self.session.resolve(source)
        except Exception as e:
            self._on_error(str(e))

    # ---------- 下载任务：添加与操作 ----------

    def _add_download_flow(self):
        """顶栏「添加下载」：优先取输入框内容，否则弹输入框要来源。"""
        text = self.input.text().strip()
        if not (text.lower().startswith("magnet:")
                or (os.path.isfile(text) and text.lower().endswith(".torrent"))):
            text, ok = QInputDialog.getText(
                self, "添加下载",
                "磁力链接或 .torrent 文件路径：", text=text)
            if not ok:
                return
        source = text.strip()
        if source:
            self._confirm_add_task(source)

    def _confirm_add_task(self, source: str):
        """添加下载确认：子目录 / 优先级 / 完成后做种 → 调 add_task。"""
        source = (source or "").strip()
        if not source:
            self.status_panel.set_state("添加下载失败：来源为空")
            return
        dlg = AddDownloadDialog(self.cfg, "（任务创建后自动获取名称）", 0,
                                default_save_dir="", parent=self)
        if dlg.exec() != AddDownloadDialog.DialogCode.Accepted:
            return
        save_subdir = dlg.save_path() or None
        try:
            tid = self.session.add_task(source, save_subdir=save_subdir,
                                        priority=dlg.priority(),
                                        seed=dlg.seed_after_complete())
        except Exception as e:
            log_warning("main.add_task", str(e))
            QMessageBox.warning(self, "添加下载失败", str(e))
            return
        self.tabs.setCurrentIndex(TAB_DOWNLOADS)
        # add_task 对重复 info_hash 返回已存在任务 id（自动去重），
        # 新任务与已存在任务统一按此提示
        self.status_panel.set_state(
            f"已提交下载任务：{str(tid)[:16]}…（重复 info_hash 自动去重不重复下载）")

    def _task_id(self, task: dict) -> str:
        return task_id(task)

    def _task_pause(self, task: dict):
        tid = self._task_id(task)
        if tid and self.session.pause_task(tid):
            self.status_panel.set_state(f"已暂停任务：{task.get('name') or tid[:16]}")
        else:
            self.status_panel.set_state("暂停失败：任务不存在或状态不可暂停")

    def _task_resume(self, task: dict):
        tid = self._task_id(task)
        if tid and self.session.resume_task(tid):
            self.status_panel.set_state(f"已恢复任务：{task.get('name') or tid[:16]}")
        else:
            self.status_panel.set_state("恢复失败：任务不存在或状态不可恢复")

    def _task_remove(self, task: dict):
        tid = self._task_id(task)
        if not tid:
            return
        name = task.get("name") or tid[:16]
        box = QMessageBox(self)
        box.setWindowTitle("删除任务")
        box.setText(f"删除任务「{name}」？\n\n请选择清理方式：")
        btn_keep = box.addButton("仅删除任务（保留文件）", QMessageBox.AcceptRole)
        btn_del = box.addButton("删除任务和文件", QMessageBox.DestructiveRole)
        box.addButton("取消", QMessageBox.RejectRole)
        box.exec()
        clicked = box.clickedButton()
        if clicked is btn_keep or clicked is btn_del:
            ok = self.session.remove_task(tid, delete_files=(clicked is btn_del))
            self.status_panel.set_state(
                f"已删除任务：{name}" if ok else f"删除任务失败：{name}")

    def _task_priority(self, task: dict, delta: int):
        if not hasattr(self.session, "set_priority"):
            self.status_panel.set_state("优先级调整暂未开放（后续版本接入）")
            return
        cur = int(task.get("priority", 1) or 1)
        try:
            self.session.set_priority(self._task_id(task),
                                      max(0, min(3, cur + delta)))
            self.status_panel.set_state("已调整任务优先级")
        except Exception as e:
            log_warning("main.priority", str(e))
            self.status_panel.set_state("调整优先级失败")

    def _task_open_dir(self, task: dict):
        path = task.get("save_path") or ""
        if not path or not os.path.isdir(path):
            path = os.path.join(self.cache_dir, "downloads")
        QDesktopServices.openUrl(QUrl.fromLocalFile(path))

    def _task_open_preview(self, task: dict):
        """下载页「打开预览」：聚焦任务 → 载入其文件树 → 定位可预览文件。

        任务文件在 downloads/<ih>/（或自定义子目录）落盘，已下载分块
        可边下边播（分块级流服务 + 按需补拉 + moov 尾部窗口）。
        """
        tid = self._task_id(task)
        if tid:
            try:
                self.session.focus_task(tid)
            except Exception as e:
                log_warning("main.focus_task", f"{e}")
        res = self.session.task_result(tid)
        if res is None:
            self.status_panel.set_state(
                f"任务「{task.get('name') or tid[:16]}」元数据未就绪"
                f"或无法载入文件列表")
            self.tabs.setCurrentIndex(TAB_DOWNLOADS)
            return
        # 与 _on_metadata 相同的映射/文件树载入（任务落盘目录带子目录前缀；
        # save_subdir 为绝对路径时直接使用——download_dir 可在缓存目录之外）
        self.result = res
        save_dir = disk_root(res.cache_dir or self.cache_dir,
                             getattr(res, "save_subdir", ""))
        self._path_to_file = {
            os.path.normpath(file_disk_path(save_dir, f)): f
            for f in res.files
        }
        self._map_miss_warned.clear()   # 新种子：重置未命中告警节流
        self.tree.populate(res)
        self.preview.gallery.set_result(res)
        self.tabs.setTabEnabled(TAB_PREVIEW, True)
        self.tabs.setCurrentIndex(TAB_FILES)
        vf = next((f for f in res.view_files if f.is_previewable), None)
        if vf is not None:
            self.tree.select_file(vf)
            self.status_panel.set_state(
                f"任务「{res.name}」已载入文件列表，选中 {vf.name}"
                f"（可预览已下载分块）")
        else:
            self.status_panel.set_state(
                f"任务「{res.name}」已载入文件列表（无可预览媒体文件）")

    def _preview_to_download(self):
        """预览页「转为下载」：当前预览种子直接转正为下载任务（零额外下载）。"""
        if not self._last_source:
            self.status_panel.set_state("请先解析磁力链或种子，再转为下载")
            return
        self._confirm_add_task(self._last_source)

    def _on_metadata(self, result: ParseResult):
        self.result = result
        # 以主窗口的 cache_dir 为准（不依赖解析侧注入，双重保险）；
        # 落盘目录含任务隔离子目录（.preview/<ih> 或 downloads/<ih>，见
        # ParseResult.save_subdir），映射键必须拼在该子目录下才能命中
        # StreamServer 实际回调的磁盘路径。save_subdir 为绝对路径时
        # disk_root 直接采用（download_dir 可在缓存目录之外）。
        save_dir = disk_root(result.cache_dir or self.cache_dir,
                             getattr(result, "save_subdir", ""))
        self._path_to_file = {
            os.path.normpath(file_disk_path(save_dir, f)): f
            for f in result.files
        }
        self._map_miss_warned.clear()   # 新种子：重置未命中告警节流
        self.tree.populate(result)
        self.preview.gallery.set_result(result)
        self.tabs.setTabEnabled(TAB_PREVIEW, True)
        self.tabs.setCurrentIndex(TAB_FILES)
        self.status_panel.set_state(
            f"解析完成：{result.name} · {len(result.view_files)} 个文件 · "
            f"共 {result.total_size / 1024 / 1024:.1f} MB · info_hash={result.info_hash[:16]}…")

    def _on_error(self, msg: str):
        self.status_panel.set_state(f"失败：{msg}")
        QMessageBox.warning(self, "解析失败", msg)

    def _open_preview(self, f: TorrentFile):
        if self.result is None:
            return
        self._enforce_cache_quota()
        try:
            self.session.start_preview(f)
        except Exception as e:
            QMessageBox.warning(self, "预览失败", str(e))
            return
        self._preview_file = f
        self._pending_video = None
        self._stream_url = None
        self._stream_attempts = 0
        if f.is_video:
            self.preview.show_video(f)
            # 不立即播放：等头部连续数据 + 尾部索引块（moov，MP4/MOV）都就绪后
            # 再 set_stream，避免播放器读到稀疏零数据或探测不到 moov
            self._stream_url = self.server.url_for(self._stream_rel(f))
            self._pending_video = (f, self._stream_url)
            self.preview.video.set_waiting(f.name, f.size)
        else:
            self.preview.show_gallery(f)
        self.tabs.setCurrentIndex(TAB_PREVIEW)

    def _stream_rel(self, f: TorrentFile) -> str:
        return stream_rel(self.result, self.cache_dir, f)

    def _stop_preview(self):
        """用户点击「停止预览」：停下载、停播放、复位界面。"""
        self.session.stop_preview()
        self.preview.reset()
        self._preview_file = None
        self._pending_video = None
        self._stream_url = None
        self._stream_attempts = 0
        self.status_panel.set_state("已停止预览（下载配额已释放）")

    def _on_seek(self, byte_offset: int):
        """播放位置跳转 → 调度器从对应分块重新开始顺序下载。"""
        if self._preview_file is not None and self._preview_file.is_video:
            self.session.scheduler.seek_to_byte(byte_offset)

    def _on_scrub_preview(self, byte_offset: int):
        """拖动中预取：目标区间先点播，松手前数据已在路上。

        request_range 自带 60 块上限，预取 4MB 足够开播解码；
        误预取（拖过又拖回）由下次 seek 的 clear_piece_deadlines 回收。
        """
        if self._preview_file is not None and self._preview_file.is_video:
            self.session.scheduler.request_range(
                byte_offset, byte_offset + 4 * 1024 * 1024)

    def _on_gallery_file(self, f):
        """画廊中切换到未下载的图片 → 按需下载该文件。"""
        if f is None or f == self._preview_file:
            return
        # A2：与 _open_preview（视频路径）对齐——预览启停前必须走缓存配额，
        # 否则画廊连刷大量图片会绕过 limit 无上限增长。
        self._enforce_cache_quota()
        try:
            self.session.start_preview(f)
            self._preview_file = f
        except Exception:
            pass

    # ---------- 预览缓存配额（P2-2） ----------

    def _enforce_cache_quota(self):
        """切换预览前执行缓存配额：超限按 LRU 清最旧的预览目录。

        只动 <cache>/.preview/<ih>/；保护名单来自 session.protected_dirs()
        （活跃句柄的落盘目录），downloads/ 与任务持久化文件天然不在
        扫描范围。limit<=0 表示用户关闭了配额。
        """
        limit = int(self.cfg.get("cache_limit_mb") or 0)
        if limit <= 0:
            return
        preview_root = os.path.join(self.cache_dir, ".preview")
        if not os.path.isdir(preview_root):
            return
        keep = {os.path.normcase(p)
                for p in self.session.protected_dirs()}
        keep.add(os.path.normcase(self.cache_dir))
        try:
            total, _freed = enforce_preview_limit(
                preview_root, limit, keep,
                warn=lambda m: log_warning("main.cache_quota", m))
        except Exception as e:
            log_warning("main.cache_quota", f"配额清理异常（已忽略）：{e}")
            return
        self._update_cache_usage(total)

    def _refresh_cache_usage(self):
        """刷新状态栏缓存占用显示（低频：30 秒定时器 + 打开设置后）。"""
        preview_root = os.path.join(self.cache_dir, ".preview")
        total = dir_size_bytes(preview_root) if os.path.isdir(preview_root) else 0
        self._update_cache_usage(total)

    def _update_cache_usage(self, total_bytes: int):
        limit = int(self.cfg.get("cache_limit_mb") or 0)
        if limit > 0:
            self.status_panel.set_cache_usage(
                f"缓存 {human_size(total_bytes)} / {human_size(limit * 1024 * 1024)}")
        else:
            # 不限制时仅在确有占用时提示，避免常驻噪音
            self.status_panel.set_cache_usage(
                f"缓存 {human_size(total_bytes)}" if total_bytes > 0 else "")

    def _pieces_map(self, disk_path: str):
        """流服务回调（薄委托，逻辑在 StreamCallbacks.pieces_map）。"""
        return self.stream_cb.pieces_map(disk_path)

    def _demand_range(self, disk_path: str, start: int, end_excl: int):
        """流服务回调（薄委托，逻辑在 StreamCallbacks.demand_range）。"""
        self.stream_cb.demand_range(disk_path, start, end_excl)

    def _refresh_status(self):
        # 预览调度器的滚动预约窗口必须周期性驱动：它负责按播放位置（含拖动
        # 后的 seek 位置）持续把后续分块置为最高优先级。此前仅 begin()/seek
        # 时调用一次，窗口从不滚动，seek 点之后完全依赖流服务的点播回调。
        try:
            if self.session.scheduler.active:
                self.session.scheduler.tick()
        except Exception as e:
            log_warning("main.scheduler.tick", f"{e}")
        st = self.session.status()
        self.status_panel.update_status(st)
        self.preview.gallery.update_status(st)
        if self._preview_file is not None and st is not None:
            self.preview.video.update_buffer(st.get("buffer", 0.0),
                                             st.get("download_rate", 0))
            # 进度条缓冲分段着色（一次批量取块位图，见 fetcher 注释）
            try:
                segs = self.session.buffered_segments_of_preview()
            except Exception as e:
                segs = None
                log_warning("main.buffered_segments", f"{e}")
            if segs is not None:
                self.preview.video.update_segments(segs)
        # 下载任务列表（700ms 轮询注入；tasks() 自带派生进度/速度/ETA）
        try:
            self.downloads.set_tasks(self.session.tasks())
        except Exception as e:
            log_warning("main.refresh_tasks", f"{e}")
        # 视频开播条件：头部连续数据 + 尾部索引块（moov）都已就绪
        if self._pending_video is not None and st is not None:
            f, url = self._pending_video
            contig = st.get("contiguous", 0)
            if contig >= min(1024 * 1024, f.size) and st.get("tail_ready", True):
                self._pending_video = None
                self.preview.video.set_stream(url, f.name, f.size)

    # ---------- 播放失败重试 ----------

    def _on_stream_failed(self):
        """播放器打开媒体失败：多半是索引块/数据仍在下载，稍后重试。"""
        if self._preview_file is None or self._pending_video is not None:
            return  # 已停止或尚未开播
        if self._stream_url is None:
            return
        if self._stream_attempts >= 4:
            self.preview.video.show_error("播放器多次打开失败：数据仍未就绪，继续下载中…")
            self.status_panel.set_state("播放器多次打开失败：数据仍未就绪，继续下载中…")
            return
        self._stream_attempts += 1
        delay = 1500 * self._stream_attempts
        QTimer.singleShot(delay, self._retry_stream)

    def _retry_stream(self):
        if self._preview_file is None or self._stream_url is None:
            return
        if self._pending_video is not None:
            return  # 用户已重新选文件/重新开播：放弃旧重试（竞态守卫）
        st = self.session.status()
        if st is None:
            QTimer.singleShot(1500, self._retry_stream)
            return
        if not st.get("tail_ready", True) or st.get("contiguous", 0) < 1:
            # 数据仍未就绪：静默等待，不算失败次数，避免反复弹错误
            QTimer.singleShot(1500, self._retry_stream)
            return
        # 带上出错前的位置续播：setSource 会从头开始，不带位置的话
        # 拖动进度条后因数据未就绪报错时，用户会看到「突然从头重播」
        self.preview.video.set_stream(self._stream_url,
                                      self._preview_file.name,
                                      self._preview_file.size,
                                      resume_ms=self.preview.video.take_resume_ms())
        self._stream_attempts = 0   # 开播成功：重试计数归零，下次失败可重新重试

    # ---------- 关闭 ----------

    def closeEvent(self, event):
        try:
            self.session.shutdown()
            self.server.shutdown()
            if self.cfg.get("clear_cache_on_exit"):
                # 决策 D8：退出清理只清预览缓存，downloads/（用户下载数据）
                # 与任务持久化文件（.tasks.json/.resume）保留；
                # 根目录仍须通过受管标记守卫（拒绝清非受管目录）
                if not guard_ok_for_cleanup(self.cache_dir):
                    log_warning("main.close.clear_cache",
                                f"拒绝清理非受管缓存目录：{self.cache_dir}")
                else:
                    _clear_preview_cache(self.cache_dir)
        finally:
            super().closeEvent(event)
