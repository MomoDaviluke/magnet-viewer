"""流媒体生命周期协作者（Wave 2 拆分自 ui/main_window.py，见 ARCHITECTURE.md §2.3）。

为什么单独成模块：StreamServer 实例的构造/启停、磁盘路径→文件映射回调
（HTTP 线程入口）、开播门控（头部数据 + 尾部 moov 入口就绪）、播放失败
指数退避重试链、可归因超时文案——这是一条完整的「流媒体生命周期」内聚
链，与视图组装无涉，只经 SessionManager facade（create_stream_server /
piece_map_for_path / demand_for_path / scheduler）与宿主窗口的控件引用
交互。留在 MainWindow 里是「上帝对象」症状的核心一簇（实测约 270 行）。

宿主耦合约定（行为零变化）：
- 播放会话状态（pending_video / pending_video_since / stream_url /
  stream_attempts）由本类持有，MainWindow 侧不再存副本；白盒测试
  （gui_feature / close_lag / wait_timeout）经 ``w.stream_host.*`` 访问。
- ``StreamCallbacks`` 仍在构造期**捕获** ``win._path_to_file`` /
  ``win._map_miss_warned`` 两个容器对象——与拆分前逐字一致（含「重绑定
  _path_to_file 后回调仍持旧 dict」的既有语义，本重构不修行为）。
- 开播门控的放行判据与文案分支解耦纪律（C3）原样保留：文案不驱动门控。
"""
from __future__ import annotations

import os
import time

from PySide6.QtCore import QTimer

# §2.2 UI 依赖规则：只准 core.models / core.config / core.fetcher（facade）/
# core.logutil。流服务构造经 SessionManager.create_stream_server 工厂，
# 不再越层直连 core.stream_server。
from core.logutil import log_warning
from core.models import (ParseResult, PieceMap, TorrentFile, disk_root)
from ui.preview_player import WAIT_DATA, WAIT_INDEX


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


def gate_wait_timeout_reason(st: dict, file, elapsed_s: float,
                             proxy_hint: str = "") -> str:
    """开播门控等待超时的**可归因**文案（纯函数 → 单测可断言，无需起 GUI）。

    为什么需要（C3，2026-09-18）：旧门控分支**没有任何计时器** —— 数据永不到达时
    用户会无限看到「等待数据/索引块就绪」，既不知道等的是什么、也不知道卡在哪。
    这里只陈述**事实数字**、不下武断结论：同一组数字在不同资源上含义不同
    （冷门资源 peer 少是正常的），所以给的是「去看什么」而不是「就是资源没种」。
    """
    contig = int(st.get("contiguous", 0) or 0)
    target = min(1024 * 1024, getattr(file, "size", 0) or 0)
    idx_ok = "已就绪" if st.get("tail_entry_ready", True) else "未就绪"
    peers = int(st.get("num_peers", 0) or 0)
    seeds = int(st.get("num_seeds", 0) or 0)
    rate_kb = float(st.get("download_rate", 0) or 0) / 1024.0
    state = st.get("state", "?")
    return (
        f"等待开播数据超时（已等 {elapsed_s:.0f} 秒）：头部连续数据 "
        f"{contig / 1024 / 1024:.2f}MB / 需 {target / 1024 / 1024:.2f}MB，"
        f"索引块{idx_ok}；当前 peer {peers}（种子 {seeds}）、"
        f"速率 {rate_kb:.1f}KB/s、状态 {state}。"
        f"若 peer 与速率长期为 0，多半是资源无做种或本机网络受限；"
        f"可再次点击播放重试，或换一个资源。{proxy_hint}"
    )


def stream_failure_message(note: str = "") -> str:
    """播放器多次打开失败时的文案（纯函数；``note`` = 流服务最近一次应答摘要）。

    C4 的要点就是带上服务端应答：否则用户与日志里只剩播放器的原始报错
    （FFmpeg 的 `moov atom not found` / ExoPlayer 的 5xx 文本），既不知道
    服务端回了什么码，也不知道它等了多久、就绪了多少，排查只能靠猜。
    """
    base = "播放器多次打开失败：数据仍未就绪，已转为后台慢速重试…"
    return f"{base}（{note}）" if note else base


class StreamHost:
    """流媒体生命周期协作者：流服务实例 + 开播门控 + 失败重试链（宿主晚绑定）。"""

    # 退避参数（2026-09-17 等待超时专项）。旧实现是 1500ms × 次数 的**线性**
    # 退避，并在第 4 次失败后直接 `return`——弹一句「播放器多次打开失败」就
    # 不再重试。慢链路上数据常常只差几秒就位（尾部 moov 入口 + 头部前缀），
    # 用户看到的却是终态错误。改为**指数**退避 + 不硬性放弃：越过
    # STREAM_RETRY_SOFT_LIMIT 只提示一次，重试链继续以封顶间隔跑（数据真到位
    # 就立刻开播，数据永远不来也不会比旧行为更差——旧行为同样什么都没发生，
    # 只是不再更新提示）。
    STREAM_RETRY_BASE_MS = 800
    STREAM_RETRY_MAX_MS = 12000
    STREAM_RETRY_SOFT_LIMIT = 3

    # 开播门控的等待上限（毫秒，C3）。旧实现没有计时器：数据永不到达时用户
    # 会无限看到「等待数据」。取 60s —— 给慢链路足够时间，又能在「确实等不出来」
    # 时给出结论。注意与 `metadata_timeout`（默认 90s）区分：那是**元数据阶段**
    # 的上限，本项是元数据到手**之后**等首批数据/索引块，是前后两个阶段。
    GATE_WAIT_TIMEOUT_MS = 60_000

    def __init__(self, win):
        self._win = win
        # ---- 播放会话状态（原 MainWindow 成员，语义逐字保留）----
        self.pending_video: tuple[TorrentFile, str] | None = None  # 等待首批数据
        # 起表时刻（C3）：门控等待超时判据用它计时。只在**赋值** pending_video
        # 时刷新，读取处只在 pending_video is not None 时才会用 —— 因此不需要
        # 在每个清空点同步清零（陈旧值不会参与判断）。
        self.pending_video_since: float = 0.0
        self.stream_url: str | None = None   # 当前视频流地址（失败重试用）
        self.stream_attempts = 0
        # 流服务回调集（HTTP 线程入口独立成类，P2-3）：与主窗口共享
        # path_to_file / map_miss_warned 两个容器（构造期捕获对象引用，
        # 与拆分前一致），preview_file 用 getter 取
        self.callbacks = StreamCallbacks(
            win.session, win._path_to_file, win._map_miss_warned,
            preview_file_getter=lambda: win._preview_file)
        # 流服务多根：download_dir 配置在缓存目录之外时，下载任务文件的
        # 分块级可用性判定/按需补拉仍须可服务（url_for 携带绝对落盘路径）。
        # 构造经 facade（§2.2：UI 不直连 core.stream_server）。
        self.server = win.session.create_stream_server(
            pieces_cb=self.callbacks.pieces_map,
            demand_cb=self.callbacks.demand_range)
        self.server.start()

    # ---------- 开播入口与状态复位 ----------

    def rel(self, f: TorrentFile) -> str:
        """文件的服务相对路径（宿主 result/cache_dir 晚绑定）。"""
        win = self._win
        return stream_rel(win.result, win.cache_dir, f)

    def begin_video(self, f: TorrentFile):
        """视频预览的流侧动作：算 URL → 起门控等待 → 播放页置「等待」态。

        不立即播放：等头部连续数据 + 尾部索引块（moov，MP4/MOV）都就绪后
        再 set_stream（门控放行在 tick() 里做），避免播放器读到稀疏零数据
        或探测不到 moov。
        """
        win = self._win
        self.stream_url = self.server.url_for(self.rel(f))
        self.pending_video = (f, self.stream_url)
        self.pending_video_since = time.monotonic()   # 门控计时起表（C3）
        win.preview.video.set_waiting(f.name, f.size)

    def reset_state(self):
        """复位流侧状态（停止预览/重新解析/重新选文件时调用）。"""
        self.pending_video = None
        self.stream_url = None
        self.stream_attempts = 0

    # ---------- 播放位置回调转发 ----------

    def seek_to(self, byte_offset: int):
        """播放位置跳转 → 调度器从对应分块重新开始顺序下载。"""
        win = self._win
        if win._preview_file is not None and win._preview_file.is_video:
            win.session.scheduler.seek_to_byte(byte_offset)

    def scrub_request(self, byte_offset: int):
        """拖动中预取：目标区间先点播，松手前数据已在路上。

        `request_range` 的上限与顺序窗口**同源**（`window_pieces` 的 16MB 字节
        预算，见 C1）：4MB 块 → 4 块（16MB）、16KB 块 → 60 块（960KB），
        对本预取（目标 4MB）都够开播解码；
        误预取（拖过又拖回）由下次 seek 的 clear_piece_deadlines 回收。
        节流由 `preview_player.SCRUB_INTERVAL_MS`（300ms）负责，不会每次
        拖动都打一次点播。
        """
        win = self._win
        if win._preview_file is not None and win._preview_file.is_video:
            win.session.scheduler.request_range(
                byte_offset, byte_offset + 4 * 1024 * 1024)

    # ---------- 开播门控（状态轮询驱动） ----------

    def tick(self, st: dict | None):
        """视频开播条件：头部连续数据 + 尾部**入口**就绪（plan/07 阶段 2）。

        入口 = 文件最后 min(2MB, size) 覆盖的块（moov 所在），**不是**整尾窗：
        4.1GB / 4MB 块真机上整尾窗 44MB 在 2.4MB/s 下要 ≈18s 才齐，旧的
        「整尾窗就绪」门控因此永不满足（缓冲恒 0.0%、30s 不见画面）；
        入口只需最末 1 块。整尾窗仍在后台由 scheduler.tick() 继续补拉，
        万一 moov 大于入口，播放器打开失败会走 on_stream_failed 退避重试。
        """
        win = self._win
        if self.pending_video is None or st is None:
            return
        f, url = self.pending_video
        contig = st.get("contiguous", 0)
        data_ok = contig >= min(1024 * 1024, f.size)
        index_ok = bool(st.get("tail_entry_ready", True))
        if data_ok and index_ok:
            self.pending_video = None
            win.preview.video.set_stream(url, f.name, f.size)
        else:
            # C3（2026-09-18）：门控等待**必须有上界**。旧实现在这里没有
            # 任何计时器 —— 数据永不到达时用户会无限看到「等待数据」，
            # 既不知道等的是什么、也不知道卡在哪。到点给可归因结论并停止
            # 空转（下载仍在后台继续；用户可再点播放重试，重新计时）。
            # 时间戳兜底：**直接赋值 pending_video 的路径**（测试替身、
            # 以及将来可能新增的入口）不会设 pending_video_since，
            # 那样 elapsed 会变成「从开机算起」的天文数字、超时立刻触发。
            # 把「无时间戳」按「此刻刚开始等」处理，语义正确且对调用方宽容。
            if self.pending_video_since <= 0:
                self.pending_video_since = time.monotonic()
            elapsed_s = time.monotonic() - self.pending_video_since
            if elapsed_s * 1000 >= self.GATE_WAIT_TIMEOUT_MS:
                self.on_gate_timeout(f, st, elapsed_s)
            else:
                # 阶段 3：等数据 / 等索引块分句提示。**仅文案分支**——放行
                # 判据是上面的布尔量，文案不反过来驱动门控（本轮询方法也
                # 不读文案）。
                win.preview.video.set_waiting_stage(
                    WAIT_DATA if not data_ok else WAIT_INDEX)

    # ---------- 等待与失败的可归因（C3 / C4） ----------

    def on_gate_timeout(self, f, st: dict, elapsed_s: float):
        """门控等待超时：给可归因结论、停止空转（C3）。

        不弹模态框：播放页已有醒目的错误标题区（`show_error`），弹窗会打断
        用户正在做的其他操作；同时写状态栏，保证「不看播放页也能看到」。
        """
        win = self._win
        msg = gate_wait_timeout_reason(st, f, elapsed_s, self.proxy_hint())
        self.pending_video = None         # 停止空转（用户可再点播放重试）
        log_warning("main.gate_timeout", msg)
        win.preview.video.show_error(msg)
        win.status_panel.set_state(msg)

    def proxy_hint(self) -> str:
        """已配置代理时的提示（facade 委托 proxycheck.hint_text 纯文案，
        **不联网、不阻塞 GUI**）。"""
        win = self._win
        try:
            return win.session.proxy_hint(win.cfg.proxy())
        except Exception as e:
            log_warning("main.proxy_hint", f"{e}")
            return ""

    def last_stream_note(self) -> str:
        """流服务最近一次应答摘要（C4）。取不到时返回空串，绝不抛。"""
        try:
            return self.server.last_response_note()
        except Exception as e:
            log_warning("main.stream_note", f"{e}")
            return ""

    # ---------- 播放失败重试 ----------

    def on_stream_failed(self):
        """播放器打开媒体失败：多半是索引块/数据仍在下载 → 催数据 + 退避重试。

        「催数据」（``scheduler.rearm``）是有意加的：旧实现只重排一个定时器，
        完全被动等 ``tick()``（700ms 一拍，且带「窗口仍充裕即返回」的防抖）
        把数据补上。播放器**已经**失败，说明按现有预约没能及时满足它，
        此时该做的是立刻把尾部索引窗与播放位置窗口重刷为最高优先级。
        """
        win = self._win
        if win._preview_file is None or self.pending_video is not None:
            return  # 已停止或尚未开播
        if self.stream_url is None:
            return
        self.stream_attempts += 1
        n = self.stream_attempts
        self.rearm_download()
        if n == self.STREAM_RETRY_SOFT_LIMIT:
            # C4：带上流服务最近一次应答（码 + 区间 + 就绪量 + 等待耗时）——
            # 否则用户与日志里只剩播放器的原始报错，服务端侧一无所知。
            msg = stream_failure_message(self.last_stream_note())
            win.preview.video.show_error(msg)
            win.status_panel.set_state(msg)
        delay = min(self.STREAM_RETRY_MAX_MS,
                    self.STREAM_RETRY_BASE_MS * (2 ** min(6, n - 1)))
        # 重试链排到宿主**带守卫**的 _retry_stream 槽（拆分前 QTimer 的目标
        # 就是 MainWindow._retry_stream）：停机窗口内到点的续排必须首行早退，
        # 绝不能直接排本类未守卫的方法——否则关窗竞态里会打半死会话。
        QTimer.singleShot(delay, win._retry_stream)

    def rearm_download(self):
        """催数据：立即重刷尾部索引窗 + 播放位置窗口（幂等、有界）。

        失败只记日志——重试链本身不因催数据失败而中断。
        """
        try:
            self._win.session.scheduler.rearm()
        except Exception as e:
            log_warning("main.stream_rearm", f"{e}")

    def retry_stream(self):
        win = self._win
        if win._preview_file is None or self.stream_url is None:
            return
        if self.pending_video is not None:
            return  # 用户已重新选文件/重新开播：放弃旧重试（竞态守卫）
        st = win.session.status()
        if st is None:
            QTimer.singleShot(1500, win._retry_stream)   # 续排走宿主守卫槽
            return
        if (not st.get("tail_entry_ready", True)) or st.get("contiguous", 0) < 1:
            # 数据仍未就绪（尾部入口/头部前缀）：静默等待，不算失败次数，
            # 避免反复弹错误。判据与开播门控同源（入口而非整尾窗）——
            # 否则门控已放行、这里却继续等整尾窗到位，出现「能开却不重试」。
            QTimer.singleShot(1500, win._retry_stream)   # 续排走宿主守卫槽
            return
        # 带上出错前的位置续播：setSource 会从头开始，不带位置的话
        # 拖动进度条后因数据未就绪报错时，用户会看到「突然从头重播」
        win.preview.video.set_stream(self.stream_url,
                                     win._preview_file.name,
                                     win._preview_file.size,
                                     resume_ms=win.preview.video.take_resume_ms())
        self.stream_attempts = 0   # 开播成功：重试计数归零，下次失败可重新重试
