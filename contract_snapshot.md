# 契约快照（单句柄 → 多任务改造前）· Contract Snapshot

> 快照时间：2026-09-03（改造前基线，git 工作区即快照现场）
> 用途：**回归比对基准**。下载管理模块验收（download_mgr_test.py）与 7 套旧测试
> （smoke / local_magnet / local_torrent / single_file / gui_feature / moov / qt）
> 依赖以下接口；改造中接口契约保持不变，仅实现内部换为 task 注册表。
>
> 本文件是静态快照；`download_mgr_test.py` 的契约自检段会在运行时对这些签名
> 做存在性断言（改造后跑同一脚本即得回归结论）。

## 0. 基线回归证据（2026-09-03 改造前实测）

| 测试 | 结果 | 备注 |
|------|------|------|
| smoke_test | 通过 | 解析/注入/穿越防护/bencode 防御/鉴权/Range/回环 |
| local_magnet_test | 通过 | 磁力链→元数据→900KB 缓冲 100%、磁盘字节一致 |
| local_torrent_test | 通过 9/9 | cache_dir 注入、绝对路径键、流服务联动 |
| single_file_test | 通过 12/12 | 单文件落盘不套前缀、file_disk_path 落点 |
| gui_feature_test | 通过 24/24 | 实例化/拖放/历史/文件树/磁盘路径映射 |
| moov_stream_test | 通过 A/B/C | ffprobe 实测（非 SKIP）：A 打不开复现、B/C 可探测 |
| qt_stream_open_test | 通过 A/B/C | QMediaPlayer LoadedMedia 开播实测 |

一键重跑：`python regression_run.py`（7 套全量汇总退出码）。

## 1. SessionManager（core/fetcher.py，当前 438 行）

**构造**：`SessionManager(cache_dir: str, listen_port: int = 6881)`
- 对 cache_dir 立即 `ensure_cache_dir`（高风险目录拒绝：盘符根/用户数据目录）
- 回调位：`on_metadata: Callable[[ParseResult], None]`（后台线程触发）、
  `on_error: Callable[[str], None]`、`on_file_completed: Callable[[int], None]`

**生命周期**：
- `start(proxy: dict | None = None, metadata_timeout: float | None = None)` —
  libtorrent 2.1 会话配置：enable_dht=True、enable_upnp=False、connections_limit=300、
  **active_downloads=1**；端口占用回退随机端口；显式 alert_mask。
- `apply_proxy(proxy: dict)` — 会话热更新代理设置。
- `shutdown()` — 停调度器、remove_torrent(_handle, options=1)、jojn 线程（2s）。

**解析入口**：
- `resolve(source: str)` — 磁力链或 .torrent 路径；**现语义：重置并独占**（remove 旧
  handle + gen 换代）；本地 .torrent 后台线程解析。
- `connect_peer(ip: str, port: int, wait_handle: float = 5.0)` — 等待 `_handle` 出现
  （≤ wait_handle 秒），然后 `connect_peer((ip, port))`。

**预览**：
- `start_preview(f: TorrentFile)` — 无句柄/结果时抛 `RuntimeError("请先解析种子")`；
  委托 `self.scheduler.begin(handle, f)`。
- `stop_preview()` — `self.scheduler.stop()`。
- `have_piece(piece: int) -> bool`；`piece_length() -> int | None`（无元数据 None）。
- `fetch` 侧不提供下载进度持久化；`file_progress` 经 status() 暴露。

**状态**：
- `status() -> dict | None`（无 handle 时 None）。既有字段（UI 轮询 main_window.py:308,343）：
  `state`（STATE_NAMES 映射字符串）、`num_peers`、`num_seeds`、`download_rate`、
  `total_done`、`metadata_ready`、`buffer`（0~1）、`contiguous`、`tail_ready`、
  `preview_file`、`resolving`、`elapsed`、`file_progress: list[int]`。
- `current_result -> ParseResult | None`。

**内部（改造目标，非契约）**：`_handle`（fetcher.py:57）、`_gen` 代次、
`_reset_current`（:158-170）、`_resolve_started` 全局超时看门狗（:361-370）、
alert 归属 `a.handle == self._handle`（:348/:351）。

## 2. PreviewScheduler（core/scheduler.py，当前 207 行）

- `begin(handle, file)` — 单文件锁定：`prioritize_files` 目标文件=4 其余 0、
  unset upload_mode、auto_managed、resume；尾部窗口（moov）优先预约 +
  tick() 顺序滚动。
- `request_range(start_byte, end_byte)` — 按字节区间点播，**幂等**（deadline 覆盖）。
- `seek_to_byte(byte_offset)`、`tick()`、`stop()`（撤 auto_managed 防自动续传，
  audit P1-9）、`contiguous_progress() -> int`、`tail_ready() -> bool`、
  `buffer_progress() -> float`、`active -> bool`。
- `tail_piece_window(file, piece_length) -> list[int]`（纯函数，TAIL_FIRST_EXTS=
  mp4/mov/m4v/3gp/3g2/mj2；4MB~64MB 窗口，≤128 块）。
- 常量：LOOKAHEAD_PIECES=60、TAIL_BYTES_MIN/MAX、TAIL_RATIO=0.01、TAIL_MAX_PIECES=128。

## 3. StreamServer（core/stream_server.py）

- `StreamServer(base_dir, avail_cb=None, pieces_cb=None, demand_cb=None,
  wait_timeout=20.0)`；`start()` / `shutdown()` / `port` / `url_for(rel_path)`。
- 仅 127.0.0.1；token/Host 鉴权（无 token 或伪造 Host → 403）；
  Range 206/416；未就绪区间**点播+等待**（demand_cb 触发 request_range，超时 503）；
  `_is_within(root, path)`（commonpath+normcase 前缀防护）。
- 回调契约：`pieces_cb(disk_path) -> PieceMap | None`、`demand_cb(disk_path, start, end_excl)`。

## 4. models（core/models.py）

- `TorrentFile(index, path, size, offset, start_piece, end_piece)` —
  `path` 为相对路径（含根名）；属性 `name/ext/is_video/is_image/is_pad/is_previewable`。
- `ParseResult(info_hash, name, total_size, piece_size, num_pieces, files, trackers,
  comment, created_by, source)` — `cache_dir` 由会话注入（本地 .torrent 与磁力链
  双入口一致，audit 修复 #3）；`view_files/images/videos` 过滤 .pad。
- `safe_rel_path(*segments)` — 逐级净化，防穿越（audit 修复 #6）。
- `file_disk_path(cache_dir, f)` — `cache_dir / safe_rel_path(f.path)`（**单文件不套
  额外前缀**，audit 修复 #4 契约）。
- `PieceMap` / `contiguous_bytes(pm, limit)` / `range_available(pm, start, end_excl)`；
  `human_size(n)`。

## 5. parser（core/parser.py）

- `parse_torrent_file(path) -> ParseResult`、`parse_magnet_uri(uri) -> dict`、
  `torrent_info_hash(info) -> str`（v1 SHA-1 / v2 SHA-256 前 20 字节，与 libtorrent
  一致）、`is_pure_v2(info) -> bool`、`is_torrent_path(source) -> bool`、
  `bdecode/bencode`（深度/大小/整数 DoS 上限）。

## 6. cache_guard（core/cache_guard.py）

- `CACHE_MARKER = ".magnet_viewer_cache"`；`is_risky_dir`、`ensure_cache_dir`
  （幂等 + 写标记）、`guard_ok_for_cleanup(path, require_marker=True)`。

## 7. config（core/config.py）

- `AppConfig.get/set/as_dict`、`proxy`、`recent()`（≤15 置顶去重）、`push_recent`；
  `lt_proxy_settings(proxy) -> dict`（含 tracker 重置）；`RECENT_LIMIT`。

## 8. UI 侧契约（ui/main_window.py）

- `MainWindow()`：双页签（解析/预览）、输入框 `input`、`_recent_model`、
  拖放（接受 magnet/.torrent、拒绝无关文本）、`_pieces_map(disk_path)`（键=绝对路径）、
  `_demand_range(disk_path, start, end_excl)`、`_refresh_status()`（700ms 轮询）、
  `closeEvent` 清理钩子（:358）、设置对话框（代理/超时/缓存目录/退出清理）。
- `TAB_*` 常量旁待加 `TAB_DOWNLOADS=2`（UI 改造点，非契约）。

## 9. 改造后不允许变化的契约断言清单（download_mgr_test 契约自检段）

> **状态（2026-09-07，fetcher 重构阶段 0~6 收口）**：本节 10 条契约已全部
> 由 `contract_check.py` 固化为可执行断言（156 项），并纳入回归首套；
> 七阶段重构完成，SessionManager 降级为 Facade（1607 → ~560 行），
> 注册表/锁归 core.registry，会话生命周期归 core.session，任务 CRUD 归
> core.taskops，解析编排归 core.resolver，预览桥归 core.preview，持久化归
> core.persist。全程 15 套回归无行为回归。

| # | 契约 | 依赖测试 |
|---|------|---------|
| 1 | `resolve(source)` + on_metadata 回调（含 info_hash/src/cache_dir 注入） | smoke:84-93, local_magnet:82-117, local_torrent:93-136, single_file:77-103 |
| 2 | `status()` 返回上述字段（不得减少键） | main_window 轮询、全部集成测试 |
| 3 | `start_preview(f)` / `stop_preview()` 语义不变 | local_magnet:117, moov, qt |
| 4 | `connect_peer(ip, port, wait_handle=5.0)` 等句柄语义 | local_magnet:87、live:50-53 |
| 5 | scheduler `begin(handle,f)`、`request_range` 幂等 | moov/qt 点播 |
| 6 | StreamServer pieces_cb/demand_cb 磁盘路径回调（绝对路径键） | local_torrent:123, single_file:99, gui_feature:179 |
| 7 | 单文件落盘不套前缀（file_disk_path 契约） | single_file 全量 |
| 8 | cache_guard 清理守卫语义（清理入口只走守卫） | smoke、gui_feature |
| 9 | `_is_within` 防越界（commonpath） | smoke 穿越用例 |
| 10 | 退出码约定 0/1/2；测试后不污染用户 QSettings | gui_feature 恢复段 |