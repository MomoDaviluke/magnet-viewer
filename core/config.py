"""应用设置：QSettings 持久化 + libtorrent 代理配置映射。"""
from __future__ import annotations

import json
import os

from PySide6.QtCore import QSettings

from core import secretbox
from core.cache_mode import PREVIEW_CACHE_CONVERT

RECENT_LIMIT = 15

DEFAULTS: dict = {
    "proxy_type": "none",          # none | socks5 | http
    "proxy_host": "",
    "proxy_port": 1080,
    "proxy_user": "",
    "proxy_pass": "",
    "proxy_peer": True,            # peer 连接也走代理（隐私关键项）
    "metadata_timeout": 90,        # 磁力链元数据获取超时（秒）
    "cache_dir": "",               # 空 = 系统临时目录/magnet_viewer_cache
    "clear_cache_on_exit": False,  # 退出时清理预览缓存
    "default_concurrency": 3,      # 默认并发下载数
    "download_dir": "",            # 空 = 缓存目录/downloads
    "seed_after_complete": False,  # 任务完成后继续做种（MVP 默认不做种）
    "download_rate_limit": 0,      # 下载限速 KB/s，0 = 不限（libtorrent 会话级）
    "cache_limit_mb": 2048,        # 预览缓存上限 MB，0 = 不限；超限按 LRU 清最旧预览目录
    "preview_cache_mode": PREVIEW_CACHE_CONVERT,
    # 关预览行为：convert=自动转正继续缓存（迅雷式）| hold=暂停冻结（基线）。
    # 下次开启预览时生效；值域即两常量（cache_mode.py），不新增校验逻辑。
    "logging_enabled": True,       # 运行日志开关（core.logutil；关闭后全部记录降为空操作）
}

_TYPES: dict = {
    "proxy_port": int,
    "metadata_timeout": int,
    "proxy_peer": bool,
    "clear_cache_on_exit": bool,
    "default_concurrency": int,
    "seed_after_complete": bool,
    "download_rate_limit": int,
    "cache_limit_mb": int,
    "logging_enabled": bool,
}

# libtorrent settings_pack::proxy_type_t 的整型值（2.1.x 仍是稳定枚举）
LT_PROXY_TYPES = {"none": 0, "socks4": 1, "socks5": 2,
                  "socks5_pw": 3, "http": 4, "http_pw": 5}


class AppConfig:
    """QSettings 封装：读写用户设置（Windows 下存注册表）。"""

    def __init__(self):
        self.q = QSettings("Bitseed", "MagnetViewer")

    def get(self, key: str):
        default = DEFAULTS[key]
        t = _TYPES.get(key, str)
        try:
            v = self.q.value(key, default, type=t)
        except TypeError:
            return default
        if key == "proxy_pass":
            # 注册表里存的是 DPAPI 密文（dpapi:v1:…），旧明文原样返回
            return secretbox.unprotect(str(v or ""))
        return v

    def set(self, key: str, value):
        if key == "proxy_pass":
            # 凭据不明文落注册表（REVIEW-2026-09 P1-3）；空串不加密
            value = secretbox.protect(str(value or ""))
        self.q.setValue(key, value)
        self.q.sync()

    def as_dict(self) -> dict:
        return {k: self.get(k) for k in DEFAULTS}

    def default_download_dir(self, cache_dir: str) -> str:
        """任务默认保存目录：设置 download_dir 优先，留空 = 缓存目录/downloads。"""
        return (str(self.get("download_dir") or "").strip()
                or os.path.join(str(cache_dir or ""), "downloads"))

    # ---- 派生配置 ----

    def proxy(self) -> dict:
        """返回给 libtorrent 用的代理配置（dict 形式，见 lt_proxy_settings）。"""
        return {
            "type": self.get("proxy_type"),
            "host": self.get("proxy_host"),
            "port": self.get("proxy_port"),
            "user": self.get("proxy_user"),
            "pass": self.get("proxy_pass"),
            "peer": self.get("proxy_peer"),
        }

    # ---- 解析历史（JSON 字符串存储，规避 QVariant 列表类型差异） ----

    def recent(self) -> list:
        try:
            return list(json.loads(self.q.value("recent", "[]")))
        except (TypeError, ValueError):
            return []

    def push_recent(self, item: str) -> list:
        """记录一次解析输入（去重、置顶、限量）；过长的磁力链不入库。"""
        item = (item or "").strip()
        if not item or len(item) > 2048:
            return self.recent()
        items = [x for x in self.recent() if x != item]
        items.insert(0, item)
        items = items[:RECENT_LIMIT]
        self.q.setValue("recent", json.dumps(items, ensure_ascii=False))
        self.q.sync()
        return items


def lt_proxy_settings(proxy: dict | None) -> dict:
    """应用代理配置 → libtorrent settings 键值对（纯函数，便于测试）。

    未启用代理或主机为空时显式回落为直连（proxy_type=0）。
    带账号密码时自动使用 socks5_pw / http_pw 类型。
    """
    p = proxy or {}
    t = str(p.get("type", "none") or "none").lower()
    host = str(p.get("host") or "").strip()
    if t not in ("socks5", "http") or not host:
        # 直连分支必须同时重置 tracker 连接设置——否则从代理切回直连后，
        # tracker 仍走旧代理（已实证的残留缺陷）
        return {"proxy_type": LT_PROXY_TYPES["none"],
                "proxy_peer_connections": False,
                "proxy_tracker_connections": False}
    user, password = str(p.get("user") or ""), str(p.get("pass") or "")
    if user:
        t = {"socks5": "socks5_pw", "http": "http_pw"}[t]
    return {
        "proxy_type": LT_PROXY_TYPES[t],
        "proxy_hostname": host,
        "proxy_port": int(p.get("port") or 1080),
        "proxy_peer_connections": bool(p.get("peer", True)),
        "proxy_tracker_connections": True,
        **({"proxy_username": user, "proxy_password": password} if user else {}),
    }
