# -*- coding: utf-8 -*-
"""代理可用性自检（诊断 D-2026-09-18 加固：下载极慢 + 做种报错）。

背景
----
客户端允许把 ``proxy_host`` 设成任意地址并**静默生效**。实测一次真实事故：
``proxy_type=socks5`` + ``proxy_host=1.2.3.4``（占位黑洞）+ ``proxy_peer=True``
⇒ :func:`core.config.lt_proxy_settings` 同时打开 ``proxy_peer_connections`` 与
``proxy_tracker_connections`` ⇒ peer 连接、tracker 连接、**以及 DHT bootstrap
的域名解析**（引导节点是主机名，SOCKS5 下解析被委派给代理）三条通道全灭。
实测 A/B（唯一差异是代理）：无代理 60 秒 310MB / 末段 8.9MB/s；
带该代理 60 秒 **0KB**、DHT 节点 0。

更糟的是症状具有**误导性**：90 秒后客户端弹出的是
「获取元数据超时：该资源可能已无做种/无在线 Peer」——文案把根因指向资源，
用户会去换种子，而真正该改的是代理设置。

本模块提供两件事
----------------
- :func:`probe`：SOCKS5 / HTTP 代理的**真实可达性**探测（TCP 连通 + SOCKS5 握手）
- :func:`warning_text` / :func:`hint_text`：面向用户的中文文案

纯标准库（core 层不 import Qt），无全局状态，可被单测直接注入替身。
"""
from __future__ import annotations

import socket

# 探测超时（秒）。取 2s：既覆盖跨境代理的正常 RTT，又不会让「黑洞地址」
# （实测 1.2.3.4:1080 为 TCP 6s 超时）拖住太久——探测在后台线程跑，
# 2s 是「宁可少等，让用户尽快看到明确报错」的取值。
DEFAULT_TIMEOUT = 2.0

# SOCKS5 版本字节与「无认证」方法（RFC 1928 §3）
_SOCKS5_VERSION = 0x05
_SOCKS5_NO_AUTH = 0x00
_SOCKS5_NO_ACCEPTABLE = 0xFF


def is_configured(proxy: dict | None) -> bool:
    """该配置是否**真的启用了代理**（决定要不要探测/提示）。

    与 :func:`core.config.lt_proxy_settings` 的「直连回落」判据保持同源：
    ``type`` 为空/none、或 ``host`` 为空，都视为直连。
    """
    p = proxy or {}
    kind = str(p.get("type", "none") or "none").strip().lower()
    host = str(p.get("host") or "").strip()
    return kind != "none" and bool(host)


def address_text(proxy: dict | None) -> str:
    """``socks5://1.2.3.4:1080`` 形式的人类可读地址（用于文案与日志）。"""
    p = proxy or {}
    kind = str(p.get("type", "none") or "none").strip().lower()
    host = str(p.get("host") or "").strip() or "?"
    try:
        port = int(p.get("port") or 0)
    except (TypeError, ValueError):
        port = 0
    return f"{kind}://{host}:{port}"


def probe(proxy: dict | None, timeout: float = DEFAULT_TIMEOUT
          ) -> tuple[bool, str]:
    """探测代理可达性，返回 ``(是否可用, 不可用原因)``。

    未配置代理时返回 ``(True, "")``——「没开代理」不是故障，调用方不该报错。

    对 SOCKS5 额外做一次**握手**（发送 ``05 01 00`` 期望 ``05 xx``）：
    仅 TCP 连通不足以判定可用，实测存在「端口开着但不是 SOCKS5 服务」的情况，
    那种配置同样会把 BT 流量送进死路。
    """
    p = proxy or {}
    if not is_configured(p):
        return True, ""
    kind = str(p.get("type") or "").strip().lower()
    host = str(p.get("host") or "").strip()
    try:
        port = int(p.get("port") or 0)
    except (TypeError, ValueError):
        return False, "端口不是合法数字"
    if port <= 0:
        return False, "端口未填写或非法"

    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except OSError as e:
        return False, f"地址无法解析（{e}）"
    if not infos:
        return False, "地址无法解析"
    # getaddrinfo 的元素是 (family, type, proto, canonname, sockaddr) 五元组
    af, socktype, proto_, _canon, sockaddr = infos[0]

    s = socket.socket(af, socktype, proto_)
    s.settimeout(timeout)
    try:
        try:
            s.connect(sockaddr)
        except socket.timeout:
            return False, f"连接超时（{timeout:g} 秒内无响应）"
        except OSError as e:
            return False, f"连接失败（{e.strerror or e}）"

        if kind.startswith("socks5"):
            try:
                s.sendall(bytes((_SOCKS5_VERSION, 1, _SOCKS5_NO_AUTH)))
                resp = s.recv(2)
            except socket.timeout:
                return False, "SOCKS5 握手超时（端口开放但无响应）"
            except OSError as e:
                return False, f"SOCKS5 握手失败（{e.strerror or e}）"
            if len(resp) < 2 or resp[0] != _SOCKS5_VERSION:
                return False, "端口可连接但不是 SOCKS5 服务"
            if resp[1] == _SOCKS5_NO_ACCEPTABLE:
                return False, "SOCKS5 服务拒绝无认证方式（如需账号密码请一并填写）"
        return True, ""
    finally:
        try:
            s.close()
        except OSError:
            pass


def warning_text(proxy: dict | None, reason: str = "") -> str:
    """代理不可用时的完整告警文案（面向用户）。"""
    head = f"代理 {address_text(proxy)} 不可用"
    if reason:
        head += f"（{reason}）"
    return (head + "。peer 连接、tracker 连接与 DHT 引导会同时失效，"
                   "下载无法开始；请在「设置 → 网络与代理」中关闭代理，"
                   "或改为可用的代理地址。")


def hint_text(proxy: dict | None, reason: str = "") -> str:
    """追加到「元数据超时」文案后的提示；未配置代理时返回空串。

    ``reason`` 非空 = 本次启动自检已确认该代理不可达，此时文案要直接点明
    根因，避免用户按「资源没种」去换种子（这正是原事故的误导点）。
    """
    if not is_configured(proxy):
        return ""
    addr = address_text(proxy)
    if reason:
        return (f"；另：启动自检显示代理 {addr} 不可达（{reason}），"
                "这就是下载无法开始的原因——请先关闭或修正代理。")
    return (f"；另：当前已配置代理 {addr}，若其不可达会同时切断 "
            "peer/tracker/DHT，请一并检查。")
