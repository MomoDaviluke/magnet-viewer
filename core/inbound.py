"""入站能力自检（纯标准库，core 层不碰 Qt，所有探测可超时/可失败）。

背景（诊断 D-2026-09-18：下载慢 + 做种报错）
-------------------------------------------
用户本机内网 192.168.5.4、外网是真公网 119.98.134.164（路由器 NAT）。原客户端
硬编码关闭 UPnP/NAT-PMP，已改为可配置开关（见 core/session.build_session_settings 的
port_mapping）。但实测本机**路由器 UPnP 不可用**：SSDP M-SEARCH 到 239.255.255.250:1900
无设备响应；开启开关 45 秒内既无 portmap_alert 也无 portmap_error_alert。

结论：光有开关不够。用户在路由器上**手动端口转发**才能拿到入站端口。本模块是软件
能做的最后一件事——把这件事讲清楚、并给出精确参数（内网 IP + 端口 + 协议）。

设计约束（与项目铁律一致）
--------------------------
- **只用标准库**（socket / os / struct），不引入 psutil / netifaces 等新依赖；
- core 层**不 import Qt**：UI 经回调注入，core 不碰 Qt；
- 所有网络探测**可超时、可失败**，绝不向调用方抛异常（失败即回落为「不可用」）；
- 纯函数 + 详细中文 docstring（见 core/session._listen_host / core/config.lt_proxy_settings
  的同款风格）；返回纯 dict，便于单测与 UI 直接展示。

为什么用 UDP「盲连」探测本机 IP（而不是 getaddrinfo 主机名）
--------------------------------------------------------
`socket.connect(("8.8.8.8", 80))` 不会真正发包，只是让内核根据路由表选出「真正出站的
网卡」并返回其源地址——这恰好是用户该把端口转发到的那张网卡 IP，天然避开 VPN /
Hyper-V / TAP 等虚拟网卡的干扰。主机名解析在部分机器上只会解析回 127.0.1.1（回环），
故仅作兜底补充，二者并集去重。多网卡下本函数可能返回多个地址，hint 会逐个列出并提示
「转发到实际联网的那张网卡」——更合理的收敛策略见本模块末尾说明（未改 API 契约）。
"""
from __future__ import annotations

import socket

# SSDP 多播地址 / 端口（UPnP 设备发现固定值）
SSDP_ADDR = "239.255.255.250"
SSDP_PORT = 1900
# UPnP IGD（Internet Gateway Device）服务类型：M-SEARCH 只问「网关设备」，
# 路由器若支持 UPnP 才会应答此类 ST。
SSDP_MSEARCH = (
    "M-SEARCH * HTTP/1.1\r\n"
    f"HOST: {SSDP_ADDR}:{SSDP_PORT}\r\n"
    'MAN: "ssdp:discover"\r\n'
    "MX: 1\r\n"
    "ST: urn:schemas-upnp-org:device:InternetGatewayDevice:1\r\n"
    "\r\n"
)


# ----------------------------------------------------------------------------
# 内部小工具（与 session._listen_host 同款：纯函数 + 容错）
# ----------------------------------------------------------------------------

def _is_loopback_or_unspecified(ip: str) -> bool:
    """0.0.0.0（未定/未路由）/ 127.*（回环）都不该作为端口转发目标。"""
    return ip == "0.0.0.0" or ip.startswith("127.")


def _is_valid_ipv4(ip: str) -> bool:
    """严格四段 0-255 的 IPv4 字面量校验（防 getaddrinfo 偶发的脏串）。"""
    if not isinstance(ip, str) or ip.count(".") != 3:
        return False
    parts = ip.split(".")
    if len(parts) != 4:
        return False
    try:
        return all(0 <= int(p) <= 255 for p in parts)
    except ValueError:
        return False


def _is_usable_ipv4(ip: str) -> bool:
    """可用于端口转发的内网 IP：格式合法 + 非回环/未定地址。"""
    return _is_valid_ipv4(ip) and not _is_loopback_or_unspecified(ip)


# ----------------------------------------------------------------------------
# 对外 API
# ----------------------------------------------------------------------------

def local_ipv4s() -> list[str]:
    """枚举本机非回环 IPv4 地址（用于告诉用户把端口转发到哪个内网 IP）。

    实现：UDP「盲连」8.8.8.8 取默认路由网卡源地址（最贴近真实出站内网 IP，自动
    避开虚拟网卡）+ 主机名解析作兜底，二者并集去重。

    容错：任何一步异常都静默跳过，绝不抛给调用方；可能返回空列表（如完全离线且
    主机名只解析到回环），此时调用方应走 inbound_report 的「无可用内网 IP」分支。
    """
    found: set[str] = set()
    primary: str | None = None
    # 方法 1：UDP 盲连取真正出站的网卡 IP（不发包，仅读路由表选出的源地址）。
    # 这是**最该转发到的**那张网卡——默认路由所走，天然避开虚拟网卡，故记 primary
    # 并排在最前，让多网卡环境下 hint 能把它标为「推荐」。
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            if _is_usable_ipv4(ip):
                primary = ip
                found.add(ip)
        finally:
            s.close()
    except Exception:
        pass
    # 方法 2：主机名解析兜底（Windows 上通常解析到主 LAN IP；Linux 常为 127.0.1.1
    # 已被 _is_usable_ipv4 过滤，故不会污染结果）。此方法可能补出虚拟网卡 IP，
    # 仅作候选，不覆盖 primary。
    try:
        for info in socket.getaddrinfo(
                socket.gethostname(), None,
                family=socket.AF_INET, type=socket.SOCK_STREAM):
            ip = info[4][0]
            if _is_usable_ipv4(ip):
                found.add(ip)
    except Exception:
        pass
    # primary 置顶，其余稳定排序（避免每次顺序抖动干扰 UI 对比）
    rest = sorted(ip for ip in found if ip != primary)
    return [primary, *rest] if primary is not None else rest


def probe_upnp_igd(timeout: float = 3.0) -> bool:
    """SSDP M-SEARCH 探测局域网是否有 UPnP IGD 设备响应（有则 UPnP 可用）。

    发一条 ST=InternetGatewayDevice 的 M-SEARCH 到多播组，等待任意设备回包；
    回包里出现 "internetgatewaydevice" 即视为 UPnP 可用（True）。

    容错：超时 / 无应答 / 任何异常（建套接字失败、recv 中断）一律回落 False——
    不抛异常给调用方。timeout 可极短（如 0.01s）：仅 recvfrom 等 timeout 后返回
    False，不会卡住也不会崩。
    """
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        sock.settimeout(float(timeout))
        try:
            sock.sendto(SSDP_MSEARCH.encode("utf-8", "replace"),
                        (SSDP_ADDR, SSDP_PORT))
            # 收到非 IGD 设备的 SSDP 响应则继续等，直到超时
            while True:
                try:
                    data, _ = sock.recvfrom(4096)
                except (socket.timeout, OSError):
                    return False
                if data and b"internetgatewaydevice" in data.lower():
                    return True
        finally:
            sock.close()
    except Exception:
        return False
    return False


def inbound_report(listen_port: int = 6881,
                   upnp_available: bool | None = None,
                   timeout: float = 3.0) -> dict:
    """入站能力自检报告（纯数据，便于测试与 UI 展示）。

    参数
    ----
    listen_port   : 监听端口（默认 6881，与 build_session_settings 一致）。
    upnp_available: 入站 UPnP 是否可用。显式传 True/False 时直接采用（UI 会把真实
                    libtorrent 的 portmap 结果传进来）；为 None 时才自行跑
                    probe_upnp_igd(timeout) 探测。
    timeout       : 自行探测时的 SSDP 超时（秒）。

    返回 dict 的键
    --------------
    listen_port           : int，监听端口。
    local_ips             : list[str]，本机非回环 IPv4（可能空列表，绝不为 None）。
    upnp_available        : bool，UPnP 是否可用（已按入参解析）。
    need_manual_forwarding: bool，not upnp_available。
    ok                    : bool，与 upnp_available 同义（更直观的别名）。
    hint                  : str，面向用户的中文说明（IP/端口来自实参或探测，不写死）。
    """
    local_ips = local_ipv4s()
    # 入参为 None 才自行探测；显式传入则信任调用方（UI 已有真实 portmap 结果）
    upnp = bool(probe_upnp_igd(timeout)) if upnp_available is None else bool(upnp_available)
    report = {
        "listen_port": int(listen_port),
        "local_ips": list(local_ips),
        "upnp_available": upnp,
        "need_manual_forwarding": not upnp,
        "ok": upnp,
        "hint": "",
    }
    report["hint"] = _build_hint(report)
    return report


def _build_hint(report: dict) -> str:
    """根据报告拼出面向用户的中文说明（IP/端口来自实参，绝不写死）。"""
    port = report.get("listen_port", 0)
    ips: list[str] = list(report.get("local_ips") or [])
    if report.get("upnp_available"):
        return ("路由器已响应 UPnP，可自动完成端口映射，无需手动转发；"
                "做种时他人可直接连入。")
    if ips:
        # 多内网 IP 时逐个列出，首个（primary，默认路由网卡）标「推荐」，
        # 其余为候选；明确提示转发到实际联网的那张网卡，避免用户误转发到
        # Docker / Hyper-V / VMware 等虚拟适配器 IP。
        parts = []
        for i, ip in enumerate(ips):
            prefix = "内网 " if i == 0 else "（候选）内网 "
            tag = "（推荐）" if i == 0 and len(ips) > 1 else ""
            parts.append(f"{prefix}{ip}:{port}{tag}")
        inner = "；".join(parts)
        tail = ("——多网卡环境：请转发到标「推荐」的那张实际联网网卡 IP，"
                "勿转发到 Docker/Hyper-V/VMware 等虚拟适配器" if len(ips) > 1 else "")
        return (f"路由器未响应 UPnP（SSDP 无应答），需在路由器管理页做手动端口转发："
                f"外网端口 {port} → {inner}，协议 TCP+UDP。"
                f"转发后重启本程序，做种时他人才可连入。{tail}")
    return (f"路由器未响应 UPnP（SSDP 无应答），且在本地未探测到可用的内网 IPv4 地址。"
            f"请在路由器管理页做手动端口转发：外网端口 {port} → 本机监听端口 {port}，"
            f"协议 TCP+UDP；转发后重启本程序，做种时他人才可连入。")


def format_hint(report: dict) -> str:
    """把报告渲染成一段可直接显示在 UI 上的中文文本（多行用 \\n）。

    容错：report 为 None / 缺键 / 拼装异常时，最多只回落为原 hint 字符串，
    绝不抛异常（UI 线程调用，错误不能冒泡到界面）。
    """
    try:
        r = report if isinstance(report, dict) else {}
        port = r.get("listen_port", 0)
        upnp = bool(r.get("ok", r.get("upnp_available", False)))
        lines = [
            "入站连接自检：",
            "本机内网 IP：" + ("、".join(r.get("local_ips") or []) or "（未探测到）"),
            f"监听端口：{port}",
            "UPnP 自动映射：" + ("可用" if upnp else "不可用"),
            "需手动端口转发：" + ("是" if r.get("need_manual_forwarding") else "否"),
            "",
            str(r.get("hint", "")),
        ]
        return "\n".join(lines)
    except Exception:
        try:
            return str((report or {}).get("hint", ""))
        except Exception:
            return ""
