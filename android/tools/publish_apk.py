#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""安卓升级包一键发版链（2026-10-12）：构建 -> 门禁 -> dist 刷新 -> 上传 GitHub Release。

解决「发升级包」三缺口：
  1. Release 页不挂 APK（此前 release.yml 只出桌面件）；
  2. versionCode 硬编码 1（真机永远拒绝覆盖安装）；
  3. 发包全手工（构建/检查/上传分散多处）。

用法（仓库根目录执行）：
    python android/tools/publish_apk.py --version-name 1.1.1 --tag v1.1.1
    python android/tools/publish_apk.py --version-name 1.1.1 --tag v1.1.1 --skip-build   # 重传已构建包
    python android/tools/publish_apk.py --version-name 1.1.1 --tag v1.1.1 --no-upload    # 只本地构建+门禁

步骤：
  S1 构建    gradle :app:assembleRelease -PappVersionName=.. -PappVersionCode=..（正式签名取自
             android/app/keystore.properties，gitignored 不入库）
  S2 门禁    release_check.py **无 --allow-dev-cert**——签名/16KB 对齐/ABI/对账任一 FAIL 即中止
  S3 刷新    拷贝到 dist/MagnetViewer-<ver>-android-arm64.apk，打印 sha256
  S4 上传    git credential fill 取 token -> 查 Release id -> 上传资产
             MagnetViewer-<tag>-android-arm64.apk（同名先删后传，可重跑）；需传 --tag

versionCode 默认取 `git rev-list --count HEAD`（单调递增，满足升级语义）；可用 --version-code 覆盖。
凭据只走本机 Windows 凭据管理器，keystore 与口令绝不离开本机。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
APK_SRC = REPO / "android/app/build/outputs/apk/release/app-release.apk"
DIST = REPO / "dist"
API = "https://api.github.com"
UPLOAD = "https://uploads.github.com"
OWNER_REPO = "MomoDaviluke/magnet-viewer"


def run(cmd: list[str], **kw) -> int:
    print("+", " ".join(cmd), flush=True)
    return subprocess.call(cmd, **kw)


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def github_token() -> str:
    """复用项目既有通道：git credential fill 取本机存储的 GitHub token。"""
    out = subprocess.run(
        ["git", "credential", "fill"],
        input="protocol=https\nhost=github.com\n\n",
        capture_output=True, text=True,
    )
    for line in out.stdout.splitlines():
        if line.startswith("password="):
            return line.split("=", 1)[1]
    raise RuntimeError("git credential fill 未取到 token——请确认本机已存 GitHub 凭据")


def api_json(url: str, token: str, method: str = "GET", data: bytes | None = None,
             content_type: str = "application/vnd.github+json") -> tuple[int, dict | bytes]:
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Accept", content_type)
    if data is not None:
        req.add_header("Content-Type", content_type)
    try:
        with urllib.request.urlopen(req) as resp:
            body = resp.read()
            code = resp.status
    except urllib.error.HTTPError as e:
        body = e.read()
        code = e.code
    try:
        return code, json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return code, body


def upload_asset(token: str, release_id: int, tag: str, apk: Path) -> None:
    """上传资产。走 curl 而非 urllib：本机会话中 curl 是全天实测最稳的 HTTPS 通道，
    且 GitHub 资产上传对 Content-Type 敏感（urllib 实测 415×3，curl+octet-stream 稳定）。"""
    name = f"MagnetViewer-{tag}-android-arm64.apk"
    # 同名资产先删后传（可重跑；404 视为不存在）
    code, rel = api_json(f"{API}/repos/{OWNER_REPO}/releases/tags/{tag}", token)
    if code != 200:
        raise RuntimeError(f"找不到 Release {tag}（HTTP {code}）——请先推 tag 触发 release.yml")
    for a in rel.get("assets", []):
        if a["name"] == name:
            api_json(f"{API}/repos/{OWNER_REPO}/releases/assets/{a['id']}", token, method="DELETE")
            print(f"  已删除旧资产 {name}（重传）")
    size = apk.stat().st_size
    for attempt in (1, 2, 3):
        rc = subprocess.run(
            ["curl", "-s", "--max-time", "3600", "-X", "POST",
             "-H", f"Authorization: Bearer {token}",
             "-H", "Content-Type: application/octet-stream",
             "--data-binary", f"@{apk}",
             f"{UPLOAD}/repos/{OWNER_REPO}/releases/{release_id}/assets?name={name}",
             "-o", "/dev/null", "-w", "%{http_code}"],
            capture_output=True, text=True)
        code = rc.stdout.strip()
        if code == "201":
            print(f"  上传成功：{name}（{size:,} B）")
            return
        print(f"  上传失败 HTTP {code or rc.stderr[:80]}（第 {attempt}/3 次），重试…")
    raise RuntimeError(f"资产上传连续失败：{name}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--version-name", required=True, help="如 1.1.1（写入 APK versionName）")
    ap.add_argument("--version-code", default=None, help="缺省= git rev-list --count HEAD（单调递增）")
    ap.add_argument("--tag", default=None, help="目标 Release 的 tag（如 v1.1.1）；传了才上传")
    ap.add_argument("--skip-build", action="store_true", help="跳过 gradle，直接门禁+刷新+上传现有包")
    ap.add_argument("--no-upload", action="store_true", help="只构建+门禁+刷新 dist，不上传")
    args = ap.parse_args()

    if not args.tag and not args.no_upload:
        ap.error("上传需要 --tag（或显式 --no-upload）")

    vcode = args.version_code
    if vcode is None:
        vcode = subprocess.run(["git", "rev-list", "--count", "HEAD"], cwd=REPO,
                               capture_output=True, text=True).stdout.strip()
    print(f"=== 安卓发版链：versionName={args.version_name} versionCode={vcode} ===")

    # S1 构建（正式签名来自 android/app/keystore.properties；缺失时 build.gradle.kts
    # 会静默退化为未签名包——由 S2 门禁 R1 拦截，这里不重复判断）
    if not args.skip_build:
        gradle = Path(os.environ.get("LOCALAPPDATA", "")) / "Android/tools/gradle-9.1.0/bin/gradle.bat"
        if not gradle.exists():
            print("X 找不到本地 gradle.bat（见 android/ENV.md）")
            return 1
        rc = run([str(gradle), ":app:assembleRelease", "--no-build-cache",
                  f"-PappVersionName={args.version_name}", f"-PappVersionCode={vcode}"],
                 cwd=str(REPO / "android"))
        if rc != 0:
            print("X gradle 构建失败")
            return 1
    elif not APK_SRC.exists():
        print(f"X --skip-build 但找不到 {APK_SRC}")
        return 1

    # S2 门禁（硬门禁：无 --allow-dev-cert，红了就不许发）
    rc = run([sys.executable, str(REPO / "android/tools/release_check.py"), str(APK_SRC)])
    if rc != 0:
        print("X release_check 门禁未过——禁止发布")
        return 1

    # S3 dist 刷新
    DIST.mkdir(exist_ok=True)
    dest = DIST / f"MagnetViewer-{args.version_name}-android-arm64.apk"
    shutil.copyfile(APK_SRC, dest)
    print(f"=== dist 交付副本：{dest.name} ===")
    print(f"    sha256 = {sha256(dest)}")

    # S4 上传
    if args.no_upload:
        print("=== --no-upload：到此为止 ===")
        return 0
    token = github_token()
    code, rel = api_json(f"{API}/repos/{OWNER_REPO}/releases/tags/{args.tag}", token)
    if code != 200:
        print(f"X Release {args.tag} 不存在（HTTP {code}）——先推 tag 再跑本脚本")
        return 1
    upload_asset(token, rel["id"], args.tag, dest)
    print(f"=== 完成：https://github.com/{OWNER_REPO}/releases/tag/{args.tag} ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
