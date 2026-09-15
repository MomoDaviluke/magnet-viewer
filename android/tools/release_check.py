#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""安卓发布包门禁（RB-3：签名 / 16KB 页对齐 / ABI 收窄 / 产物对账）。

为什么要有这个脚本：`app/build.gradle.kts` 在**找不到 `keystore.properties` 时故意不注册签名配置**
（干净的 CI checkout / 新机器上 release 退化成未签名包，而不是让整个构建红）。这个取舍对 CI 是对的，
但对**交付**是个陷阱：谁都可以 `assembleRelease` 出一只没签名（或开发签名）的包然后发出去。
本脚本把「发布包必须满足什么」写成可机器判定的四项，交给发布前那一步跑：**红了就不许发**。

用法
    python android/tools/release_check.py                      # 检查默认 release APK
    python android/tools/release_check.py <apk 路径>
    python android/tools/release_check.py --allow-dev-cert     # 本地冒烟：放行开发签名
    python android/tools/release_check.py --self-test          # 只跑内置自检（不需要 SDK）
    python android/tools/release_check.py --build-tools <目录>  # 显式指定 build-tools

检查项（退出码 0 = 全过 / 1 = 有 FAIL / 2 = 环境缺工具，显式 SKIP）
    R1 签名：`apksigner verify --print-certs` → **恰好 1 个签名者**；证书 DN 不得是开发凭据
           （`MagnetViewer Dev`，见 `docs`/README 的「换成自己的凭据」）。
           未签名包 → FAIL（装不上，或装上了后续无法覆盖升级）。
    R2 16KB 页对齐：`zipalign -c -P 16 -v 4` → `Verification successful`
           （2027-02-01 起 Play 对 targetSdk 35+ 强制；Android 15+ 的 16KB 页设备也要求）。
    R3 ABI 收窄：APK 内 `lib/` 只允许 `arm64-v8a`（release 不该夹带 x86_64 那份 ~16MB 的 .so）。
    R4 产物对账：打印 sha256 与字节数（与 `dist/` 交付副本逐位比对用）。

设计取舍
    - 只用标准库（zipfile / subprocess / hashlib），不引第三方依赖 —— CI 与干净机器都能跑。
    - 找不到 apksigner/zipalign 时**不猜、不静默放过**：打印 SKIP 理由并返回 2
      （项目既有约定：跳过 ≠ 通过；见桌面 `qt_stream_open_test` 的退出码 2 语义）。
    - 开发签名只按**证书 DN 里的项目标记**判定（不是按文件路径/时间），换正式凭据后自动放行。
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

# 开发用 keystore 的证书标记（`RELEASE-TEST-2026-09-15.md` 实测 DN：
#   CN=MagnetViewer Dev, OU=Android, O=Bitseed, L=Beijing, ST=Beijing, C=CN）
# 命中即视为「还没换成正式凭据」→ FAIL（除非 --allow-dev-cert）。
DEV_CERT_MARKERS = ("MagnetViewer Dev",)

# release 只允许这一套 ABI（与 app/build.gradle.kts 的 release.ndk.abiFilters 对齐）
REQUIRED_ABIS = ("arm64-v8a",)
FORBIDDEN_ABIS = ("x86", "x86_64", "armeabi-v7a")

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_APK = REPO_ROOT / "android/app/build/outputs/apk/release/app-release.apk"

OK = "OK"
FAIL = "FAIL"
SKIP = "SKIP"

# ------------------------------------------------------------------ 纯函数（可自检，不碰 SDK）


def parse_signer_count(apksigner_output: str) -> int:
    """从 `apksigner verify --print-certs` 输出里数签名者个数。

    apksigner 对每个签名者打印一行 `Signer #1 certificate DN: …`；
    对未签名包则输出 `DOES NOT VERIFY` / `Missing … signature` 等（签名者数 = 0）。
    """
    return len(re.findall(r"^Signer #\d+ certificate DN:", apksigner_output, flags=re.MULTILINE))


def is_dev_cert(apksigner_output: str, markers=DEV_CERT_MARKERS) -> bool:
    """证书 DN 里是否命中开发凭据标记。"""
    for line in apksigner_output.splitlines():
        if not line.strip().startswith("Signer #"):
            continue
        for m in markers:
            if m in line:
                return True
    return False


def check_abis(lib_entries: list[str]) -> tuple[bool, list[str]]:
    """`lib/` 下的 ABI 是否满足「只允许 REQUIRED_ABIS」。

    返回 (是否通过, 问题说明列表)。APK 里没有原生库也算不通过 —— 本应用的核心
    引擎就是 libtorrent4j 的 .so，少了它装上也跑不起来。
    """
    abis = set()
    for name in lib_entries:
        parts = name.split("/")
        if len(parts) >= 2 and parts[0] == "lib":
            abis.add(parts[1])
    problems: list[str] = []
    if not abis:
        problems.append("APK 内没有 lib/<abi>/ 原生库（libtorrent4j 的 .so 缺失？）")
        return False, problems
    missing = [a for a in REQUIRED_ABIS if a not in abis]
    if missing:
        problems.append("缺少必需的 ABI：%s" % ", ".join(missing))
    extra = sorted(a for a in abis if a not in REQUIRED_ABIS)
    if extra:
        problems.append("夹带了不该发布的 ABI：%s（release 只出 %s）" % (", ".join(extra), "/".join(REQUIRED_ABIS)))
    return not problems, problems


def find_build_tools(sdk_home: Path | None, explicit: Path | None) -> Path | None:
    """定位 build-tools 目录（取版本号最大的那个）；找不到返回 None。"""
    if explicit is not None:
        return explicit if explicit.is_dir() else None
    if sdk_home is None or not sdk_home.is_dir():
        return None
    bt = sdk_home / "build-tools"
    if not bt.is_dir():
        return None

    def key(p: Path):
        return [int(x) if x.isdigit() else 0 for x in re.split(r"[.\-]", p.name)]

    dirs = sorted((d for d in bt.iterdir() if d.is_dir()), key=key, reverse=True)
    return dirs[0] if dirs else None


def _tool(build_tools: Path, stem: str) -> str | None:
    for suffix in (".exe", ".bat", ""):
        cand = build_tools / (stem + suffix)
        if cand.is_file():
            return str(cand)
    return None


def run(cmd: list[str]) -> tuple[int, str]:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, errors="replace")
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except FileNotFoundError as e:
        return 127, str(e)
    except OSError as e:  # pragma: no cover - 平台相关
        return 126, str(e)


# ------------------------------------------------------------------ 检查主体


def check(apk: Path, build_tools: Path | None, allow_dev_cert: bool, verbose: bool) -> tuple[int, list[tuple[str, str, str]]]:
    results: list[tuple[str, str, str]] = []  # (项, 状态, 说明)

    if not apk.is_file():
        results.append(("R0 APK 存在", FAIL, "找不到 %s —— 先跑 :app:assembleRelease" % apk))
        return 1, results
    results.append(("R0 APK 存在", OK, str(apk)))

    if build_tools is None:
        results.append(("R1 签名", SKIP, "找不到 build-tools（设 ANDROID_HOME 或用 --build-tools 指定）"))
        results.append(("R2 16KB 页对齐", SKIP, "同上"))
    else:
        apksigner = _tool(build_tools, "apksigner")
        zipalign = _tool(build_tools, "zipalign")

        # ---- R1 签名 ----
        if apksigner is None:
            results.append(("R1 签名", SKIP, "%s 下没有 apksigner" % build_tools))
        else:
            rc, out = run([apksigner, "verify", "--print-certs", str(apk)])
            if verbose:
                print(out)
            n = parse_signer_count(out)
            if rc != 0 or n == 0:
                results.append(("R1 签名", FAIL, "未签名 / 校验失败（apksigner rc=%d，签名者 %d 个）—— 装不上，也不能覆盖升级" % (rc, n)))
            elif n != 1:
                results.append(("R1 签名", FAIL, "签名者 %d 个（发布包必须恰好 1 个）" % n))
            elif is_dev_cert(out) and not allow_dev_cert:
                results.append(("R1 签名", FAIL, "仍是**开发用凭据**（%s）—— 正式分发前必须换成自己的 keystore；本地冒烟可加 --allow-dev-cert" % "/".join(DEV_CERT_MARKERS)))
            else:
                dn = next((l.split("certificate DN:", 1)[1].strip() for l in out.splitlines() if l.strip().startswith("Signer #1 certificate DN:")), "?")
                tag = "（开发凭据，已按 --allow-dev-cert 放行）" if is_dev_cert(out) else ""
                results.append(("R1 签名", OK, "1 个签名者：%s%s" % (dn, tag)))

        # ---- R2 16KB 页对齐 ----
        if zipalign is None:
            results.append(("R2 16KB 页对齐", SKIP, "%s 下没有 zipalign" % build_tools))
        else:
            rc, out = run([zipalign, "-c", "-P", "16", "-v", "4", str(apk)])
            if rc == 0 and "Verification successful" in out:
                results.append(("R2 16KB 页对齐", OK, "Verification successful"))
            else:
                results.append(("R2 16KB 页对齐", FAIL, "未达标（zipalign rc=%d）：%s" % (rc, out.strip().splitlines()[-1] if out.strip() else "")))

    # ---- R3 ABI 收窄 ----
    try:
        with zipfile.ZipFile(apk) as z:
            lib_entries = [n for n in z.namelist() if n.startswith("lib/")]
    except zipfile.BadZipFile as e:
        results.append(("R3 ABI 收窄", FAIL, "读不了 APK：%s" % e))
        lib_entries = None  # type: ignore[assignment]
    if lib_entries is not None:
        good, problems = check_abis(lib_entries)
        results.append(("R3 ABI 收窄", OK if good else FAIL, "只含 %s" % "/".join(REQUIRED_ABIS) if good else "；".join(problems)))

    # ---- R4 产物对账 ----
    h = hashlib.sha256()
    with apk.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    results.append(("R4 产物对账", OK, "sha256=%s  size=%d 字节" % (h.hexdigest(), apk.stat().st_size)))

    code = 0
    for _, st, _ in results:
        if st == FAIL:
            code = 1
            break
    else:
        if any(st == SKIP for _, st, _ in results):
            code = 2
    return code, results


# ------------------------------------------------------------------ 自检（不需要 SDK / APK）


def self_test() -> int:
    """内置自检：纯函数 + 本脚本自身可跑（CI 里没有 SDK 时也能验证逻辑没坏）。"""
    cases = 0
    bad = 0

    def expect(cond: bool, what: str):
        nonlocal cases, bad
        cases += 1
        if not cond:
            bad += 1
            print("  FAIL %s" % what)

    signed_one = "Verification successful\nSigner #1 certificate DN: CN=MagnetViewer Dev, OU=Android\n"
    signed_two = signed_one + "Signer #2 certificate DN: CN=Other\n"
    unsigned = "DOES NOT VERIFY\nERROR: Missing META-INF signature files\n"
    formal = "Verification successful\nSigner #1 certificate DN: CN=Bitseed Release, O=Bitseed, C=CN\n"

    expect(parse_signer_count(signed_one) == 1, "数出 1 个签名者")
    expect(parse_signer_count(signed_two) == 2, "数出 2 个签名者")
    expect(parse_signer_count(unsigned) == 0, "未签名 → 0")
    expect(is_dev_cert(signed_one), "识别开发凭据")
    expect(not is_dev_cert(formal), "正式凭据不误判")

    good, _ = check_abis(["lib/arm64-v8a/libtorrent4j.so", "META-INF/x", "classes.dex"])
    expect(good, "只含 arm64 通过")
    bad1, p1 = check_abis(["lib/arm64-v8a/a.so", "lib/x86_64/a.so"])
    expect(not bad1 and "x86_64" in "".join(p1), "夹带 x86_64 → 拒")
    bad2, p2 = check_abis(["classes.dex"])
    expect(not bad2 and "没有 lib/" in "".join(p2), "没有原生库 → 拒")
    bad3, p3 = check_abis(["lib/x86_64/a.so"])
    expect(not bad3 and "缺少必需" in "".join(p3), "缺 arm64 → 拒")

    expect(find_build_tools(Path("Z:/definitely-not-here"), None) is None, "SDK 不存在 → 返回 None")

    print("自检：%d 项 / %d 失败" % (cases, bad))
    return 0 if bad == 0 else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="安卓发布包门禁（签名 / 16KB 对齐 / ABI / 产物对账）")
    ap.add_argument("apk", nargs="?", default=str(DEFAULT_APK), help="待检 APK（默认 release 产物）")
    ap.add_argument("--allow-dev-cert", action="store_true", help="放行开发签名（仅供本地冒烟，交付不许用）")
    ap.add_argument("--build-tools", default=None, help="显式指定 build-tools 目录")
    ap.add_argument("--self-test", action="store_true", help="只跑内置自检（不需要 SDK/APK）")
    ap.add_argument("--verbose", action="store_true", help="打印 apksigner 原始输出")
    args = ap.parse_args(argv)

    if args.self_test:
        return self_test()

    sdk_home = None
    for var in ("ANDROID_HOME", "ANDROID_SDK_ROOT"):
        v = os.environ.get(var)
        if v:
            sdk_home = Path(v)
            break
    if sdk_home is None:
        # 退化到 android/local.properties 的 sdk.dir（与 Gradle 同源）
        lp = REPO_ROOT / "android/local.properties"
        if lp.is_file():
            for line in lp.read_text(encoding="utf-8", errors="replace").splitlines():
                if line.strip().startswith("sdk.dir="):
                    sdk_home = Path(line.split("=", 1)[1].strip().replace("\\\\", "\\"))
                    break
    explicit = Path(args.build_tools) if args.build_tools else None
    build_tools = find_build_tools(sdk_home, explicit)

    apk = Path(args.apk)
    code, results = check(apk, build_tools, args.allow_dev_cert, args.verbose)

    print("=== 安卓发布门禁：%s ===" % apk)
    for name, st, msg in results:
        print("[%s] %s —— %s" % (st, name, msg))
    tail = {0: "全过（可以发布）", 1: "有 FAIL（不许发布）", 2: "有跳过项（跳过 ≠ 通过）"}[code]
    print("=== 结论：%s ===" % tail)
    return code


if __name__ == "__main__":
    sys.exit(main())
