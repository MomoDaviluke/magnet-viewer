"""主窗口：视图组装 + `_Bridge` 信号接线 + 关闭路径（Wave 2 拆分后收敛）。

架构审查（ARCHITECTURE.md §2.3）判定本类曾是「上帝对象」：视图接线、缓存
治理、流媒体生命周期、任务操作、预览面板全揉在一个类里。Wave 2 按内聚簇
抽出四个协作者，MainWindow 收敛为「core 装配 + 视图组装 + 信号槽接线 +
停机守卫入口槽 + 关闭路径」：

- ui/cache_host.CacheHost     —— 清理入口/活名单/LRU 配额/占用显示
- ui/stream_host.StreamHost   —— 流服务实例/开播门控/失败重试/流侧状态
- ui/task_controller.TaskController —— 添加下载流程 + 任务 CRUD 槽
- ui/inbound_host.InboundHost —— 入站能力探测（含其跨线程信号桥）

刻意保留在本类的簇（不硬拆的理由）：
- 关闭路径（closeEvent/遮罩/轮询/硬超时/后台收尾编排）：Qt 事件覆写必须
  留在 QMainWindow 子类，且它是 close_lag_test 白盒断言密度最高的表面，
  抽出只增加属性路径而无内聚收益——「窗口生命周期」本就是主窗口职责。
- 视图装配（_build_ui / _on_metadata / _task_open_preview 的树/画廊/页签
  灌入部分）：搬进协作者反而要反向抓一堆窗口控件。
- 停机守卫（I2）入口槽：22 个入口/桥回调的首行
  ``if self._shutdown_started: return`` 留在 MainWindow（close_lag §G 用
  AST 逐条清点），协作者方法是守卫通过后的执行体、不重复守卫。

§2.2 UI 依赖规则：ui/* 只准 import core.models / core.config / core.fetcher
（facade）/ core.logutil。cache_guard/cache_mode/cache_quota/proxycheck/
stream_server/inbound 的调用面全部经 SessionManager facade 方法或 facade
模块级再导出取用，协作者模块同样受此白名单约束。
"""
from __future__ import annotations

import os
import tempfile
import threading

from PySide6.QtCore import (QObject, QTimer, QStringListModel, Qt, Signal)
from PySide6.QtGui import QDragEnterEvent, QDropEvent
from PySide6.QtWidgets import (QCompleter, QFileDialog, QHBoxLayout,
                               QLabel, QLineEdit, QMainWindow, QMessageBox,
                               QProgressBar, QPushButton, QTabWidget,
                               QVBoxLayout, QWidget)

from core.config import AppConfig
from core.fetcher import SessionManager, ensure_cache_dir
from core import logutil
from core.logutil import log_warning
from core.models import ParseResult, TorrentFile, disk_root, file_disk_path
from ui.cache_host import CacheHost, background_cache_text
from ui.downloads_pane import DownloadsPane
from ui.file_tree import FileTreeWidget
from ui.inbound_host import InboundHost
from ui.preview_pane import PreviewPane
from ui.settings_dialog import SettingsDialog
from ui.status_panel import StatusPanel
from ui.stream_host import StreamHost
from ui.task_controller import TaskController, task_id
from ui.theme import SP_LG, SP_MD, SP_SM

TAB_FILES, TAB_PREVIEW, TAB_DOWNLOADS = 0, 1, 2


class _Bridge(QObject):
    """把 core 后台线程的回调转成 Qt 信号（跨线程安全）。"""
    metadata_ready = Signal(object)
    resolve_failed = Signal(str)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        # ---- 关窗异步化状态（plan 阶段 A A2）----
        # 放在最前：停机窗口守卫（I2 整改）会在入口/桥回调首行读它，而这些
        # 函数理论上可能在构造期被 Qt/信号回调触达（属性必须先于任何 UI 动作存在）。
        self._shutting_down = False       # 已真正关窗（closeEvent 放行依据/幂等）
        self._shutdown_started = False    # 停机窗口已开始（守卫依据；只置真不复位）
        self._shutdown_stage = ""         # 后台线程写的进度文案（GUI 轮询读取）
        self._shutdown_done = False       # 后台线程完成标志
        self._shutdown_overlay = None
        self._shutdown_label = None
        self._shutdown_timer = None
        self.setWindowTitle("磁力链实时解析查看器 Magnet Viewer")
        # 双主题 + 视觉规格（用户拍板）：更大的默认窗口让文件树/画廊有呼吸感，
        # 并给最小尺寸兜底（小于此值布局会挤成一团）。
        self.resize(1200, 800)
        self.setMinimumSize(960, 640)
        self._setup_core()
        # ---- UI ----
        self._build_ui()
        self._init_history()
        self.setAcceptDrops(True)  # 拖拽 .torrent 文件 / magnet 文本进窗口直接解析
        self._wire_signals()
        self._setup_timers()

    def _setup_core(self):
        """core 侧装配 + 协作者构造（P2-3 分段，Wave 2 拆分收敛）。

        协作者一律晚绑定读宿主成员（不快照 cache_dir/cfg/session）：白盒
        测试在运行中重绑这些属性来验证清理/配额路径，快照会让断言假通过。
        构造顺序只要求 session 与两个共享容器先于 StreamHost 存在
        （流回调集在构造期捕获容器对象——与拆分前逐字一致）。
        """
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
            active_downloads=int(self.cfg.get("default_concurrency") or 3),
            # 阶段 B：预览缓存模式（convert/hold）——每次关预览时现读，
            # 保存设置即「下次关预览生效」，无需重建会话。
            cache_mode_get=lambda: self.cfg.get("preview_cache_mode"),
            # 入站能力（D-2026-09-18）：端口映射 + 出口网卡。启动时读一次
            # 固定；端口映射另可在保存设置时热更新，无需重启。
            port_mapping_get=lambda: self.cfg.get("port_mapping"),
            listen_interface_get=lambda: self.cfg.get("listen_interface"),
            extra_trackers_get=lambda: self.cfg.get("extra_trackers"))
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
        # 分块映射未命中告警节流：pieces_map 每个 HTTP 请求都会调用，
        # 同一路径只告警一次，避免日志被刷屏
        self._map_miss_warned: set[str] = set()

        # ---- 协作者（Wave 2 拆分）----
        self.cache_host = CacheHost(self)
        self.stream_host = StreamHost(self)
        # 流服务实例别名：关闭收尾与测试直访沿用 w.server 路径（同一对象）
        self.server = self.stream_host.server
        self.inbound_host = InboundHost(self)
        self.task_ctrl = TaskController(self)

        self.result: ParseResult | None = None
        self._preview_file: TorrentFile | None = None
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
        if self._shutdown_started:
            return          # 停机窗口守卫（I2）：不启新工作、不弹模态
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
        # 顶栏 / 状态栏做成通栏色带（圆角卡片由 QSS #topBar 负责）：
        # 外边距保持 0（通栏），纵向间距 12px 给三段色带之间留呼吸感
        # （旧版 0 间距，界面挤在一起）。
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(SP_MD)

        # 顶栏（#topBar：面板底色 + 下边框）
        bar_wrap = QWidget()
        bar_wrap.setObjectName("topBar")
        bar = QHBoxLayout(bar_wrap)
        bar.setContentsMargins(SP_LG, SP_MD, SP_LG, SP_MD)
        bar.setSpacing(SP_MD)
        self.input = QLineEdit()
        self.input.setObjectName("urlInput")
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
        self.btn_settings.setObjectName("ghost")
        self.btn_settings.clicked.connect(self._open_settings)
        bar.addWidget(self.btn_settings)
        root.addWidget(bar_wrap)

        # 页签
        self.tabs = QTabWidget()
        self.tabs.setObjectName("mainTabs")
        self.tree = FileTreeWidget()
        self.preview = PreviewPane()
        self.downloads = DownloadsPane()
        self.tabs.addTab(self.tree, "文件列表")
        self.tabs.addTab(self.preview, "预览")
        self.tabs.addTab(self.downloads, "下载")
        self.tabs.setTabEnabled(TAB_PREVIEW, False)
        root.addWidget(self.tabs, 1)

        # hint：移到页签下方、状态栏上方（与输入框同行会挤顶栏）。
        # 右侧挂「检测入站能力」按钮（D-2026-09-18）：一键探测当前监听端口
        # 能否从公网入站连通，不通则给出在路由器手动端口转发的步骤。
        # 探测链（后台线程 + 信号桥 + 结果弹窗）在 ui/inbound_host.py。
        # 用一行 HBox 包住，左文案（拉伸）+ 右按钮，避免顶栏挤压。
        self.hint_row = QWidget()
        self.hint_row.setObjectName("hintRow")
        hint_lay = QHBoxLayout(self.hint_row)
        hint_lay.setContentsMargins(SP_LG, SP_SM, SP_LG, SP_SM)
        hint_lay.setSpacing(SP_MD)
        self.hint = QLabel()
        # 模板保存原始指引文案：保存设置时只替换超时数字，不截断其他部分
        self._hint_template = ("解析只获取文件清单（不下载资源本体）；"
                               "双击视频/图片文件即可在「预览」页边下边播或浏览。"
                               "磁力链元数据获取超时 {} 秒。")
        self.hint.setText(
            self._hint_template.format(int(self.session.metadata_timeout)))
        self.hint.setObjectName("hint")
        self.hint.setWordWrap(True)
        hint_lay.addWidget(self.hint, 1)
        self.btn_inbound = QPushButton("检测入站能力")
        self.btn_inbound.setObjectName("ghost")
        self.btn_inbound.setToolTip(
            "检测当前监听端口能否从公网入站连通（UPnP 不可用时常不通）。\n"
            "不通会给出在路由器手动做端口转发的步骤。")
        self.btn_inbound.clicked.connect(self._detect_inbound)
        hint_lay.addWidget(self.btn_inbound)
        root.addWidget(self.hint_row)

        # 状态栏（#statusBar：面板底色 + 上边框）
        self.status_panel = StatusPanel()
        root.addWidget(self.status_panel)
        self.setCentralWidget(central)

    def _open_settings(self):
        if self._shutdown_started:
            return          # 停机窗口守卫（I2）：不启新工作、不弹模态
        dlg = SettingsDialog(self.cfg, self.cache_dir,
                             on_clear_cache=self._clear_cache_now,
                             # C1：注入活名单取器（UI 不直闯 core：闭包封装
                             # session.protected_dirs），对话框清编辑框当前值时
                             # 同样复核活任务目录（含转正的 .preview/<ih>）
                             keep_dirs_get=self._live_cache_dirs,
                             parent=self)
        if dlg.exec() == SettingsDialog.DialogCode.Accepted:
            # 代理与超时立即生效（缓存目录重启生效）
            self.session.apply_proxy(self.cfg.proxy())
            self.session.apply_metadata_timeout(
                float(self.cfg.get("metadata_timeout")))
            # 限速与日志开关：保存即热更新，无需重启
            self.session.apply_rate_limit(int(self.cfg.get("download_rate_limit") or 0))
            # 端口映射（UPnP/NAT-PMP）：保存即热更新，做种/入站连接的前提
            self.session.apply_port_mapping(bool(self.cfg.get("port_mapping")))
            logutil.set_enabled(bool(self.cfg.get("logging_enabled")))
            self.hint.setText(
                self._hint_template.format(int(self.session.metadata_timeout)))
            self.status_panel.set_state("设置已保存")
            self._refresh_cache_usage()

    # ---------- 缓存治理入口槽（执行体在 ui/cache_host.py） ----------

    def _clear_cache_now(self):
        """设置对话框「立即清理缓存」：入口守卫 + 委托 CacheHost.clear_now。"""
        if self._shutdown_started:
            return          # 停机窗口守卫（I2）：不启新工作、不弹模态
        return self.cache_host.clear_now()

    def _live_cache_dirs(self) -> set[str]:
        """活任务落盘目录快照（委托 CacheHost.live_dirs，C1 复核名单）。"""
        return self.cache_host.live_dirs()

    def _clear_preview_cache_now(self, keep_dirs=set(),
                                 log_key: str = "main.clear_cache") -> int:
        """守卫 + 清理的统一执行段（委托 CacheHost，D8/D9/C1）。

        保留本入口槽：手动清理与退出收尾都经此调用——close_lag §C 与
        gui_feature [4b-3] 以**实例属性替换**本槽位断言收尾顺序/名单快照，
        故 _shutdown_worker 必须走 ``self._clear_preview_cache_now`` 晚绑定。
        """
        return self.cache_host.clear_preview_cache_now(
            keep_dirs=keep_dirs, log_key=log_key)

    def _enforce_cache_quota(self):
        """切换预览前的缓存配额执行（委托 CacheHost.enforce_quota，P2-2）。

        有意不加停机守卫（close_lag NOT_GUARDED 清点）：入口
        （_open_preview/_on_gallery_file）已守。
        """
        self.cache_host.enforce_quota()

    def _refresh_cache_usage(self):
        """刷新状态栏缓存占用显示（委托 CacheHost.refresh_usage，阶段 3）。"""
        self.cache_host.refresh_usage()

    # ---------- 动作 ----------

    def _pick_torrent(self):
        if self._shutdown_started:
            return          # 停机窗口守卫（I2）：不启新工作、不弹模态
        path, _ = QFileDialog.getOpenFileName(
            self, "选择种子文件", "", "种子文件 (*.torrent)")
        if path:
            self.input.setText(path)
            self._resolve(path)

    def _resolve_input(self):
        if self._shutdown_started:
            return          # 停机窗口守卫（I2）：不启新工作、不弹模态
        text = self.input.text().strip()
        if text:
            self._resolve(text)

    def _resolve(self, source: str):
        if self._shutdown_started:
            return          # 停机窗口守卫（I2）：不启新工作、不弹模态
        self.status_panel.set_state("解析中…（加入 DHT 网络获取元数据）")
        self.preview.reset()
        self.session.stop_preview()
        self._preview_file = None
        self.stream_host.reset_state()
        self._last_source = source
        self.tabs.setTabEnabled(TAB_PREVIEW, False)
        self._recent_model.setStringList(self.cfg.push_recent(source))
        try:
            self.session.resolve(source)
        except Exception as e:
            self._on_error(str(e))

    # ---------- 入站能力检测（D-2026-09-18，执行体在 ui/inbound_host.py） ----------

    def _detect_inbound(self):
        """「检测入站能力」入口：守卫 + 委托 InboundHost.detect（防重入在其内）。"""
        if self._shutdown_started:
            return          # 停机窗口守卫（I2）：不启新工作、不弹模态
        self.inbound_host.detect()

    # ---------- 下载任务：添加与操作（执行体在 ui/task_controller.py） ----------

    def _add_download_flow(self):
        """顶栏「添加下载」：入口守卫 + 委托 TaskController.add_download_flow。"""
        if self._shutdown_started:
            return          # 停机窗口守卫（I2）：不启新工作、不弹模态
        self.task_ctrl.add_download_flow()

    def _confirm_add_task(self, source: str):
        """添加下载确认：入口守卫 + 委托 TaskController.confirm_add_task。"""
        if self._shutdown_started:
            return          # 停机窗口守卫（I2）：不启新工作、不弹模态
        self.task_ctrl.confirm_add_task(source)

    def _task_id(self, task: dict) -> str:
        return task_id(task)

    def _task_pause(self, task: dict):
        if self._shutdown_started:
            return          # 停机窗口守卫（I2）：不启新工作、不弹模态
        self.task_ctrl.pause(task)

    def _task_resume(self, task: dict):
        if self._shutdown_started:
            return          # 停机窗口守卫（I2）：不启新工作、不弹模态
        self.task_ctrl.resume(task)

    def _task_remove(self, task: dict):
        if self._shutdown_started:
            return          # 停机窗口守卫（I2）：不启新工作、不弹模态
        self.task_ctrl.remove(task)

    def _task_priority(self, task: dict, delta: int):
        if self._shutdown_started:
            return          # 停机窗口守卫（I2）：不启新工作、不弹模态
        self.task_ctrl.priority(task, delta)

    def _task_open_dir(self, task: dict):
        """下载页「打开目录」：委托 TaskController（有意不加守卫，见 close_lag）。"""
        self.task_ctrl.open_dir(task)

    def _task_open_preview(self, task: dict):
        """下载页「打开预览」：聚焦任务 → 载入其文件树 → 定位可预览文件。

        任务文件在 downloads/<ih>/（或自定义子目录）落盘，已下载分块
        可边下边播（分块级流服务 + 按需补拉 + moov 尾部窗口）。

        留在 MainWindow：主体是「把任务的 ParseResult 灌进文件树/画廊/页签」
        的视图装配，与任务 CRUD 不同轴（见模块头注释的边界说明）。
        """
        if self._shutdown_started:
            return          # 停机窗口守卫（I2）：不启新工作、不弹模态
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
        self._load_result(res, TAB_FILES)
        vf = next((f for f in res.view_files if f.is_previewable), None)
        if vf is not None:
            self.tree.select_file(vf)
            self.status_panel.set_state(
                f"任务「{res.name}」已载入文件列表，选中 {vf.name}"
                # 与拆分前逐字一致的提示文案
                f"（可预览已下载分块）")
        else:
            self.status_panel.set_state(
                f"任务「{res.name}」已载入文件列表（无可预览媒体文件）")

    def _preview_to_download(self):
        """预览页「转为下载」：当前预览种子直接转正为下载任务（零额外下载）。"""
        if self._shutdown_started:
            return          # 停机窗口守卫（I2）：不启新工作、不弹模态
        if not self._last_source:
            self.status_panel.set_state("请先解析磁力链或种子，再转为下载")
            return
        self._confirm_add_task(self._last_source)

    def _load_result(self, result: ParseResult, tab: int):
        """把解析结果灌进视图：磁盘路径映射 + 文件树 + 画廊 + 页签（共用装配段）。

        以主窗口的 cache_dir 为准（不依赖解析侧注入，双重保险）；
        落盘目录含任务隔离子目录（.preview/<ih> 或 downloads/<ih>，见
        ParseResult.save_subdir），映射键必须拼在该子目录下才能命中
        StreamServer 实际回调的磁盘路径。save_subdir 为绝对路径时
        disk_root 直接采用（download_dir 可在缓存目录之外）。
        """
        self.result = result
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
        self.tabs.setCurrentIndex(tab)

    def _on_metadata(self, result: ParseResult):
        if self._shutdown_started:
            return          # 停机窗口守卫（I2）：不启新工作、不弹模态
        self._load_result(result, TAB_FILES)
        self.status_panel.set_state(
            f"解析完成：{result.name} · {len(result.view_files)} 个文件 · "
            f"共 {result.total_size / 1024 / 1024:.1f} MB · info_hash={result.info_hash[:16]}…")

    def _on_error(self, msg: str):
        if self._shutdown_started:
            return          # 停机窗口守卫（I2）：不启新工作、不弹模态
        self.status_panel.set_state(f"失败：{msg}")
        QMessageBox.warning(self, "解析失败", msg)

    def _open_preview(self, f: TorrentFile):
        if self._shutdown_started:
            return          # 停机窗口守卫（I2）：不启新工作、不弹模态
        if self.result is None:
            return
        self._enforce_cache_quota()
        try:
            self.session.start_preview(f)
        except Exception as e:
            QMessageBox.warning(self, "预览失败", str(e))
            return
        self._preview_file = f
        self.stream_host.reset_state()
        if f.is_video:
            self.preview.show_video(f)
            # 不立即播放：等头部连续数据 + 尾部索引块（moov，MP4/MOV）都就绪
            # 后再 set_stream——门控放行由 StreamHost.tick 在状态轮询里做（C3）
            self.stream_host.begin_video(f)
        else:
            self.preview.show_gallery(f)
        self.tabs.setCurrentIndex(TAB_PREVIEW)

    def _stop_preview(self):
        """用户点击「停止预览」：停下载、停播放、复位界面。

        有意不加停机守卫（停机语义、幂等，且被清理/收尾路径复用）。
        """
        self.session.stop_preview()
        self.preview.reset()
        self._preview_file = None
        self.stream_host.reset_state()
        self.status_panel.set_state("已停止预览（下载配额已释放）")

    def _on_seek(self, byte_offset: int):
        """播放位置跳转 → 调度器从对应分块重新开始顺序下载。"""
        if self._shutdown_started:
            return          # 停机窗口守卫（I2）：不启新工作、不弹模态
        self.stream_host.seek_to(byte_offset)

    def _on_scrub_preview(self, byte_offset: int):
        """拖动中预取：目标区间先点播（委托 StreamHost，节流在播放控件侧）。"""
        if self._shutdown_started:
            return          # 停机窗口守卫（I2）：不启新工作、不弹模态
        self.stream_host.scrub_request(byte_offset)

    def _on_gallery_file(self, f):
        """画廊中切换到未下载的图片 → 按需下载该文件。"""
        if self._shutdown_started:
            return          # 停机窗口守卫（I2）：不启新工作、不弹模态
        if f is None or f == self._preview_file:
            return
        # A2：与 _open_preview（视频路径）对齐——预览启停前必须走缓存配额，
        # 否则画廊连刷大量图片会绕过 limit 无上限增长。
        self._enforce_cache_quota()
        try:
            self.session.start_preview(f)
            self._preview_file = f
        except Exception as e:
            # S-10：与 _open_preview（视频路径 :549）同模式——启动失败必须给用户
            # 反馈，不再静默吞掉；失败时保持原 _preview_file 不变（赋值未执行）。
            QMessageBox.warning(self, "预览失败", str(e))

    # ---------- 流服务回调兼容薄委托（逻辑在 StreamCallbacks） ----------

    def _pieces_map(self, disk_path: str):
        """流服务回调（薄委托，逻辑在 StreamCallbacks.pieces_map）。"""
        return self.stream_host.callbacks.pieces_map(disk_path)

    def _demand_range(self, disk_path: str, start: int, end_excl: int):
        """流服务回调（薄委托，逻辑在 StreamCallbacks.demand_range）。"""
        self.stream_host.callbacks.demand_range(disk_path, start, end_excl)

    def _refresh_status(self):
        """700ms 状态轮询：调度器驱动 + 状态栏/画廊/缓冲着色 + 开播门控。

        预览调度器的滚动预约窗口必须周期性驱动：它负责按播放位置（含拖动
        后的 seek 位置）持续把后续分块置为最高优先级。此前仅 begin()/seek
        时调用一次，窗口从不滚动，seek 点之后完全依赖流服务的点播回调。
        """
        try:
            if self.session.scheduler.active:
                self.session.scheduler.tick()
        except Exception as e:
            log_warning("main.scheduler.tick", f"{e}")
        st = self.session.status()
        self.status_panel.update_status(st)
        self.preview.gallery.update_status(st)
        if self._preview_file is not None and st is not None:
            self.preview.video.update_buffer(
                st.get("buffer", 0.0),
                st.get("download_rate", 0),
                # D3：convert 档播放中补「后台缓存完整文件」注记（现成的
                # file_progress 数组，零新增查询路径；hold 档恒 None）
                background_cache_text(str(self.cfg.get("preview_cache_mode")),
                                      self._preview_file,
                                      st.get("file_progress") or []))
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
        # 视频开播门控（头部数据 + 尾部 moov 入口，C3）：判据与文案在
        # StreamHost.tick——本方法只负责把状态快照喂进去。
        self.stream_host.tick(st)

    # ---------- 播放失败重试入口槽（执行体在 ui/stream_host.py） ----------

    def _on_stream_failed(self):
        """播放器打开媒体失败：入口守卫 + 委托 StreamHost（催数据 + 退避重试）。"""
        if self._shutdown_started:
            return          # 停机窗口守卫（I2）：不启新工作、不弹模态
        self.stream_host.on_stream_failed()

    def _retry_stream(self):
        """singleShot 重试链入口：入口守卫 + 委托 StreamHost.retry_stream。"""
        if self._shutdown_started:
            return          # 停机窗口守卫（I2）：不启新工作、不弹模态
        self.stream_host.retry_stream()

    # ---------- 关闭 ----------

    # 硬超时：后台收尾卡死时也必须能真正关掉（plan A2）
    SHUTDOWN_HARD_TIMEOUT_MS = 10000

    def _shutdown_worker(self) -> None:
        """关闭收尾（**后台线程**执行）：快照 → 停会话 → 停流服务 → 可选清缓存。

        为什么要异步（Minor 10 文案诚实化，口径 = close_lag_probe 实测）：
        关窗约 3.8s 的真来源是 **session.shutdown()**——内部 2s join + 最多
        3s fastresume drain，再加 remove_torrent/会话析构的磁盘 flush——另加
        server.shutdown ≈0.5s。退出清理的 rmtree 很便宜（实测 200MB .preview
        约 15ms），**不是**耗时主因（旧注释把「清 GB 级缓存要数秒」当既定
        事实，已按实测改写）。全部在 GUI 线程做就是「关闭卡死」。

        硬超时风险（Minor 5）：本方法跑在 **daemon 线程**上；硬超时
        （``SHUTDOWN_HARD_TIMEOUT_MS``）到点会强制关窗并结束进程——此时本
        线程可能被**中途结束**，最新一次续传状态（fastresume）可能来不及
        落盘，下次启动按更早的 ``.resume`` 续传（或对未确认分块重新校验）。

        C1 铁律不变：保护名单快照必须在 shutdown 之前取。

        本方法运行在后台线程：**绝不碰任何 QWidget**，只写
        ``self._shutdown_stage`` 字符串（进度文案由 GUI 线程的
        ``_poll_shutdown`` 更新）；完成标志 ``self._shutdown_done`` 同理。

        留在 MainWindow（Wave 2 评估结论）：收尾编排跨 session/server/cache
        三个协作者本身，且清理走 ``self._clear_preview_cache_now`` 晚绑定——
        close_lag §C 与 gui_feature [4b-3] 正是替换该实例属性来断言收尾
        顺序与名单快照时机的；抽成独立类只会把测试替身面变长而无内聚收益。
        """
        try:
            self._shutdown_stage = "正在保存任务状态…"
            # D4b：clear_cache_on_exit 单读（旧实现在名单快照与清理分支
            # 各读一次——双读之间设置被改会「快照了却不清理/清理了没快照」
            # 的错拍，且 False 时白算一遍 protected_dirs()）。
            # C1 时机结论（实证 core/session.SessionCore.shutdown）：shutdown
            # 末段 clear_runtime_state_locked() 清空注册表——**事后**
            # protected_dirs() 必为空。名单快照必须在 shutdown 之前抓。
            exit_clear = bool(self.cfg.get("clear_cache_on_exit"))
            exit_keep = self._live_cache_dirs() if exit_clear else set()
            self.session.shutdown()
            self._shutdown_stage = "正在停止流服务…"
            self.server.shutdown()
            if exit_clear:
                # 决策 D8：退出清理只清预览缓存，downloads/（用户下载数据）
                # 与任务持久化文件（.tasks.json/.resume）保留；
                # 根目录仍须通过受管标记守卫（拒绝清非受管目录）。
                # 阶段 C C1：清理在 shutdown（句柄已全部移除、文件锁释放）
                # **之后**执行，但保护名单用 shutdown **之前**的快照——
                # convert 转正任务目录（.preview/<ih>）连同内容跳过：清单与
                # .resume 保留、重启 restore 续传，文件不被删光（防互噬）。
                self._shutdown_stage = "正在清理预览缓存…"
                self._clear_preview_cache_now(
                    keep_dirs=exit_keep, log_key="main.close.clear_cache")
        except Exception as e:
            log_warning("main.shutdown_worker", f"{e}")
        finally:
            self._shutdown_done = True

    def closeEvent(self, event):
        """关闭：阻塞工作交给后台线程，GUI 只显示遮罩并轮询（plan A2）。

        第一次 ``close()``：忽略事件 → 停周期轮询 → 起遮罩 + 后台线程 +
        100ms 轮询 + 10s 硬超时；后台完成或硬超时后置 ``_shutting_down``
        再 ``close()`` 一次，此时直接放行。窗口在遮罩期间始终可响应，
        不会出现 Windows 的「未响应」白屏。

        两个与 ``_shutting_down`` 分开的标志：
        - ``_shutdown_started``：停机窗口**一开始**就置真且不再复位——I2
          整改的守卫读它（窗口期内与关窗后都不许再起新工作/弹模态）。
          ``_shutting_down`` 的真实语义是「已真正关窗」（本方法用它放行
          关闭），窗口期它仍是假，不能当守卫依据。
        - 遮罩已存在时再次 close：直接 ignore 返回（Minor 7：不重复起后台
          线程、不提前关窗）。

        Minor 6（审查）：本段整体包 try/except——异步化组件（遮罩/线程/
        定时器）逆常时**回退** ``super().closeEvent(event)`` 放行关闭；否则
        事件已被 ``ignore()``，窗口永远关不掉（只能杀进程）。
        """
        if self._shutting_down:
            super().closeEvent(event)
            return
        if self._shutdown_overlay is not None:
            event.ignore()          # 停机窗口期内的重复关闭：什么都不重做
            return
        try:
            event.ignore()
            # 停掉周期轮询：会话正在后台停机，700ms 状态刷新会打到半死的
            # 会话/字符串判据上；旧实现阻塞在 GUI 线程，天然不存在这段
            # 窗口期，异步化后必须显式停（关窗后也不再需要状态显示）。
            for t in (getattr(self, "_status_timer", None),
                      getattr(self, "_cache_timer", None)):
                if t is not None:
                    t.stop()
            self._shutdown_started = True
            self._show_shutdown_overlay()
            threading.Thread(target=self._shutdown_worker,
                             daemon=True).start()
            self._shutdown_timer = QTimer(self)
            self._shutdown_timer.setInterval(100)
            self._shutdown_timer.timeout.connect(self._poll_shutdown)
            self._shutdown_timer.start()
            QTimer.singleShot(self.SHUTDOWN_HARD_TIMEOUT_MS, self._force_close)
        except Exception as e:      # noqa: BLE001
            # 逆常回退（Minor 6）：异步化跑不起来也要把窗口关掉。
            log_warning("main.close_event", f"异步化关窗失败，回退同步放行：{e}")
            self._shutting_down = True      # 幂等依据（后续 close 走放行分支）
            try:
                if self._shutdown_timer is not None:
                    self._shutdown_timer.stop()
            except Exception:               # noqa: BLE001
                pass
            super().closeEvent(event)

    def resizeEvent(self, event):
        """窗口几何变化：停机遮罩须同步跟随（Minor 9 审查整改）。

        遮罩是主窗口子部件，几何只在创建时取一次 ``centralWidget().geometry()``；
        窗口在跑时被 resize（拖边框/最大化/DPI 变化）遮罩就会错位——只盖住
        旧区域，窗口右/下侧露出未被遮罩的内容。
        """
        super().resizeEvent(event)
        ov = self._shutdown_overlay
        if ov is None:              # 未起遮罩（正常期）或构造期：无事可做
            return
        c = self.centralWidget()
        if c is not None:
            ov.setGeometry(c.geometry())

    def _show_shutdown_overlay(self):
        """半透明遮罩 + 一行进度文案（居中），盖住 centralWidget。"""
        ov = QWidget(self)
        ov.setObjectName("shutdownOverlay")
        # I4（审查）：显式声明「样式化背景」——QSS 的 background 才会被绘制，
        # 不依赖样式引擎 polish 期的隐式置位（实测 PySide6 6.11 会替被规则
        # 命中的 QWidget 自动打开该位；显式置位在正常/逆常下都成立，且**不会
        # 双绘**——实测像素逐点一致，见 close_lag_test 的绘制层断言）。
        ov.setAttribute(Qt.WA_StyledBackground, True)
        lay = QVBoxLayout(ov)
        lay.setAlignment(Qt.AlignCenter)
        self._shutdown_label = QLabel("正在保存并退出…")
        self._shutdown_label.setObjectName("shutdownLabel")
        self._shutdown_label.setAlignment(Qt.AlignCenter)
        bar = QProgressBar()
        bar.setObjectName("shutdownBar")
        bar.setRange(0, 0)          # 不确定进度
        bar.setFixedWidth(260)
        bar.setFixedHeight(6)
        lay.addWidget(self._shutdown_label, 0, Qt.AlignHCenter)
        lay.addWidget(bar, 0, Qt.AlignHCenter)
        ov.setGeometry(self.centralWidget().geometry())
        ov.raise_()
        ov.show()
        self._shutdown_overlay = ov

    def _poll_shutdown(self):
        """GUI 线程轮询（100ms）：刷新进度文案；后台完成则真正关窗。"""
        if self._shutdown_label is not None and self._shutdown_stage:
            self._shutdown_label.setText(self._shutdown_stage)
        if self._shutdown_done:
            self._force_close()

    def _force_close(self):
        """真正关窗（后台完成 / 硬超时共用）。"""
        if self._shutting_down:
            return
        self._shutting_down = True
        try:
            if self._shutdown_timer is not None:
                self._shutdown_timer.stop()
        except RuntimeError:
            pass  # 关窗竞态：Qt 底层 C++ 对象可能已销毁（wrapped C/C++ object deleted）
        self.close()
