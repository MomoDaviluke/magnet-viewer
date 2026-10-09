"""缓存目录守卫：防止「清理缓存」误删用户数据目录。

背景：缓存目录可由用户在设置面板自由配置（QSettings 持久化），
「立即清理/退出清理」会清空目录内容。若 cache_dir 指向磁盘根目录、
用户主目录或文档目录，一键清理即灾难（历史审查 P1-2）。

策略：
1. 高风险目录（盘符根、主目录及常见用户数据目录）直接拒绝作为缓存目录；
2. 受管缓存目录写入标记文件 `CACHE_MARKER`，清理前必须存在该标记
   —— 只有本程序声明过“这是我的缓存目录”的路径才允许被清空；
3. 清理内容必须走 `clear_cache_contents()` 的**保留名单**（downloads/、
   .tasks.json、.resume/ 是用户下载数据与任务持久化，绝不参与清理）——
   历史缺陷：设置对话框曾绕开保留名单二次清空整个目录（P0-1）。
"""
from __future__ import annotations

import os
import shutil
import time
from typing import Callable, Iterable

CACHE_MARKER = ".magnet_viewer_cache"

# 清理缓存时保留的条目（决策 D8/D9）：用户下载数据 + 任务持久化文件 + 受管标记
# + 元数据缓存 .meta（性能专项 P-1：几 KB~几 MB 的 info 段，删掉等于让下次
# 解析同一资源重新走一遍 DHT 往返，代价远大于它占的磁盘）+ DHT 路由表快照。
# 二者都是「可重建但重建很贵」的加速数据，与 .resume 同一处置级别。
CLEANUP_KEEP: frozenset[str] = frozenset(
    {"downloads", ".tasks.json", ".resume", CACHE_MARKER,
     ".meta", ".dht_state"})

_DATA_DIRS: frozenset[str] | None = None


def _user_data_dirs() -> frozenset[str]:
    """主目录 + 常见用户数据目录（规范化、小写化后缓存）。"""
    global _DATA_DIRS
    if _DATA_DIRS is None:
        home = os.path.expanduser("~")
        cands = [home,
                 os.path.join(home, "Documents"),
                 os.path.join(home, "Desktop"),
                 os.path.join(home, "Downloads"),
                 os.path.join(home, "Pictures"),
                 os.path.join(home, "Videos"),
                 os.path.join(home, "Music"),
                 os.path.join(home, "AppData")]
        _DATA_DIRS = frozenset(
            os.path.normcase(os.path.normpath(p)) for p in cands)
    return _DATA_DIRS


def is_risky_dir(path: str) -> bool:
    """盘符根 / 用户主目录与常见数据目录 → 禁止作为缓存目录。"""
    norm = os.path.normpath(os.path.abspath(path))
    drive, tail = os.path.splitdrive(norm)
    if tail in ("\\", "/", ""):          # 盘符根（C:\、D:/ 等）
        return True
    return os.path.normcase(norm) in _user_data_dirs()


def ensure_cache_dir(path: str) -> None:
    """校验并准备缓存目录（幂等）：拒绝高风险目录，写入受管标记文件。

    标记文件写入失败（只读介质等）不阻断使用——清理守卫会因缺标记而
    拒绝清理，属安全降级。
    """
    if is_risky_dir(path):
        raise ValueError(f"缓存目录不允许是磁盘根目录或用户数据目录：{path}")
    os.makedirs(path, exist_ok=True)
    marker = os.path.join(path, CACHE_MARKER)
    if not os.path.isfile(marker):
        try:
            with open(marker, "w", encoding="utf-8") as f:
                f.write("magnet-viewer managed cache dir (safe to delete).\n")
        except OSError:
            pass


def guard_ok_for_cleanup(path: str, require_marker: bool = True) -> bool:
    """清理前守卫：非高风险目录，且（默认）含受管标记文件。"""
    if is_risky_dir(path):
        return False
    if require_marker:
        return os.path.isfile(os.path.join(path, CACHE_MARKER))
    return True


def clear_cache_contents(cache_dir: str,
                         keep: Iterable[str] | None = None,
                         keep_dirs: set[str] | Iterable[str]
                         | Callable[[], Iterable[str] | None] = ()) -> int:
    """清空缓存目录内容，但保留名单之外的条目一律删除（唯一清理入口）。

    **保留名单**（默认 CLEANUP_KEEP）：downloads/（用户下载数据）、
    .tasks.json（任务清单）、.resume/（fastresume 续传数据）、
    .meta/（元数据缓存）、.dht_state（DHT 路由表快照）、受管标记。

    ``keep_dirs``（阶段 C C1）：**绝对路径集合**（调用方自
    ``session.protected_dirs()`` 取快照）——命中目录**及其内容**整体跳过，
    归一判定（normcase+abspath）与 cache_quota 的 keep 语义对齐。convert
    转正任务的落盘目录仍在 ``.preview/<ih>``（沿用已下分块零重下），手动
    清理/退出清理若不复核活任务名单会把其已缓存文件删光、引擎对着空目录
    重下（永动机）。父目录（如 .preview）因含受保子目录转入**部分清理**
    （只删无保兄弟）。缺省空集 = 基线行为逐字不变（无命中即每层纯 rmtree，
    向后兼容）。

    **plan/11 R-7（TOCTOU 修复）**：``keep_dirs`` 现在也接受**回调**
    （返回最新名单 / ``None`` = 取用失败）。回调形态下，**每删一个顶层条目
    前都会重取一次名单**——因为「取名单 → 逐条删除」之间可能新起任务或
    convert 转正，旧快照认不得新目录，被 ``rmtree`` 后就是「引擎假完成：
    磁盘空、零错误日志、不重下」。复核粒度只在**顶层条目**（嵌套语义不变）；
    取用失败按 ``core/cache_quota.py`` C2 的保守策略**中止本轮剩余删除**。

    两条路径各自的正确用法（不要混）：
      - **手动清理**（``_clear_cache_now``）→ 传**回调**（删除期间活任务可能
        变化，必须逐条复核）；
      - **退出清理**（``_shutdown_worker``）→ 传**shutdown 之前**的快照集合
        ——此时段在 shutdown 之后，活任务已清空、libtorrent 文件锁已释放，
        回调反而会拿到空集把转正任务目录删光（``ui/main_window.py`` 的
        D4b/C1 时机注释是这么定的，勿改）。

    调用方须先经 guard_ok_for_cleanup() 守卫（本函数不重复校验，便于
    设置对话框对编辑框当前值先校验再清理）。返回删除的条目数（顶层
    口径；keep_dirs 部分清理时嵌套删除数上卷），目录不存在返回 -1。
    **顶层** listdir 失败（权限/竞态删除）上抛 OSError——调用方
    （_clear_preview_cache_now 等）统一 try→-1 兜底并提示清理失败，
    绝不静默返回 0 冒充「清理成功、删了 0 项」（D4c，基线语义）；
    回调取名单失败则抛 RuntimeError（同样走调用方的 -1 兜底，绝不静默
    当成「名单为空」继续删）；
    **嵌套**失败条目与嵌套目录扫描失败静默跳过（部分清理尽力而为）。

    已知风险：删除**活任务**目录会导致引擎假完成（libtorrent 继续报进度
    直至 state=finished、磁盘为空、零错误日志、不重下）。回调形态下逐条
    复核已把「取名单→删除」窗口内的新增任务挡住；**静态集合形态**仍依赖
    调用方自己保证窗口内无新任务（退出清理路径满足此条件），仅在此留档。
    """
    keep = CLEANUP_KEEP if keep is None else frozenset(keep)
    if callable(keep_dirs):
        keep_fn = keep_dirs
    else:
        _static = frozenset(keep_dirs)
        keep_fn = lambda: _static       # noqa: E731  形态统一：闭包成常量回调
    _fresh = keep_fn()
    if _fresh is None:                  # 调用方显式报告「名单取不到」
        raise RuntimeError("活任务名单取用失败：中止清理（保守，plan/11 R-7）")
    keep_paths = _norm_keep_dirs(_fresh) or frozenset()
    if not os.path.isdir(cache_dir):
        return -1
    if os.path.normcase(os.path.abspath(cache_dir)) in keep_paths:
        return 0            # 缓存根本身是活任务落盘目录：内容全保，清理无操作

    def _norm(p: str) -> str:
        return os.path.normcase(os.path.abspath(p))

    def _purge(path: str, top: bool, keep_paths: frozenset) -> int:
        """删 path 下无保条目。keep 名单只在顶层生效（嵌套 .preview/<ih>
        内不豁免同名条目）；keep_paths 命中目录整棵跳过，含受保后代的目录
        递归部分清理。keep_paths 为空时每层都是纯 rmtree → 基线行为。

        ``top=True``（顶层入口）时 listdir 失败**上抛**（调用方兜底）；
        嵌套层失败返回 0（只跳过该目录，不影响兄弟清理）。"""
        removed = 0
        try:
            names = os.listdir(path)
        except OSError:
            if top:
                raise
            return 0
        for name in names:
            if top and name in keep:
                continue
            p = os.path.join(path, name)
            norm = _norm(p)
            cur = keep_paths
            if top and callable(keep_dirs):
                # plan/11 R-7：顶层**逐条复核**最新活任务名单——
                # 「取名单→删除」之间新起的任务目录在这里现形并被跳过。
                # 注意不能写 `keep_fn() or ()`：那会把 None（名单源故障
                # 信号）吞成空元组，_norm_keep_dirs(()) 返回空集合而非
                # None，下方「取不到就停手」的保守检查变成死代码，
                # 失败场景下会按空名单继续删活任务目录（审计 2026-09-29）。
                fresh = _norm_keep_dirs(keep_fn())
                if fresh is None:
                    return removed      # 保守：名单取不到就停手，不赌
                if norm in fresh:
                    continue    # 活任务目录（含转正的 .preview/<ih>）整棵跳过
                cur = fresh
            if norm in cur:
                continue
            try:
                if os.path.isdir(p):
                    if any(kp.startswith(norm + os.sep) for kp in cur):
                        # 内含受保子目录：只删无保部分；父目录保留不计条目，
                        # 但嵌套删除数上卷（口径：实删文件/目录条目总数）
                        removed += _purge(p, False, cur)
                    else:
                        shutil.rmtree(p, ignore_errors=True)
                        removed += 1
                else:
                    os.remove(p)
                    removed += 1
            except OSError:
                pass
        return removed

    return _purge(cache_dir, top=True, keep_paths=keep_paths)


def _norm_keep_dirs(dirs) -> frozenset | None:
    """活任务名单 → 规范化（normcase+abspath）集合；取用失败返回 ``None``。

    返回 ``None`` 的语义是「调用方/名单源故障」，调用方据此**保守中止**
    （对齐 ``core/cache_quota.py`` C2 的「取名单失败即停手」策略）。
    """
    try:
        return frozenset(os.path.normcase(os.path.abspath(d))
                         for d in dirs if d)
    except (TypeError, OSError):
        # 收窄：可抛面 = dirs 非可迭代 / 元素非 str（TypeError）、abspath
        # 环境异常（OSError）。返回 None 语义是「名单故障」，调用方保守中止
        return None


def sweep_tmp_files(cache_dir: str, older_than: float = 60.0) -> int:
    """清扫崩溃/强杀遗留的 ``*.tmp`` 孤儿（plan/11 R-14）。

    背景：R-1 把临时文件名改成唯一后缀之后，「崩溃后残留」不再会被下次
    同目标写入覆盖（旧固定名每目标最多留一个），而是**每次崩溃新增一个**。
    只扫**三层固定位置**——cache_dir 根、``.resume/``、``.meta/``——
    因为 R-1 的临时文件只会落在这三处，且都不递归；
    **不做全目录递归、也不在写路径上扫**（与 plan/08 D3-b 已修的
    「30 次写入触发 30 次全目录扫描」划清界限）。

    只删 ``older_than`` 秒之前的条目：防止与**另一个实例**的并发写入赛跑
    （本项目按单实例设计，这是纵深防御而非主防线）。
    清扫是旁路设施：任何失败静默跳过，绝不阻断启动。

    调用时机：``SessionManager.start()``（进程启动、尚无任何并发写者）。
    """
    if not guard_ok_for_cleanup(cache_dir):
        return 0
    deadline = time.time() - max(0.0, older_than)
    removed = 0
    for sub in (cache_dir, os.path.join(cache_dir, ".resume"),
                os.path.join(cache_dir, ".meta")):
        try:
            entries = list(os.scandir(sub))
        except OSError:
            continue
        for e in entries:
            try:
                if not e.name.endswith(".tmp") or not e.is_file():
                    continue
                if e.stat().st_mtime > deadline:
                    continue              # 太新：可能是别的实例正在写
                os.remove(e.path)
                removed += 1
            except OSError:
                continue
    return removed