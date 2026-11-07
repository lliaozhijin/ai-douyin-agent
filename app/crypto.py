"""API Key 加密：Windows 上使用系统自带的 DPAPI，只有当前 Windows 用户能解密。

非 Windows（仅开发调试用）退化为 base64，并带上 plain: 前缀以示区别。
"""
from __future__ import annotations

import base64
import sys

_PREFIX_DPAPI = "dpapi:"
_PREFIX_PLAIN = "plain:"


if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    class _DataBlob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    _crypt32 = ctypes.windll.crypt32
    _kernel32 = ctypes.windll.kernel32
    _CRYPTPROTECT_UI_FORBIDDEN = 0x01

    def _to_blob(data: bytes):
        buf = ctypes.create_string_buffer(data, len(data))
        blob = _DataBlob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
        return blob, buf  # buf 必须保持引用，防止被回收

    def _from_blob(blob: _DataBlob) -> bytes:
        try:
            return ctypes.string_at(blob.pbData, blob.cbData)
        finally:
            _kernel32.LocalFree(ctypes.cast(blob.pbData, ctypes.c_void_p))

    def _protect(raw: bytes) -> bytes:
        inp, _keep = _to_blob(raw)
        out = _DataBlob()
        ok = _crypt32.CryptProtectData(
            ctypes.byref(inp), ctypes.c_wchar_p("aiWatchTikTok"), None, None, None,
            _CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(out),
        )
        if not ok:
            raise ctypes.WinError()
        return _from_blob(out)

    def _unprotect(enc: bytes) -> bytes:
        inp, _keep = _to_blob(enc)
        out = _DataBlob()
        ok = _crypt32.CryptUnprotectData(
            ctypes.byref(inp), None, None, None, None, _CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(out),
        )
        if not ok:
            raise ctypes.WinError()
        return _from_blob(out)


def encrypt(secret: str) -> str:
    if not secret:
        return ""
    raw = secret.encode("utf-8")
    if sys.platform == "win32":
        return _PREFIX_DPAPI + base64.b64encode(_protect(raw)).decode("ascii")
    return _PREFIX_PLAIN + base64.b64encode(raw).decode("ascii")


def decrypt(token: str) -> str:
    if not token:
        return ""
    if token.startswith(_PREFIX_DPAPI):
        if sys.platform != "win32":
            raise RuntimeError("这个 API Key 是在 Windows 上加密的，只能在原电脑上解密")
        return _unprotect(base64.b64decode(token[len(_PREFIX_DPAPI):])).decode("utf-8")
    if token.startswith(_PREFIX_PLAIN):
        return base64.b64decode(token[len(_PREFIX_PLAIN):]).decode("utf-8")
    return token


def mask(secret: str) -> str:
    """界面上显示用：只露出前 6 位和后 4 位。"""
    if not secret:
        return ""
    if len(secret) <= 12:
        return secret[:2] + "•" * 6
    return f"{secret[:6]}••••••{secret[-4:]}"
