"""core/inbound.py 专项验收（纯标准库，不依赖 libtorrent / Qt）。

与 session_test.py 同款框架（test_support.Checker）：
- ck.check(cond, msg) 累积 OK/FAIL；
- ck.report() 打印「OK N 项，FAIL M 项」并返回退出码 0/1/2；
- 结尾 sys.exit(main())。

退出码约定：0=通过，1=失败。本模块不依赖外部资源，不会 SKIP。

覆盖（团队要求）：
- local_ipv4s：返回地址非 127.* 且合法 IPv4，不抛异常；
- probe_upnp_igd：timeout 极短（0.01s）也安全返回 bool，不抛异常；
- inbound_report(False)：need_manual_forwarding=True 且 hint 含实际内网 IP 与端口；
- inbound_report(True)：need_manual_forwarding=False 且 hint 表达「可自动映射」；
- 非默认端口（51413）：hint 出现 51413 而非 6881（抓写死端口的 bug）；
- upnp_available=None：自行探测，返回类型仍为 bool 且不崩；
- format_hint：对两种报告均返回非空 str。
"""
from __future__ import annotations

import os
import sys

# 让 `from core import inbound` 与 `import test_support` 都可解析：
# 本文件在 core/，项目根需同时入路径。
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.dirname(_HERE))

import test_support as ts
from core import inbound


def _valid_ipv4(ip: str) -> bool:
    if not isinstance(ip, str) or ip.count(".") != 3:
        return False
    try:
        return all(0 <= int(p) <= 255 for p in ip.split("."))
    except ValueError:
        return False


def section_local_ips(ck: "ts.Checker") -> None:
    ck.section("local_ipv4s：枚举非回环 IPv4，不抛异常")
    try:
        ips = inbound.local_ipv4s()
    except Exception as e:
        ck.check(False, f"local_ipv4s() 不应抛异常：{e}")
        return
    ck.check(isinstance(ips, list), "返回值为 list[str]")
    ck.check(ips is not None, "返回值不为 None")
    ck.check(all(not ip.startswith("127.") for ip in ips),
            "每个地址都不是 127.*（回环）")
    ck.check(all(_valid_ipv4(ip) for ip in ips),
            "每个地址都是合法 IPv4 格式")
    ck.check(len(ips) >= 1,
            f"本环境至少探测到一个可用内网 IPv4（实得 {ips}）")


def section_probe(ck: "ts.Checker") -> None:
    ck.section("probe_upnp_igd：极短超时也能安全返回 bool")
    try:
        r = inbound.probe_upnp_igd(0.01)
    except Exception as e:
        ck.check(False, f"probe_upnp_igd(0.01) 不应抛异常：{e}")
        return
    ck.check(isinstance(r, bool), "返回类型为 bool（极短超时也不崩）")


def section_report_false(ck: "ts.Checker") -> None:
    ck.section("inbound_report(upnp_available=False)：需手动转发 + 含实际 IP/端口")
    try:
        rep = inbound.inbound_report(6881, upnp_available=False)
    except Exception as e:
        ck.check(False, f"inbound_report(False) 不应抛异常：{e}")
        return
    ck.check(rep["need_manual_forwarding"] is True,
            "need_manual_forwarding 为 True")
    ck.check(rep["ok"] is False and rep["upnp_available"] is False,
            "ok / upnp_available 同义且为 False")
    ck.check("6881" in rep["hint"], "hint 含端口 6881")
    # 断言 hint 含「实际探测到的」内网 IP（取报告自己的 local_ips，不写死）
    ips = rep.get("local_ips") or []
    if ips:
        ck.check(ips[0] in rep["hint"],
                f"hint 含实际探测到的内网 IP（{ips[0]}）")
    else:
        ck.check(False, "local_ips 为空，无法断言 hint 含内网 IP")


def section_report_true(ck: "ts.Checker") -> None:
    ck.section("inbound_report(upnp_available=True)：可自动映射")
    try:
        rep = inbound.inbound_report(6881, upnp_available=True)
    except Exception as e:
        ck.check(False, f"inbound_report(True) 不应抛异常：{e}")
        return
    ck.check(rep["need_manual_forwarding"] is False,
            "need_manual_forwarding 为 False")
    ck.check(rep["ok"] is True, "ok 为 True")
    ck.check("自动" in rep["hint"] or "UPnP" in rep["hint"],
            "hint 表达「已可自动映射」")


def section_alt_port(ck: "ts.Checker") -> None:
    ck.section("非默认端口 51413：hint 出现 51413 而非 6881（抓写死端口）")
    try:
        rep = inbound.inbound_report(51413, upnp_available=False)
    except Exception as e:
        ck.check(False, f"inbound_report(51413) 不应抛异常：{e}")
        return
    ck.check("51413" in rep["hint"], "hint 含 51413")
    ck.check("6881" not in rep["hint"], "hint 不含写死的 6881")


def section_probe_self(ck: "ts.Checker") -> None:
    ck.section("upnp_available=None：自行探测，返回类型仍为 bool 且不崩")
    try:
        rep = inbound.inbound_report(6881, upnp_available=None, timeout=0.5)
    except Exception as e:
        ck.check(False, f"inbound_report(None) 不应抛异常：{e}")
        return
    ck.check(isinstance(rep["upnp_available"], bool),
            "upnp_available 解析为 bool")
    ck.check(isinstance(rep["need_manual_forwarding"], bool),
            "need_manual_forwarding 为 bool")
    ck.check(isinstance(rep["hint"], str) and rep["hint"],
            "hint 为非空 str")


def section_format_hint(ck: "ts.Checker") -> None:
    ck.section("format_hint：两种报告均返回非空 str")
    try:
        r_false = inbound.inbound_report(6881, upnp_available=False)
        r_true = inbound.inbound_report(6881, upnp_available=True)
        h_false = inbound.format_hint(r_false)
        h_true = inbound.format_hint(r_true)
    except Exception as e:
        ck.check(False, f"format_hint 不应抛异常：{e}")
        return
    ck.check(isinstance(h_false, str) and h_false.strip(),
            "format_hint(手动转发报告) 返回非空 str")
    ck.check(isinstance(h_true, str) and h_true.strip(),
            "format_hint(UPnP 报告) 返回非空 str")


def main() -> int:
    ck = ts.Checker("inbound_test（入站能力自检，纯标准库）")
    ck.section("core/inbound.py 专项验收（不启真会话 / 不依赖 libtorrent·Qt）")
    section_local_ips(ck)
    section_probe(ck)
    section_report_false(ck)
    section_report_true(ck)
    section_alt_port(ck)
    section_probe_self(ck)
    section_format_hint(ck)
    return ck.report()


if __name__ == "__main__":
    sys.exit(main())
