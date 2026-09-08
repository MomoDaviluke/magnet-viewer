"""凭据保护：Windows DPAPI（CryptProtectData）对称封装。

威胁模型：注册表（HKCU\\Software\\Bitseed\\MagnetViewer）里不能存明文代理
密码——注册表可以被整支导出/拷贝到其他机器。DPAPI 绑定「当前 Windows 用户
+ 本机」，其他用户或异机拿到密文也解不开（REVIEW-2026-09 P1-3）。

存储格式：`dpapi:v1:<base64(blob)>`；无前缀 = 旧版明文（兼容直读，下次
保存时自然升级为密文）。加密失败时退回明文存储并留痕——功能可用性优先
于保密性升级；解密失败返回空串（密文损坏比崩溃好）。
"""
from __future__ import annotations

import base64
import ctypes
import os

from core.logutil import log_warning

PREFIX = "dpapi:v1:"
_CRYPTPROTECT_UI_FORBIDDEN = 0x1

class _DATA_BLOB(ctypes.Structure):
    # pbData 必须是 c_void_p 而非 c_char_p：密文是任意二进制，c_char_p 会在
    # 首个 NUL 处截断
    _fields_ = [("cbData", ctypes.c_uint), ("pbData", ctypes.c_void_p)]


if os.name == "nt":
    _crypt32 = ctypes.WinDLL("crypt32")
    _kernel32 = ctypes.WinDLL("kernel32")
    # 不设 argtypes 时 ctypes 按 c_int 传参，64 位指针会被截断 → 调用静默
    # 失败（首个验证用例就抓到了：protect 一直走「加密失败回退明文」分支）
    _BLOB = ctypes.POINTER(_DATA_BLOB)
    for _fn in (_crypt32.CryptProtectData, _crypt32.CryptUnprotectData):
        _fn.argtypes = [_BLOB, ctypes.c_wchar_p, ctypes.c_void_p,
                        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint, _BLOB]
        _fn.restype = ctypes.c_bool
    _kernel32.LocalFree.argtypes = [ctypes.c_void_p]
else:            # 非 Windows 直接退化直通（本项目实际只在 Windows 运行）
    _crypt32 = None
    _kernel32 = None


def _local_free(ptr) -> None:
    if ptr:
        _kernel32.LocalFree(ptr)


def _dpapi_raw(data: bytes, protect: bool) -> bytes:
    if _crypt32 is None:
        raise OSError("DPAPI 仅在 Windows 可用")
    in_buf = _DATA_BLOB(
        len(data), ctypes.cast(ctypes.create_string_buffer(data), ctypes.c_void_p))
    out = _DATA_BLOB()
    fn = (_crypt32.CryptProtectData if protect
          else _crypt32.CryptUnprotectData)
    if not fn(ctypes.byref(in_buf), None, None, None, None,
              _CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(out)):
        raise OSError(f"DPAPI {'CryptProtectData' if protect else 'CryptUnprotectData'} 失败")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        _local_free(out.pbData)


def protect(plain: str) -> str:
    """明文 → `dpapi:v1:<base64>`；空串原样返回；失败回退明文并留痕。"""
    plain = plain or ""
    if not plain or _crypt32 is None:
        return plain
    try:
        blob = _dpapi_raw(plain.encode("utf-8"), protect=True)
        return PREFIX + base64.b64encode(blob).decode("ascii")
    except Exception as e:
        log_warning("secretbox.protect", f"DPAPI 加密失败，回退明文存储: {e}")
        return plain


def unprotect(stored: str) -> str:
    """`dpapi:v1:` 前缀 → 解密；无前缀（旧明文）原样返回；失败返回空串。"""
    stored = stored or ""
    if not stored.startswith(PREFIX):
        return stored
    try:
        blob = base64.b64decode(stored[len(PREFIX):], validate=True)
        return _dpapi_raw(blob, protect=False).decode("utf-8")
    except Exception as e:
        log_warning("secretbox.unprotect", f"DPAPI 解密失败，返回空: {e}")
        return ""
