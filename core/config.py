"""应用设置：QSettings 持久化 + libtorrent 代理配置映射。"""
from __future__ import annotations

import json
import os

from PySide6.QtCore import QSettings

from core import secretbox
from core.cache_mode import PREVIEW_CACHE_CONVERT
from core.logutil import log_warning

RECENT_LIMIT = 15

# 键按领域分组声明（Wave 3 任务 2.2 权衡：既有键**集中声明**在本表、逐字冻结
# ——把注册语句物理搬进 session.py 等领域模块会引入 config→领域→config 的
# 导入环风险，且「未 import 即键缺失」的隐式时序耦合比集中声明更难排查。
# 新增键请走 register_config_key()（领域模块启动时调用），本表只留历史契约。）
DEFAULTS: dict = {
    # ---- 网络 / 代理 ----
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
    # 下次关闭预览时生效（stop_preview 现读现判，审查 Minor-8 修正措辞）；
    # 值域即两常量（cache_mode.py），不新增校验逻辑。
    "logging_enabled": True,       # 运行日志开关（core.logutil；关闭后全部记录降为空操作）
    "ui_theme": "light",           # 界面主题：light（默认浅色）| dark | system（跟随系统）
                                   # 保存设置即热切换（ui.theme.apply_theme），无需重启
    # 入站能力（诊断 D-2026-09-18：下载慢 + 做种报错）
    "port_mapping": False,         # UPnP/NAT-PMP 端口映射。默认关闭（只收不做种，
                                   # 减暴露面）；需做种或需要入站连接时开启——无
                                   # 入站端口会连不上同为 NAT 后的 peer，且做种时
                                   # 无人能连入而报错。保存设置即热生效。
    "listen_interface": "",        # 出口网卡 IP（多网卡/VPN 环境显式绑定，防
                                   # libtorrent 从虚拟网卡绕行出境）。空 = 0.0.0.0
                                   # 全网卡监听；该值在会话启动时固定，需重启生效。
    "extra_trackers": False,       # 追加 HTTP/HTTPS 兜底 tracker（UDP 受阻网络的
                                   # 第二条 peer 发现通道）。默认关闭：无 tr= 的
                                   # 磁力链仅注入既有 5 条 udp tracker。
                                   # 下次添加任务时生效。
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
    "port_mapping": bool,
    "extra_trackers": bool,
}

# libtorrent settings_pack::proxy_type_t 的整型值（2.1.x 仍是稳定枚举）
LT_PROXY_TYPES = {"none": 0, "socks4": 1, "socks5": 2,
                  "socks5_pw": 3, "http": 4, "http_pw": 5}

# key -> callable(value) -> bool；由 register_config_key 填充。
# 只在**读取侧**（AppConfig.get）生效：写入侧不设卡，设置面板才能把
# 「编辑框里尚未定型的中间值」照常落盘（校验失败的值下次读到时回落默认）。
_VALIDATORS: dict = {}


def _infer_type(default) -> type | None:
    """从默认值推断 QSettings 读取类型（bool 必须先判：bool 是 int 子类）。"""
    if isinstance(default, bool):
        return bool
    if isinstance(default, int):
        return int
    if isinstance(default, str):
        return str
    return None


def register_config_key(key: str, default, validator=None) -> None:
    """注册新配置键（Wave 3 任务 2.2：领域模块启动时声明自己的键）。

    - ``default`` 写入 DEFAULTS（AppConfig.get 的回落值、as_dict 的键集来源）；
      能推断出 bool/int/str 时同步登记 _TYPES（QSettings 读取类型），
      其余类型沿用 get 的 str 兜底——与既有未登记键行为一致。
    - ``validator`` 可选：``value -> bool``。非法值在 get 时回落 default
      并 log_warning（配置层容错纪律：**绝不抛异常打断 UI**）。
    - **重复注册同键：拒绝，首注册者胜**，log_warning 留痕。理由：若允许
      覆盖，DEFAULTS 取值将取决于领域模块的 import 顺序（隐式时序耦合），
      同一键在不同启动路径下默认值不同——比报错更难排查；拒绝让冲突在
      注册点显性暴露，调用方应改用新键名或复用既有键。
    - 参数误用（空键名 / validator 不可调用）直接抛错：这是编程错误，
      必须在注册点炸，而不是拖到每次 get 时才静默降级。
    """
    if not key or not isinstance(key, str):
        raise ValueError(f"配置键名必须是非空字符串：{key!r}")
    if key in DEFAULTS:
        log_warning("config.register",
                    f"配置键 {key!r} 已注册：拒绝重复注册（首注册者胜，"
                    f"现有默认值 {DEFAULTS[key]!r} 保持不变）")
        return
    if validator is not None and not callable(validator):
        raise TypeError(f"validator 必须可调用：{validator!r}")
    DEFAULTS[key] = default
    inferred = _infer_type(default)
    if inferred is not None:
        _TYPES[key] = inferred
    if validator is not None:
        _VALIDATORS[key] = validator


def _validate(key: str, value, default):
    """读取侧校验：无 validator 原样返回（既有键零行为变化）；
    validator 拒绝或自身抛异常都回落 default 并留痕。"""
    fn = _VALIDATORS.get(key)
    if fn is None:
        return value
    try:
        ok = bool(fn(value))
    except Exception as e:
        # 兜底（有意宽泛）：validator 是注册方注入的回调，异常类型不可枚举；
        # 「校验器自己坏了」按无法确认合法处理 → 回落 default（容错纪律）
        log_warning("config.validator", f"{key} 校验器抛异常，按非法处理：{e}")
        return default
    if ok:
        return value
    log_warning("config.validator",
                f"{key} 值 {value!r} 被校验器拒绝，回落默认 {default!r}")
    return default


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
            # （该键不参与 validator 校验：校验密文无意义，行为逐字保留）
            return secretbox.unprotect(str(v or ""))
        return _validate(key, v, default)

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
