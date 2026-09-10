"""预览缓存配额：超限按 LRU 清理最旧的预览任务目录（REVIEW P2-2）。

背景
----
预览/画廊数据落盘 ``<cache>/.preview/<ih>/``，切换预览文件从不释放
上一个文件的已下载数据——预览几部 4 GB 影片即在系统临时目录累积
数十 GB，用户无感知。

策略
----
1. **只动 .preview/**：本模块仅扫描/删除 ``<cache>/.preview/`` 的一级
   子目录（每个子目录对应一个 info_hash 的预览数据）。``downloads/``
   （用户下载数据）、``.tasks.json``、``.resume/`` 等一律不碰——
   那是 cache_guard 保留名单管辖的用户数据，本模块无权涉足。
2. **保护名单**：``keep_dirs`` 中的目录（活跃会话/任务的落盘目录）
   绝不删除。调用方从 ``SessionManager.protected_dirs()`` 取。
3. **LRU**：按「最近活跃时间」从旧到新整目录删除，直到总占用回到
   上限以内。活跃时间 = 目录内全部文件的最大 mtime（写入即刷新），
   缺省退化为目录自身 mtime。
4. **绝不抛异常**：单个目录删除失败（文件被占用等）跳过留待下次，
   只经 ``warn`` 留痕。日志与清理都是旁路设施，不能影响预览主流程。

纯函数 + 显式依赖，便于单元测试（见 smoke_test 配额段）。
"""
from __future__ import annotations

import os
import shutil
from typing import Callable, Iterable

from core.logutil import log_warning


def dir_size_bytes(path: str) -> int:
    """目录逻辑大小（字节；含子目录，统计失败按 0 计）。"""
    total = 0
    try:
        for root, _dirs, files in os.walk(path):
            for name in files:
                try:
                    total += os.stat(os.path.join(root, name)).st_size
                except OSError:
                    pass
    except OSError:
        pass
    return total


def downloaded_bytes(file_progress) -> int:
    """已下载字节汇总（plan/07 阶段 3 的**显示**口径）。

    ``handle.file_progress()`` 是每文件的已下载字节数组（核心指标，已在
    ``background_cache_text`` / 状态快照多处使用）。此前状态栏「缓存占用」
    走 ``dir_size_bytes(.preview)``——统计的是**预分配尺寸**：稀疏文件预分配
    后目录逻辑大小恒等于文件大小，4.1GB 的种子才下 59MB 就显示「缓存
    4.1 GB / 2.0 GB」，既误导又像爆缓存。改用已下载字节后显示真实进度。

    **只服务显示**：预览缓存上限判定仍走 ``dir_size_bytes``（它管磁盘占用，
    需保守），二者口径不同、不可互换。

    容错：``None`` / 非法项按 0 计、负值截 0（不产生负数占用）；纯函数，
    不碰文件系统，便于单测（contract_check 冻结）。
    """
    total = 0
    for v in (file_progress or ()):
        try:
            n = int(v)
        except (TypeError, ValueError):
            continue
        if n > 0:
            total += n
    return total


def _last_active(path: str) -> float:
    """目录最近活跃时间：全部文件的最大 mtime，空目录用目录 mtime。"""
    newest = 0.0
    try:
        for root, _dirs, files in os.walk(path):
            for name in files:
                try:
                    newest = max(newest, os.stat(
                        os.path.join(root, name)).st_mtime)
                except OSError:
                    pass
        if newest <= 0:
            newest = os.stat(path).st_mtime
    except OSError:
        pass
    return newest


def scan_preview_dirs(preview_root: str) -> list[tuple[str, int, float]]:
    """列出 .preview 下的任务目录：[(绝对路径, 大小字节, 最近活跃时间)]。"""
    out: list[tuple[str, int, float]] = []
    try:
        entries = list(os.scandir(preview_root))
    except OSError:
        return out
    for e in entries:
        if not e.is_dir():
            continue          # 散落的杂项文件不属于任何任务，不清理（保守）
        p = e.path
        out.append((p, dir_size_bytes(p), _last_active(p)))
    return out


def _norm_keep(
        keep_dirs: "set[str] | Iterable[str] | Callable[[], set[str] | Iterable[str]] | None"
        ) -> set[str] | None:
    """keep_dirs 归一：set/None 直取，callable 现取快照（C2）。

    回调抛异常返回 None——调用方按「名单故障」保守处理（本轮不删）。
    D5-B 措辞诚实化：现网唯一名单来源 registry.protected_dirs 是锁内纯
    dict 遍历，实际不抛异常；异常上抛透传到 None 分支只是最后防线。主要
    故障模式是**空注册表竞态**（会话未起/已停机），该场景正常返回空集，
    维持「无保护目录可删」的现状语义——空集不当可疑处理。
    """
    try:
        kd = keep_dirs() if callable(keep_dirs) else keep_dirs
    except Exception:
        return None
    return {os.path.normcase(os.path.abspath(d))
            for d in (kd or set()) if d}


def enforce_preview_limit(
        preview_root: str, limit_mb: int,
        keep_dirs: "set[str] | Iterable[str] | Callable[[], set[str] | Iterable[str]] | None" = None,
        warn=None) -> tuple[int, int]:
    """执行配额：超限时按 LRU 删除最旧的任务目录。

    返回 ``(清理后总占用字节, 本次释放字节)``。
    ``limit_mb <= 0`` 视为不限制，只统计不删除。

    ``keep_dirs`` 接受**集合或零参回调**（阶段 C C2）：回调形态下入口取
    一次快照、**每个候选目录 rmtree 前再取一次**复核——堵住「取名单 →
    扫描 → 执行删除」窗口内新登记目录（刚开的 review / 刚转正的任务）
    被陈旧快照误删的竞态。回调抛异常 = 名单故障 → 本轮保守不删。
    集合形态行为与基线逐字一致（无逐项复核开销）。
    """
    keep = _norm_keep(keep_dirs)
    items = scan_preview_dirs(preview_root)
    total = sum(size for _p, size, _t in items)

    def _warn(msg: str) -> None:
        try:
            (warn or (lambda m: log_warning("cache_quota", m)))(msg)
        except Exception:
            pass

    if keep is None:
        _warn("保护名单取用失败：本轮跳过配额删除（保守）")
        return total, 0
    if limit_mb <= 0:
        return total, 0
    limit = int(limit_mb) * 1024 * 1024
    if total <= limit:
        return total, 0

    freed = 0
    # 最旧优先；同刻按路径排序保证确定性
    for path, size, _t in sorted(items, key=lambda x: (x[2], x[0])):
        if total - freed <= limit:
            break
        norm = os.path.normcase(os.path.abspath(path))
        if norm in keep:
            continue
        if callable(keep_dirs):
            # C2：删除动作与入口快照之间可插入新 review——rmtree 前复核
            fresh = _norm_keep(keep_dirs)
            if fresh is None:
                _warn("保护名单取用失败：中止本轮剩余删除（保守）")
                break
            keep = fresh
            if norm in keep:
                continue
        try:
            shutil.rmtree(path, ignore_errors=True)
        except Exception as e:
            _warn(f"删除预览目录失败（留待下次）：{path}：{e}")
            continue
        if os.path.isdir(path):
            # ignore_errors=True 会吞掉删除失败（如文件被占用），此时若仍
            # 计入 freed，配额会提前「达标」而实际仍超限（Copilot 评审指出）
            _warn(f"删除预览目录失败（留待下次）：{path}")
            continue
        freed += size
        _warn(f"预览缓存超限，按 LRU 清理：{path}（释放 {size} 字节）")
    return total - freed, freed
