"""对外契约自检 —— 重构 `core/fetcher.py` 期间的安全网。

背景
----
`core/fetcher.py` 已 1600+ 行，计划按职责拆为 session / resolver / registry /
taskops / persist / preview 六个模块，`SessionManager` 降级为 Facade。
拆分必须做到**对外行为完全不变**，而"不变"需要机器可判定的标准，
不能靠人眼 review。

本脚本用 `inspect.signature` 把当前对外接口固化为签名指纹
（参数名 + 是否可选，不含类型注解——注解调整不该误报），
任何改名 / 改序 / 新增必选参数 / 删除接口都会被立刻抓到。

契约来源
--------
1. `contract_snapshot.md` 第 9 节「改造后不允许变化的契约断言清单」；
2. 对 `ui/` 与全部测试脚本的实际调用面扫描（含 UI 直访的私有成员，
   那 4 处是历史遗留的破封装，重构期间按决策保留兼容）；

用法
----
    .\\.venv\\Scripts\\python.exe contract_check.py

退出码：`0` 契约一致 · `1` 契约被破坏 · `2` 环境/依赖缺失。
不启 libtorrent 会话、不联网，秒级完成，可在每个重构阶段随时一键自检。
"""
from __future__ import annotations

import inspect
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    from core import (cache_guard, cache_quota, models, parser, persist,
                      preview, registry, resolver, scheduler, session,
                      states, stream_server, taskops)
    from core import fetcher as fetcher_mod
    from core.fetcher import SessionManager
except Exception as e:          # 依赖缺失：显式 SKIP，绝不假装通过
    print(f"依赖缺失，无法执行契约自检：{e}")
    sys.exit(2)

OK: list[str] = []
FAIL: list[str] = []


def check(cond: bool, msg: str) -> None:
    (OK if cond else FAIL).append(msg)
    print(("  [OK] " if cond else "  [FAIL] ") + msg)


def fp(fn) -> list | None:
    """签名指纹：[(参数名, 是否有默认值)]，排除 self。

    只取参数名与可选性，不取类型注解——注解是给人看的，调整它不应
    触发契约失败；但参数数量、顺序、命名变化必须被抓住。
    """
    if fn is None or not callable(fn):
        return None
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        return None
    return [(p.name, p.default is not inspect.Parameter.empty)
            for p in inspect.signature(fn).parameters.values()
            if p.name != "self"]


def sig_check(label: str, owner, table: dict) -> None:
    """逐项比对签名指纹表。"""
    for name, expect in table.items():
        fn = getattr(owner, name, None)
        got = fp(fn)
        check(got == expect,
              f"{label}.{name}{'' if got == expect else f' 签名变化：期望 {expect}，实际 {got}'}")


def main() -> int:
    print("=== 对外契约自检（fetcher 重构安全网）===\n")

    # ------------------------------------------------ [1] SessionManager 公开方法
    print("[1] SessionManager 公开方法签名（23 项，基线 2026-09-07 + 阶段 6 R-4）")
    SESSION_SIG = {
        # 生命周期
        "start": [("proxy", True), ("metadata_timeout", True)],
        "shutdown": [],
        "apply_proxy": [("proxy", False)],
        "apply_rate_limit": [("kbps", False)],
        "apply_metadata_timeout": [("seconds", False)],
        "protected_dirs": [],
        # 解析入口（契约 #1 / #4）
        "resolve": [("source", False)],
        "connect_peer": [("ip", False), ("port", False),
                         ("wait_handle", True), ("task_id", True)],
        # 任务操作（契约 #1）
        "add_task": [("source", False), ("save_subdir", True),
                     ("priority", True), ("seed", True)],
        "set_priority": [("task_id", False), ("priority", False)],
        "pause_task": [("task_id", False)],
        "resume_task": [("task_id", False)],
        "remove_task": [("task_id", False), ("delete_files", True)],
        "focus_task": [("task_id", False)],
        "tasks": [],
        "task_result": [("task_id", False)],
        # 预览（契约 #3）
        "start_preview": [("f", False)],
        "stop_preview": [],
        "have_piece": [("piece", False)],
        "piece_length": [],
        "piece_map_for_path": [("disk_path", False)],
        "demand_for_path": [("disk_path", False), ("start_byte", False),
                            ("end_excl", False)],
        "status": [],
    }
    sig_check("SessionManager", SessionManager, SESSION_SIG)

    # ------------------------------------------------ [2] 属性（property）
    print("\n[2] SessionManager 属性（download_dir / metadata_timeout / current_result）")
    for name in ("download_dir", "metadata_timeout", "current_result"):
        check(hasattr(SessionManager, name), f"属性 {name} 存在")

    # ------------------------------------------------ [3] 实例级兼容属性
    # UI 与测试直访这些成员（历史破封装，决策：重构期间保留兼容）。
    # 均为实例属性，须构造实例检查；__init__ 不启会话，开销可忽略。
    print("\n[3] 实例级兼容属性（UI/测试直访，保留兼容决策）")
    tmp = tempfile.mkdtemp(prefix="mv_contract_")
    try:
        mgr = SessionManager(os.path.join(tmp, "cache"))
    except Exception as e:
        print(f"构造 SessionManager 失败：{e}")
        sys.exit(2)
    for name in ("scheduler", "_ses", "_metadata_timeout", "_active_downloads",
                 "cache_dir", "_handle", "on_metadata", "on_error",
                 "on_file_completed"):
        check(hasattr(mgr, name), f"实例属性 {name} 存在（外部直访依赖）")
    # R-4（阶段 6 收编）：UI 不再直写私有 _metadata_timeout —— 源码级冻结，
    # 谁改回去这里就红。_metadata_timeout 兼容 property 仍保留（测试直读）。
    import io as _io
    mw = _io.open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "ui", "main_window.py"),
                  encoding="utf-8").read()
    check("._metadata_timeout" not in mw,
          "ui/main_window.py 无私有成员直写（R-4：走 apply_metadata_timeout）")

    # ------------------------------------------------ [4] 协作模块契约
    print("\n[4] 协作模块签名（拆 fetcher 时的误伤防线）")
    sig_check("models", models, {
        "safe_rel_path": [("segments", False)],
        "file_disk_path": [("cache_dir", False), ("f", False)],   # 契约 #7
        "human_size": [("n", False)],
        "contiguous_bytes": [("pm", False), ("limit", True)],
        # P2-1 位图 have 工厂（流服务/scheduler/preview 三条热路径共用）
        "have_from_bitmap": [("pieces", False)],
        "range_available": [("pm", False), ("start", False), ("end_excl", False)],
        "disk_root": [("cache_dir", False), ("save_subdir", True)],
    })
    sig_check("parser", parser, {
        "parse_torrent_file": [("path", False)],
        "parse_magnet_uri": [("uri", False)],
        "torrent_info_hash": [("info", False)],
        "is_pure_v2": [("info", False)],
        "is_torrent_path": [("source", False)],
    })
    sig_check("scheduler.tail_piece_window", scheduler, {
        "tail_piece_window": [("file", False), ("piece_length", False)],
    })
    # plan/07 阶段 1：窗口按字节预算换算（大块种子 240MB 全 ASAP → 16MB 保序）。
    # 纯函数签名 + 两个新常量按同手法冻结；LOOKAHEAD_PIECES 降级为块数上限但
    # 取值不变（上面的 preview 绑定断言仍守着 60）。
    sig_check("scheduler.window_pieces", scheduler, {
        "window_pieces": [("piece_length", False)],
    })
    check(scheduler.LOOKAHEAD_BYTES == 16 * 1024 * 1024,
          "scheduler.LOOKAHEAD_BYTES == 16MB（plan/07 阶段 1 播放窗口字节预算）")
    check(scheduler.DEADLINE_STEP_MS == 400,
          "scheduler.DEADLINE_STEP_MS == 400（窗口内 deadline 递增步长）")
    check(scheduler.LOOKAHEAD_PIECES == 60
          and scheduler.window_pieces(16 * 1024) == 60
          and scheduler.window_pieces(1024 * 1024) == 16
          and scheduler.window_pieces(4 * 1024 * 1024) == 4
          and scheduler.window_pieces(8 * 1024 * 1024) == 4,
          "window_pieces 换算表冻结（16KB→60 上限 / 1MB→16 / 4MB→4 / 8MB→4 下限）")
    # plan/07 阶段 2：尾窗按字节收敛（大块种子整尾窗 44MB→≈10MB）+ 开播门控
    # 改判「尾部入口」（最末 2MB 覆盖块）。纯函数签名 + 常量 + 换算表同手法冻结。
    sig_check("scheduler.tail_window_bytes", scheduler, {
        "tail_window_bytes": [("size", False)],
    })
    sig_check("scheduler.tail_entry_pieces", scheduler, {
        "tail_entry_pieces": [("file", False), ("piece_length", False)],
    })
    check(scheduler.TAIL_BYTES_MIN == 2 * 1024 * 1024
          and scheduler.TAIL_BYTES_MAX == 16 * 1024 * 1024
          and scheduler.TAIL_RATIO == 0.0025
          and scheduler.TAIL_ENTRY_BYTES == 2 * 1024 * 1024
          and scheduler.TAIL_MAX_PIECES == 128,
          "阶段 2 尾窗常量冻结（下限 2MB / 上限 16MB / 0.25% / 入口 2MB / 块上限 128）")
    check(scheduler.tail_window_bytes(500 * 1024 * 1024) == 2 * 1024 * 1024
          and scheduler.tail_window_bytes(1024 ** 3) == int(1024 ** 3 * 0.0025)
          and scheduler.tail_window_bytes(int(4.1 * 1024 ** 3))
          == int(int(4.1 * 1024 ** 3) * 0.0025)
          and scheduler.tail_window_bytes(100 * 1024) == 100 * 1024
          and scheduler.tail_window_bytes(100 * 1024 ** 3) == 16 * 1024 * 1024
          and scheduler.tail_window_bytes(0) == 0,
          "tail_window_bytes 换算表冻结（4.1GB→0.25%≈10.5MB / 1GB→2.56MB / "
          "500MB→2MB 下限 / 100KB→自身 / 上限 16MB / size<=0→0）")
    sig_check("PreviewScheduler", scheduler.PreviewScheduler, {
        "begin": [("handle", False), ("file", False)],              # 契约 #5
        "request_range": [("start_byte", False), ("end_byte", False)],
        "seek_to_byte": [("byte_offset", False)],
        # 阶段 B：stop 新增可选参数 release_only（convert 档只清锚点不
        # 冻结）。可选参数不破坏既有调用面——默认 False 行为与基线逐字一致。
        "stop": [("release_only", True)],
        "contiguous_progress": [],
        "tail_ready": [],
        # plan/07 阶段 2：门控改用的新方法（tail_ready 保留不删，仍供
        # tick() 判断是否继续补拉整尾窗）。
        "tail_entry_ready": [],
        "buffer_progress": [],
    })
    sig_check("stream_server", stream_server, {
        "_is_within": [("root", False), ("path", False)],           # 契约 #9
    })
    sig_check("StreamServer", stream_server.StreamServer, {
        "__init__": [("base_dir", False), ("avail_cb", True),
                     ("pieces_cb", True), ("demand_cb", True),
                     ("wait_timeout", True), ("bases", True),
                     ("max_concurrency", True)],   # P1-1 并发上限（可选，默认 16）
        "url_for": [("rel_path", False)],
    })
    sig_check("cache_guard", cache_guard, {                          # 契约 #8
        "is_risky_dir": [("path", False)],
        "ensure_cache_dir": [("path", False)],
        "guard_ok_for_cleanup": [("path", False), ("require_marker", True)],
    })
    # persist（阶段 1 抽出）：内部模块，但后续 registry/taskops 仍依赖它，
    # 签名同样冻结——后续阶段只能**使用**它，不得顺手改签名。
    sig_check("persist", persist, {
        "is_within": [("root", False), ("path", False)],
        "task_dir": [("download_dir", False), ("ih", False),
                     ("save_subdir", True)],
        "save_subdir_of": [("cache_dir", False), ("path", False)],
        "safe_task_save_path": [("cache_dir", False), ("download_dir", False),
                                ("ih", False), ("save_path", False)],
        "read_resume": [("cache_dir", False), ("ih", False)],
        # 键/待落盘筛选：临时键 tmp-<id> 必须在这里被拦掉，否则 resume_path 的
        # ValueError 会掀翻退出清理——三个阶段（registry/taskops）都会用到。
        "is_resume_key": [("key", False)],
        "missing_resume_keys": [("cache_dir", False), ("torrents", False)],
        "pending_resume_keys": [("cache_dir", False), ("torrents", False)],
    })
    sig_check("persist.TaskPersistence", persist.TaskPersistence, {
        "persist_tasks": [],
        "read_resume": [("ih", False)],
        "request_resume": [("rec", False)],
        "write_resume_from_alert": [("a", False)],
        "drain_resume_alerts": [("timeout", True)],
        "restore_task": [("t", False)],
        # 目录工具只保留模块级纯函数（无状态，registry/taskops 直接取用），
        # 不在 TaskPersistence 上留一层同签名的实例包装，避免两套入口。
    })
    # registry（阶段 3 抽出）：单锁归属 + 注册表 + TaskRecord 本体（R-2）。
    # 后续 taskops/resolver/preview 都建在它上面，签名与别名集冻结。
    sig_check("registry", registry, {
        "hash_key": [("handle", False)],
        "ih_from_params": [("p", False)],
    })
    sig_check("registry.TaskRegistry", registry.TaskRegistry, {
        "find_record": [("handle", False)],
        "find_record_by_ih": [("ih", False)],
        "current_record": [],
        "current": [],
        "resolving_snapshot": [],
        "protected_dirs": [],
        "put_record_locked": [("ih", False), ("rec", False),
                              ("make_current", True)],
        "apply_current_locked": [("rec", False), ("ih", False)],
        "register_current_locked": [("handle", False), ("result", False),
                                    ("gen", False)],
        "detach_record_locked": [("rec", False)],
        "clear_resolving_locked": [],
        "clear_runtime_state_locked": [],
        "focus_current": [("ih", False)],
        "bump_gen": [],
        "clear_resolving_safe": [],
        "set_resolving": [("v", False), ("started", True)],
        "preview_dir": [("ih", False)],
    })
    check(registry.METADATA_TIMEOUT == 90.0
          and registry.DOWNLOADS_SUBDIR == "downloads"
          and registry.PREVIEW_SUBDIR == ".preview",
          "registry 常量取值不变（90.0/downloads/.preview）")
    _tnames = {f.name for f in __import__("dataclasses").fields(
        registry.TaskRecord)}
    check(_tnames == {"handle", "result", "gen", "resolving", "resolve_started",
                      "state", "timeout", "download", "seed", "priority",
                      "save_path", "source", "error"},
          "TaskRecord 字段集不变（13 项，持久化/UI 快照按名取值）")
    check(all(isinstance(getattr(SessionManager, a), property) for a in
              ("_lock", "_torrents", "_tasks", "_current_ih", "_handle",
               "_result", "_resolving", "_resolve_started", "_gen",
               "_metadata_timeout")),
          "fetcher 10 个注册表别名全为 property 代理（单源，无第二份数据）")
    check(all(hasattr(fetcher_mod, n) for n in
              ("TaskRecord", "TaskRegistry", "METADATA_TIMEOUT",
               "DOWNLOADS_SUBDIR", "PREVIEW_SUBDIR")),
          "core.fetcher 仍再导出 TaskRecord/TaskRegistry/常量（历史 import 不破）")
    # resolver（阶段 5 抽出）：解析与元数据编排。alert 双回调是 session
    # 注入线，begin_resolve 换代语义是 P1-10/11 竞态修复机制——全冻结。
    sig_check("resolver", resolver, {
        "result_from_torrent_info": [("ti", False), ("info_hash", False)],
    })
    sig_check("resolver.ResolverCore", resolver.ResolverCore, {
        "resolve": [("source", False)],
        "begin_resolve": [],
        "connect_peer": [("ip", False), ("port", False),
                         ("wait_handle", True), ("task_id", True)],
        "on_metadata_received": [("rec", False)],
        "on_download_finished": [("rec", False)],
    })
    # preview（阶段 5 抽出）：磁盘路径反查→PieceMap→点播补拉→状态快照。
    # find/piece_map/demand 的 None/False「不可判定」语义是流服务安全前提。
    sig_check("preview.PreviewCore", preview.PreviewCore, {
        "start_preview": [("f", False)],
        # 阶段 B：可选参数 release_only（convert 档停预览不冻结）
        "stop_preview": [("release_only", True)],
        "find_record_for_path": [("disk_path", False)],
        "piece_map_for_path": [("disk_path", False)],
        "demand_for_path": [("disk_path", False), ("start_byte", False),
                            ("end_excl", False)],
        "task_result": [("task_id", False)],
        "have_piece": [("piece", False)],
        "piece_length": [],
        "current_result": [],
        "status": [],
    })
    # A1：preview 点播上限复用 scheduler.LOOKAHEAD_PIECES（scheduler 不
    # import preview，零环保持）——常量绑定关系与取值一并冻结。
    check(hasattr(preview, "LOOKAHEAD_PIECES")
          and preview.LOOKAHEAD_PIECES == scheduler.LOOKAHEAD_PIECES == 60,
          "preview.LOOKAHEAD_PIECES 绑定 scheduler 常量且取值 60（A1）")
    check(all(hasattr(fetcher_mod, n) for n in
              ("STATE_NAMES", "resolver_result_from_ti")),
          "core.fetcher 仍再导出 STATE_NAMES / resolver 纯函数别名")
    # taskops（阶段 4 抽出）：任务 CRUD 全量签名冻结。fetcher 的 7 个任务
    # 公开方法从此只是薄委托；download_mgr_test 是它的端到端对账方。
    sig_check("taskops", taskops, {
        "lt_priority": [("p", False)],
    })
    sig_check("taskops.TaskOps", taskops.TaskOps, {
        "add_task": [("source", False), ("save_subdir", True),
                     ("priority", True), ("seed", True)],
        "task_dir": [("ih", False), ("save_subdir", True)],
        # 阶段 B：preserve_files（convert 转正保留预览 file-priority，
        # 且解除 upload_mode → auto_managed → resume 的调用顺序是转正生效前提）
        "activate_download": [("rec", False), ("preserve_files", True)],
        "set_priority": [("task_id", False), ("priority", False)],
        "pause_task": [("task_id", False)],
        "resume_task": [("task_id", False)],
        "remove_task": [("task_id", False), ("delete_files", True)],
        "delete_task_files": [("key", False), ("path", False)],
        "focus_task": [("task_id", False)],
        "tasks": [],
    })
    # session（阶段 2 抽出）：同 persist 处理——后续阶段只能使用、不得改签名。
    sig_check("session", session, {
        "alert_category_mask": [],
        "build_session_settings": [("listen_port", False),
                                   ("active_downloads", False),
                                   ("proxy", True)],
    })
    sig_check("session.SessionCore", session.SessionCore, {
        "start": [("proxy", True), ("metadata_timeout", True)],
        "apply_proxy": [("proxy", False)],
        "apply_rate_limit": [("kbps", False)],
        "shutdown": [],
        "alert_loop": [],
        "handle_alert": [("a", False)],
        "metadata_watchdog": [("now", True)],
        "resume_sweep": [("now", True)],
    })
    check(session.ALERT_POLL_INTERVAL == 0.15
          and session.SHUTDOWN_JOIN_TIMEOUT == 2.0
          and session.RESUME_SWEEP_INTERVAL == 60.0,
          "session 三个节奏常量不变（0.15 / 2.0 / 60.0）")
    check(all(hasattr(fetcher_mod, n) for n in ("SessionCore", "SessionDeps")),
          "core.fetcher 仍再导出 SessionCore / SessionDeps")

    # states（阶段 1 下沉的常量层）：取值冻结——改状态名 = 破坏磁盘数据兼容
    for name, val in (("STATE_QUEUED", "QUEUED"),
                      ("STATE_META_FETCH", "META_FETCH"),
                      ("STATE_VALIDATE", "VALIDATE"),
                      ("STATE_DOWNLOADING", "DOWNLOADING"),
                      ("STATE_PAUSED", "PAUSED"),
                      ("STATE_COMPLETED", "COMPLETED"),
                      ("STATE_STOPPED", "STOPPED"),
                      ("STATE_FAILED", "FAILED"),
                      ("STATE_SEEDING", "SEEDING"),
                      ("STATE_DELETED", "DELETED"),
                      ("STATE_READY", "READY")):
        check(getattr(states, name, None) == val, f"states.{name} == {val!r}")
    check(len(states.BOOTSTRAP_TRACKERS) == 5
          and all(t.startswith("udp://") for t in states.BOOTSTRAP_TRACKERS),
          "states.BOOTSTRAP_TRACKERS 5 条 udp tracker 不变")
    check(len(states.DOWNLOAD_STATES) == 10,
          "states.DOWNLOAD_STATES 成员数不变（10）")
    check(all(hasattr(fetcher_mod, n) for n in
              ("STATE_QUEUED", "STATE_DOWNLOADING", "STATE_PAUSED",
               "STATE_COMPLETED", "STATE_FAILED", "STATE_META_FETCH",
               "DOWNLOAD_STATES", "BOOTSTRAP_TRACKERS")),
          "core.fetcher 仍再导出 STATE_* / DOWNLOAD_STATES / BOOTSTRAP_TRACKERS")
    sig_check("cache_quota", cache_quota, {
        "dir_size_bytes": [("path", False)],
        "scan_preview_dirs": [("preview_root", False)],
        "enforce_preview_limit": [("preview_root", False), ("limit_mb", False),
                                  ("keep_dirs", True), ("warn", True)],
        # plan/07 阶段 3：缓存**显示**口径改已下载字节（file_progress 汇总），
        # 纯函数冻结；配额判定仍走 dir_size_bytes（管磁盘占用，二者不互换）。
        "downloaded_bytes": [("file_progress", False)],
    })
    check(cache_quota.downloaded_bytes([59 * 1024 * 1024]) == 59 * 1024 * 1024
          and cache_quota.downloaded_bytes([1, 2, 3]) == 6
          and cache_quota.downloaded_bytes([]) == 0
          and cache_quota.downloaded_bytes(None) == 0
          and cache_quota.downloaded_bytes([-1, 5, None, "x"]) == 5,
          "downloaded_bytes 口径冻结（汇总/空/None→0、非法与负值容错）")
    check(cache_guard.CACHE_MARKER == ".magnet_viewer_cache",
          "CACHE_MARKER 常量值不变")

    # ------------------------------------------------ 汇总
    print(f"\n=== 契约自检：OK {len(OK)} / FAIL {len(FAIL)} ===")
    if FAIL:
        for m in FAIL:
            print(f"  - {m}")
        print("\nX 契约被破坏：重构不得改变对外接口签名（如需变更，"
              "请同步更新 contract_snapshot.md 与本基线表）")
        return 1
    print("- 对外契约一致")
    return 0


if __name__ == "__main__":
    sys.exit(main())
