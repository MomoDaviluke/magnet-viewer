"""统一日志：让「静默失败」留下痕迹。

背景
----
本项目历史上多次因宽泛捕获后静默 pass 掩盖真实缺陷，且排查成本极高：

- 告警批次处理异常导致 `metadata_received_alert` 被整批丢弃（元数据永不回调）；
- 流服务回调签名错误被吞后静默降级为「按完整静态文件服务」，
  把未下载的稀疏零数据喂给播放器；
- 路径映射键错位导致预览静默退化，界面无任何异常提示。

这些问题的共性是：**失败了，但没人知道**。本模块提供开销极低的记录入口，
配合分级改造把高危路径的错误显性化。

设计约束
--------
1. **绝不因日志本身抛异常** —— 所有入口都吞掉自身错误，日志是旁路设施。
2. **线程安全** —— 告警线程、流服务线程、UI 线程都会调用；`logging` 模块
   本身线程安全，此处不再加锁。
3. **可关闭** —— 由配置 `logging_enabled` 控制，关闭后全部调用降为空操作。
4. **滚动** —— 单文件 1 MB，保留 3 份，避免长期运行撑爆磁盘。
"""
from __future__ import annotations

import logging
import os
import sys
import tempfile
from logging.handlers import RotatingFileHandler

LOGGER_NAME = "magnet_viewer"
LOG_FILENAME = "magnet-viewer.log"
MAX_BYTES = 1024 * 1024      # 单文件 1 MB
BACKUP_COUNT = 3

_enabled = True
_configured = False
_log_path = ""


def default_log_dir() -> str:
    """默认日志目录：系统临时目录下的固定子目录（跨会话可查）。"""
    return os.path.join(tempfile.gettempdir(), "magnet_viewer_logs")


def setup(log_dir: str | None = None, *, console: bool = False) -> str:
    """初始化日志（幂等，可重复调用）。返回日志文件路径。

    console=True 时额外输出到 stderr，便于开发期排查。
    """
    global _configured, _log_path
    if _configured:
        return _log_path

    directory = log_dir or default_log_dir()
    try:
        os.makedirs(directory, exist_ok=True)
    except OSError:
        directory = default_log_dir()
        try:
            os.makedirs(directory, exist_ok=True)
        except OSError:
            _configured = True
            return ""

    path = os.path.join(directory, LOG_FILENAME)
    log = logging.getLogger(LOGGER_NAME)
    log.setLevel(logging.DEBUG)
    log.propagate = False
    for h in list(log.handlers):      # 避免重复初始化产生重复记录
        log.removeHandler(h)

    fmt = logging.Formatter(
        "%(asctime)s.%(msecs)03d [%(levelname)s] [%(threadName)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S")
    try:
        fh = RotatingFileHandler(path, maxBytes=MAX_BYTES,
                                 backupCount=BACKUP_COUNT, encoding="utf-8")
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(fmt)
        log.addHandler(fh)
    except OSError:
        pass      # 日志文件不可建（权限/磁盘）：降级为无文件日志，不影响主流程
    if console:
        try:
            sh = logging.StreamHandler(sys.stderr)
            sh.setLevel(logging.DEBUG)
            sh.setFormatter(fmt)
            log.addHandler(sh)
        except OSError:
            pass  # 同上：控制台句柄建不起来也不影响主流程

    _log_path = path
    _configured = True
    return path


def set_enabled(enabled: bool) -> None:
    """关闭后所有记录调用降为空操作（仍保留文件，便于用户事后开启追溯）。"""
    global _enabled
    _enabled = bool(enabled)


def is_enabled() -> bool:
    return _enabled


def log_path() -> str:
    return _log_path


def _write(level: int, tag: str, msg: str, exc_info: bool = False) -> None:
    if not _enabled:
        return
    try:
        if not _configured:
            setup()
        log = logging.getLogger(LOGGER_NAME)
        if not log.handlers:
            return
        log.log(level, f"[{tag}] {msg}", exc_info=exc_info)
    except Exception:
        pass      # S-10 唯一豁免：日志器自身故障（handler emit/格式化/滚动/磁盘满）
                  # 绝不影响主流程——任何异常类型都可能，语义上不可收窄。


def log_exception(tag: str, exc: BaseException | None = None) -> None:
    """记录一处被吞掉的异常。tag 用「模块.函数」标明位置，便于定位。

    用于宽泛捕获分支内部，替代无声的 `pass`。
    """
    if exc is None:
        _write(logging.ERROR, tag, "异常被吞掉（未提供异常对象）", exc_info=True)
        return
    _write(logging.ERROR, tag, f"{type(exc).__name__}: {exc}", exc_info=exc)


def log_warning(tag: str, msg: str) -> None:
    _write(logging.WARNING, tag, msg)


def log_info(tag: str, msg: str) -> None:
    _write(logging.INFO, tag, msg)
