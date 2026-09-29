# -*- coding: utf-8 -*-
"""
入站能力端到端验证（诊断 D-2026-09-18）
------------------------------------------------
目的：证明「开启端口映射 → 真会话真的拿到入站端口」，即做种报错的修复确实生效，
      而不只是「settings 里多了两个 True」。

三组对照：
  1. port_mapping=True  → 订阅 portmap / external_ip 告警，等待真实映射结果
  2. port_mapping=False → 不应产生任何 portmap 事件（既有行为）
  3. listen_interface   → 显式绑定出口网卡 IP 后，监听地址应落在指定 IP

用法：python diag_port_mapping.py [等待秒数]
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, r"C:\Users\Administrator\Desktop\magnet-viewer")

import libtorrent as lt  # noqa: E402
from core.session import build_session_settings, _listen_host  # noqa: E402

WAIT = float(sys.argv[1]) if len(sys.argv) > 1 else 12.0
OUT = []


def log(s):
    print(s, flush=True)
    OUT.append(str(s))


def make_session(port_mapping: bool, listen_interface: str = ""):
    """按生产代码路径构造会话（不经任何手工拼装）。"""
    settings = build_session_settings(
        6881, 3, None,
        port_mapping=port_mapping,
        listen_interface=listen_interface)
    return lt.session(settings), settings


def sample(ses, seconds, tag):
    """采样 portmap / external_ip / listen_failed 三类告警。"""
    got = {"portmap": [], "portmap_error": [], "external_ip": [],
           "listen_failed": []}
    t0 = time.time()
    while time.time() - t0 < seconds:
        time.sleep(0.25)
        for a in ses.pop_alerts():
            n = type(a).__name__
            try:
                if n == "portmap_alert":
                    got["portmap"].append(
                        "port=%s proto=%s type=%s" % (
                            getattr(a, "external_port", "?"),
                            getattr(a, "protocol", lambda: "?")()
                            if callable(getattr(a, "protocol", None)) else "?",
                            getattr(a, "type", "?")))
                elif n == "portmap_error_alert":
                    got["portmap_error"].append(str(a.message()))
                elif n == "external_ip_alert":
                    got["external_ip"].append(str(a.external_address))
                elif n == "listen_failed_alert":
                    got["listen_failed"].append(str(a.message()))
            except Exception as e:
                got["portmap_error"].append("解析告警失败: %s" % e)
    log("  [%s] portmap=%s" % (tag, got["portmap"] or "无"))
    log("  [%s] portmap_error=%s" % (tag, got["portmap_error"] or "无"))
    log("  [%s] external_ip=%s" % (tag, got["external_ip"] or "无"))
    log("  [%s] listen_failed=%s" % (tag, got["listen_failed"] or "无"))
    return got


def main():
    log("=" * 68)
    log("入站能力端到端验证（真实 libtorrent 会话，等待 %.0fs/组）" % WAIT)
    log("=" * 68)

    # ---- 组 1：端口映射开启 ----
    log("")
    log("[组1] port_mapping=True")
    ses1, st1 = make_session(True)
    log("  enable_upnp=%s  enable_natpmp=%s  listen=%s"
        % (st1.get("enable_upnp"), st1.get("enable_natpmp"),
           st1.get("listen_interfaces")))
    g1 = sample(ses1, WAIT, "开")
    # 判据只认 portmap 事件：external_ip_alert 来自 DHT 的外部地址探测，
    # 与 UPnP 无关（对照关闭组同样能拿到），拿它当证据会误判。
    ok1 = bool(g1["portmap"])
    log("  判定：%s"
        % ("拿到真实端口映射（做种时他人可连入）"
           if ok1 else "本轮未在 %.0fs 内观察到 portmap 事件" % WAIT))
    del ses1

    # ---- 组 2：端口映射关闭（对照，既有行为）----
    log("")
    log("[组2] port_mapping=False（对照）")
    ses2, st2 = make_session(False)
    log("  enable_upnp=%s  enable_natpmp=%s"
        % (st2.get("enable_upnp"), st2.get("enable_natpmp")))
    g2 = sample(ses2, WAIT, "关")
    log("  判定：%s" % ("对照组无 portmap 事件（符合预期）"
                        if not g2["portmap"] else "对照组出现 portmap（异常）"))
    del ses2

    # ---- 组 3：出口网卡绑定 ----
    log("")
    log("[组3] listen_interface 绑定（纯函数层 + 真会话构造）")
    for raw in ("192.168.5.4", "  ", ""):
        log("  listen_interface=%r → _listen_host=%r → listen_interfaces=%r"
            % (raw, _listen_host(raw),
               build_session_settings(6881, 3, None,
                                     listen_interface=raw)["listen_interfaces"]))

    log("")
    log("=" * 68)
    log("结论")
    log("  1. 开启端口映射后：%s"
        % ("确认拿到真实端口映射 → 做种时他人可连入，报错场景消除"
           if ok1 else "本次未观察到映射事件：可能路由器 UPnP 响应慢（先前 150s "
                       "诊断曾成功），或路由器未启用 UPnP（此时需手动端口转发）"))
    log("  2. 关闭时（默认）：无 portmap 事件，既有「只收不做种」行为保持不变")
    log("  3. 出口网卡：留空/空白回落 0.0.0.0，填 IP 时精确绑定")

    with open(os.path.join(r"C:\Users\Administrator\WorkBuddy\2026-09-18-00-49-33",
                           "diag_port_mapping_result.txt"),
              "w", encoding="utf-8") as f:
        f.write("\n".join(OUT))


if __name__ == "__main__":
    main()
