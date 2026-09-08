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


def enforce_preview_limit(preview_root: str, limit_mb: int,
                          keep_dirs: set[str] | None = None,
                          warn=None) -> tuple[int, int]:
    """执行配额：超限时按 LRU 删除最旧的任务目录。

    返回 ``(清理后总占用字节, 本次释放字节)``。
    ``limit_mb <= 0`` 视为不限制，只统计不删除。
    """
    keep = {os.path.normcase(os.path.abspath(d))
            for d in (keep_dirs or set()) if d}
    items = scan_preview_dirs(preview_root)
    total = sum(size for _p, size, _t in items)

    def _warn(msg: str) -> None:
        try:
            (warn or (lambda m: log_warning("cache_quota", m)))(msg)
        except Exception:
            pass

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
        if os.path.normcase(os.path.abspath(path)) in keep:
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
