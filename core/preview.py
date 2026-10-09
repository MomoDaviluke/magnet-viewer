"""预览桥与状态快照（fetcher 重构阶段 5 抽出）。

职责边界：
    ``PreviewCore`` = 播放器/画廊/状态栏背后的这条链——预览启停
    （scheduler.begin/stop）、「磁盘路径 → (记录, 文件)」反查、分块可用性
    映射（PieceMap）、按字节区间的任务级点播补拉（demand）、当前任务的
    分块查询与状态快照（status）。

核心安全语义（历史上多个 P0/P1 缺陷的沉淀，一条都不许改）：
    - ``piece_map_for_path`` / ``demand_for_path`` 返回 **None/False 表示
      「无法判定」**——流服务调用方绝不可降级为「整文件可用」，那会把未
      下载的稀疏零数据喂给播放器（partial file / Invalid data 缺陷根源）；
    - 路径反查用 normpath 归一 + 前缀匹配，Windows 反斜杠与正斜杠混用
      均须命中（目录隔离改造后键为绝对路径）；
    - ``status()`` 里 contiguous 与 buffer 同源一次扫描复用；resolving/
      elapsed 走 registry 一致快照（R-1 家族）。

锁纪律：with reg.lock 段内只调 *_locked / 裸字典读；句柄绑定调用
（status()/have_piece/torrent_file）一律出锁——沿用阶段 4 R-3 的原则，
UI 轮询与流服务回调不按住任务锁。
"""
from __future__ import annotations

import os
import threading
import time

import libtorrent as lt

from .logutil import log_warning
from .models import ParseResult, PieceMap, TorrentFile
from .pieceview import PieceView
from .registry import TaskRecord, TaskRegistry, hash_key
from .scheduler import DEADLINE_STEP_MS, LOOKAHEAD_PIECES

STATE_NAMES = {
    getattr(lt.torrent_status, k, None): k.replace("_", " ")
    for k in ("checking_files", "downloading_metadata", "downloading",
              "finished", "seeding", "allocating", "checking_resume_data")
    if getattr(lt.torrent_status, k, None) is not None
}


class PreviewCore:
    """预览与状态（挂在 SessionManager 上，经其薄委托对外）。"""

    def __init__(self, reg: TaskRegistry,
                 scheduler_get, ses_get=None):
        self.reg = reg
        self._scheduler_get = scheduler_get
        # 落盘目录 → 记录列表 的解析索引（见 find_record_for_path）。
        # 缓存条目 = (registry.struct_version, {normpath(save_path): [rec,...]})；
        # 版本不符即整体重建（一次 dict 遍历），查询退化为 O(路径深度)。
        self._path_index: tuple = (-1, {})
        # 增量可用性视图缓存（plan/09）：{info_hash: PieceView}。
        # 同一 info_hash 的句柄被替换时按句柄身份自动重建（防串台）。
        # 审计 2026-09-29（P2）：流服务探测线程（_view_for 写）与目录索引
        # 重建线程（_dir_index 迭代+回收写）会并发访问本字典——CPython 单个
        # dict 操作原子，但「迭代中并发新增键」仍抛 RuntimeError（偶发 503）。
        # 本锁是叶子锁：持有时绝不调用 reg.lock / 调度器，无锁序风险。
        self._views_lock = threading.Lock()
        self._views: dict = {}

    # ---------- 可用性视图 ----------

    def _view_for(self, handle, npieces: int, piece_length: int) -> PieceView:
        """按句柄取/建增量可用性视图。

        视图替代「每次判定都物化全量位图」，使门控 / 流服务探测的成本与种子
        规模**无关**（论证与实测见 ``core/pieceview.py`` 与 plan/09）。
        键取 ``hash_key``（与注册表同口径），并用句柄身份校验——
        同 ih 被替换句柄时必须重建，否则会用别的种子的位图判定可读区间。
        """
        key = hash_key(handle) if handle is not None else ""
        with self._views_lock:
            v = self._views.get(key)
            if v is None or not v.matches(handle):
                v = PieceView(handle, piece_length, npieces)
                self._views[key] = v
        return v

    # ---------- 预览 ----------

    def start_preview(self, f: TorrentFile):
        """开始预览某个文件（边下边播 / 图片下载）。"""
        with self.reg.lock:
            handle, result = self.reg.handle, self.reg.result
        if handle is None or result is None:
            raise RuntimeError("请先解析种子")
        sched = self._scheduler_get()
        # 注入增量视图（注入后门控/tick 不再物化位图）。调度器是测试替身
        # （没有 attach_view）时跳过——不必为一个不会用的视图白白建对象。
        attach = getattr(sched, "attach_view", None)
        if callable(attach):
            attach(self._view_for(handle, int(result.num_pieces or 0),
                                  int(result.piece_size or 0)))
        sched.begin(handle, f)

    def stop_preview(self, release_only: bool = False):
        """停预览。release_only=True（convert 档）：只清 deadline/锚点，
        不 pause、不清文件优先级——是否转正由宿主（SessionManager）锁外决策。"""
        self._scheduler_get().stop(release_only=release_only)

    # ---------- 磁盘路径反查（流服务回调入口） ----------

    def _dir_index(self) -> dict:
        """落盘目录索引：``{normpath(save_path): [TaskRecord, ...]}``（按入表序）。

        为什么要建索引（2026-09-17 优化，探针 D1-b/D1-d 实测）：
        旧实现每次反查都 ``with reg.lock`` 复制**全部**记录，再逐记录做前缀
        比较、逐文件做 ``f.path == rel`` 全量字符串比较——O(任务数 × 文件数)。
        这条路径有两个热入口，量级都不小：

        - 流服务 ``_wait_ready`` 每 50ms 轮询一次可用性（``pieces_cb``），
          16 并发下即 320 次/秒；实测单次 253µs（2 任务 × 2000 文件），
          **8.1% 单核**，且每次都抓一次任务锁——与 GUI 线程、告警线程争锁；
        - UI 700ms 轮询链里的 ``demand`` 与终端反查同源。

        索引按 ``reg.struct_version`` 失效（入表/清表即重建），查询时仍**实时**
        读 ``rec.handle`` / ``rec.result``，所以「元数据后到」「句柄替换」都不
        会产生陈旧结果；记录集合未变时连任务锁都不需要抓（只读一个 int）。
        """
        ver = self.reg.struct_version          # 裸读 int（GIL 下原子）
        cached_ver, cached = self._path_index
        if cached_ver == ver:
            return cached
        with self.reg.lock:
            recs = list(self.reg.torrents.values())
            live_keys = set(self.reg.torrents)
        # 顺带回收已移除任务的视图（视图持有句柄引用与一份基线位图，
        # 不回收会让长期运行的内存随「看过多少种子」单调增长）。
        # 迭代+pop 全程持 _views_lock（审计 2026-09-29）：与 _view_for 的
        # 新增键写入互斥，避免「dictionary changed size during iteration」。
        with self._views_lock:
            for k in [k for k in self._views if k not in live_keys]:
                self._views.pop(k, None)
        idx: dict[str, list] = {}
        for rec in recs:
            sp = getattr(rec, "save_path", "") or ""
            if not sp:
                continue
            idx.setdefault(os.path.normpath(sp), []).append(rec)
        self._path_index = (ver, idx)
        return idx

    def find_record_for_path(self, disk_path: str):
        """磁盘路径 -> (TaskRecord, TorrentFile) 反查：匹配任务落盘目录前缀。

        供流服务回调按实际磁盘路径定位任务句柄（下载中任务文件的分块
        可用性判定），返回 None 表示未知/无句柄（调用方不得降级为全量）。

        实现：沿路径**逐级向上**查落盘目录索引（O(路径深度)），而非遍历全部
        记录（O(任务数)）。语义与旧实现一致：目录命中后再按 ``rel`` 精确匹配
        文件，匹配不到继续向外层目录找；同层多个记录按入表序取首个可用者。
        """
        np_ = os.path.normpath(disk_path)
        idx = self._dir_index()
        cur = np_
        while True:
            for rec in idx.get(cur, ()):
                if not rec.save_path or rec.handle is None \
                        or rec.result is None:
                    continue
                base = os.path.normpath(rec.save_path)
                rel = os.path.relpath(np_, base).replace("\\", "/")
                for f in rec.result.files:
                    if f.path == rel:
                        return rec, f
            parent = os.path.dirname(cur)
            if not parent or parent == cur:
                return None
            cur = parent

    def piece_map_for_path(self, disk_path: str) -> PieceMap | None:
        """按磁盘路径提供分块可用性映射（下载任务文件；未知返回 None）。

        None 表示「无法判定可用性」——调用方（流服务 pieces_cb）绝不
        能把 None 当作整文件可用：那会把未下载的稀疏零数据喂给播放器
        （历史「partial file / Invalid data」缺陷的直接根源）。
        """
        hit = self.find_record_for_path(disk_path)
        if hit is None:
            return None
        rec, f = hit
        handle = rec.handle
        try:
            # 单次锁外句柄调用（P1-8：异常/缺失 → None，绝不整文件可用）
            ti = handle.torrent_file()
            if ti is None:
                return None
            pl = ti.piece_length()
            try:
                npieces = int(ti.num_pieces())
            except Exception:
                # 宽泛保留（纪律例外登记）：此处只兼容**测试替身**
                # （preview_test.FakeTI 刻意只实现 piece_length()，缺 num_pieces
                # 抛 AttributeError）；生产 lt.torrent_info.num_pieces() 对有效
                # ti 不会抛，异常类型不可枚举的意义仅在于替身面。块数只作
                # 越界上限用，取「文件块范围上界」足够，绝不因此放弃整条路径
                npieces = f.end_piece + 1
        except (AttributeError, RuntimeError) as e:
            # RuntimeError：句柄失效/无元数据时 lt 实测抛（torrent_file()）；
            # AttributeError：替身缺成员
            log_warning("fetcher.piece_map.piece_length", f"{e}")
            return None
        if pl <= 0:
            return None
        # plan/09：挂上增量视图 → 流服务的区间判定不再物化全量位图，
        # 成本与种子规模无关（``models.*_of`` 系列据此走快路径）。
        view = self._view_for(handle, int(npieces or 0), int(pl))
        return PieceMap(piece_length=pl, offset=f.offset,
                        start_piece=f.start_piece, end_piece=f.end_piece,
                        size=f.size, have=view.have, view=view)

    def demand_for_path(self, disk_path: str, start_byte: int,
                        end_excl: int) -> bool:
        """按磁盘路径触发任务级按需补拉（播放器要哪段就先下哪段）。

        与 scheduler.request_range 语义一致，但作用于任意下载任务句柄
        （流服务 demand_cb 对非预览文件的请求也生效）。

        单次点播同样**必须有 LOOKAHEAD_PIECES 上限**（对齐
        scheduler.py:156 及其注释的论证）：FFmpeg 拖动发的是 `bytes=X-`
        （X 到文件尾），不截断会把剩余整文件全刷成 ASAP——带宽摊到几百
        MB 上反而拖慢点播目标本身。全量落盘由 file-priority 负责，
        点播只是临时插队，不需要也不应该包揽整文件。

        区间内 deadline **递增**（第 i 块 = ``i * DEADLINE_STEP_MS``，
        plan/07 阶段 2）：插队语义不变（首块仍 0ms 立即），但把区间内的
        顺序表达出来——旧实现全 0 让区间内几十块同级紧急，libtorrent 散抓，
        区间头部反而迟到（阶段 1 在 ``scheduler.request_range`` 已修同款，
        这条是任务级 demand 的漏网对称路径）。
        """
        hit = self.find_record_for_path(disk_path)
        if hit is None:
            return False
        rec, f = hit
        try:
            pl = rec.handle.torrent_file().piece_length()
            if pl <= 0:
                return False
            first = max(f.start_piece,
                        f.start_piece + max(0, start_byte) // pl)
            last = min(f.end_piece,
                       f.start_piece + max(0, end_excl - 1) // pl)
            last = min(last, first + LOOKAHEAD_PIECES - 1)
            # deadline 改**递增**（第 i 块 = i * DEADLINE_STEP_MS，plan/07
            # 阶段 2）：与 scheduler.request_range 同源——旧实现全 0 等于
            # 不带任何排序信息，FFmpeg 拖动发的 `bytes=X-` 会把区间内所有块
            # 置成同级紧急，libtorrent 在几十个同级块里散抓，区间**头部**反而
            # 迟到（阶段 1 在 scheduler 侧已修同病根，此处是漏网的同款）。
            for p in range(first, last + 1):
                rec.handle.set_piece_deadline(p, (p - first) * DEADLINE_STEP_MS)
            return True
        except (AttributeError, RuntimeError) as e:
            # RuntimeError：句柄失效时 lt 绑定实测抛（torrent_file/set_piece_deadline）；
            # AttributeError：替身缺成员
            log_warning("fetcher.demand_for_path", f"{disk_path}: {e}")
            return False

    # ---------- 当前任务查询 ----------

    def task_result(self, task_id: str) -> ParseResult | None:
        """下载任务的解析结果（供 UI 载入文件树/预览）。"""
        key = str(task_id or "")
        with self.reg.lock:
            rec = self.reg.torrents.get(key)
            return rec.result if rec is not None else None

    def have_piece(self, piece: int) -> bool:
        """指定种子分块是否已完整落盘（供流服务/调度器判定可读区间）。"""
        with self.reg.lock:
            handle = self.reg.handle
        if handle is None:
            return False
        try:
            return bool(handle.have_piece(int(piece)))
        except (AttributeError, RuntimeError) as e:
            # RuntimeError：句柄失效时 lt 绑定实测抛；AttributeError：替身缺 have_piece
            log_warning("fetcher.have_piece", f"分块查询失败 piece={piece}: {e}")
            return False

    def piece_length(self) -> int | None:
        """当前种子分块大小；元数据未就绪时返回 None。"""
        with self.reg.lock:
            handle = self.reg.handle
        if handle is None:
            return None
        try:
            if not handle.has_metadata():
                return None
            return handle.torrent_file().piece_length()
        except (AttributeError, RuntimeError) as e:
            # RuntimeError：句柄失效时 lt 绑定实测抛（has_metadata/torrent_file）；
            # AttributeError：替身缺成员
            log_warning("fetcher.piece_length", f"{e}")
            return None

    def current_result(self) -> ParseResult | None:
        """当前任务解析结果（锁读）。"""
        with self.reg.lock:
            return self.reg.result

    def status(self) -> dict | None:
        """线程安全的状态快照，供 UI 定时轮询。"""
        reg = self.reg
        with reg.lock:
            handle = reg.handle
        if handle is None:
            return None
        try:
            # plan/09：`status(0)` 用 status_flags 关掉 pieces 位图——本方法
            # 只需要 state/peers/rate，位图由增量视图提供。实测位图拷贝
            # ≈1.35ns/块（40 万块 0.54ms），关掉后恒定 ~6µs（与块数无关）。
            # 测试替身可能不接受 flags → 退回无参调用，行为与基线一致。
            try:
                s = handle.status(0)
            except TypeError:
                s = handle.status()
        except (AttributeError, RuntimeError) as e:
            # RuntimeError：句柄失效时 lt 实测抛（status）；AttributeError：替身缺 status
            log_warning("fetcher.status.handle", f"{e}")
            return None
        st = {
            "state": STATE_NAMES.get(s.state, str(s.state)),
            "num_peers": s.num_peers,
            "num_seeds": s.num_seeds,
            "download_rate": s.download_payload_rate,
            "total_done": s.total_done,
            "metadata_ready": handle.has_metadata(),
        }
        sched = self._scheduler_get()
        # 门控三判据走**单快照求值**：旧实现 contiguous / tail_ready /
        # tail_entry_ready 各自 status() 一次，加上本方法开头的一次，mp4 预览
        # 路径上一轮 UI 轮询要付 4 次 torrent_status 拷贝 + 4 次位图物化
        # （O(块数)，实测 18.75ns/块；40 万块 = 31.5ms，全落在 GUI 线程，
        # 按 700ms 轮询计 4.5%）。第一次优化把三处折叠为一次，第二次（plan/09）
        # 连那一次也去掉——见下面的最终形态说明。
        # plan/09 最终形态：一轮 UI 轮询**零位图物化**。
        # - 状态快照用 `status(0)`（上面）—— flags 关掉 pieces，恒定成本；
        # - 门控三判据由 gate_snapshot 单次求值，并**复用增量视图的 have**
        #   （同一句柄时），因此既不物化位图也不做逐块全扫；
        # - 句柄不同（后台任务与预览目标不一致）时传 None，由 gate_snapshot
        #   自行取快照——宁可多一次拷贝，绝不错用别的种子位图。
        # FakeSched 等测试替身没有 gate_snapshot / piece_view，走回退分支，
        # 行为与基线逐字一致。
        snap = getattr(sched, "gate_snapshot", None)
        if callable(snap):
            view = getattr(sched, "piece_view", None)
            have = (view.have if view is not None
                    and getattr(sched, "handle", None) is handle else None)
            m = snap(have)
            contig = int(m.get("contiguous", 0) or 0)
            tail_ok = bool(m.get("tail_ready", True))
            entry_ok = bool(m.get("tail_entry_ready", True))
        else:
            contig = sched.contiguous_progress() if sched.active else 0
            tail_ok = sched.tail_ready() if sched.active else True
            entry_ok = sched.tail_entry_ready() if sched.active else True
        pf = sched.file
        st["buffer"] = (min(1.0, contig / pf.size)
                        if sched.active and pf is not None and pf.size > 0
                        else 0.0)
        st["contiguous"] = contig
        st["tail_ready"] = tail_ok
        # 开播门控判据（plan/07 阶段 2）：只看尾部**入口**（文件最后
        # min(2MB, size) 覆盖块），不等整尾窗；tail_ready 保留但不再驱动门控。
        st["tail_entry_ready"] = entry_ok
        st["preview_file"] = pf
        # R-1 家族（D4）：resolving/elapsed 成对读走 registry 一致快照，
        # 不再锁外分两次读别名（撕裂窗口）。
        st["resolving"], st["elapsed"] = reg.resolving_snapshot()
        try:
            st["file_progress"] = list(handle.file_progress())
        except (AttributeError, RuntimeError) as e:
            # RuntimeError：句柄失效时 lt 实测抛（file_progress）；AttributeError：替身缺方法
            log_warning("fetcher.status.file_progress", f"{e}")
            st["file_progress"] = []
        return st
