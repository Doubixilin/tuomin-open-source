"""Small cross-platform helpers for owner-private runtime paths."""

from __future__ import annotations

import ctypes
from ctypes import wintypes
from functools import lru_cache
import os
from pathlib import Path


_TOKEN_QUERY = 0x0008
_TOKEN_USER = 1


class _SidAndAttributes(ctypes.Structure):
    _fields_ = [("sid", wintypes.LPVOID), ("attributes", wintypes.DWORD)]


class _TokenUser(ctypes.Structure):
    _fields_ = [("user", _SidAndAttributes)]


@lru_cache(maxsize=1)
def windows_current_user_sid() -> str:
    """Return the current process token's user SID without shell parsing."""
    if os.name != "nt":
        raise OSError("Windows user SID is only available on Windows")

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)

    get_current_process = kernel32.GetCurrentProcess
    get_current_process.restype = wintypes.HANDLE
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [wintypes.HANDLE]
    close_handle.restype = wintypes.BOOL
    local_free = kernel32.LocalFree
    local_free.argtypes = [wintypes.HLOCAL]
    local_free.restype = wintypes.HLOCAL

    open_process_token = advapi32.OpenProcessToken
    open_process_token.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.HANDLE),
    ]
    open_process_token.restype = wintypes.BOOL
    get_token_information = advapi32.GetTokenInformation
    get_token_information.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    get_token_information.restype = wintypes.BOOL
    convert_sid = advapi32.ConvertSidToStringSidW
    convert_sid.argtypes = [wintypes.LPVOID, ctypes.POINTER(wintypes.LPWSTR)]
    convert_sid.restype = wintypes.BOOL

    token = wintypes.HANDLE()
    if not open_process_token(get_current_process(), _TOKEN_QUERY, ctypes.byref(token)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        required = wintypes.DWORD()
        get_token_information(token, _TOKEN_USER, None, 0, ctypes.byref(required))
        if required.value == 0:
            raise ctypes.WinError(ctypes.get_last_error())
        buffer = ctypes.create_string_buffer(required.value)
        if not get_token_information(
            token,
            _TOKEN_USER,
            buffer,
            required,
            ctypes.byref(required),
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        token_user = ctypes.cast(buffer, ctypes.POINTER(_TokenUser)).contents
        sid_text = wintypes.LPWSTR()
        if not convert_sid(token_user.user.sid, ctypes.byref(sid_text)):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            return str(sid_text.value)
        finally:
            local_free(sid_text)
    finally:
        close_handle(token)


def restrict_permissions(path: str | Path, *, is_dir: bool) -> None:
    """Restrict a path to the current user and SYSTEM.

    POSIX uses owner-only mode bits. Windows removes inherited ACL entries and
    grants full control only to the current user and LocalSystem. Failures are
    raised because silently continuing would misrepresent sensitive storage as
    owner-private.
    """
    target = Path(path)
    if os.name != "nt":
        os.chmod(target, 0o700 if is_dir else 0o600)
        return

    from tuomin_gateway.windows_acl import restrict_windows_dacl

    restrict_windows_dacl(
        target,
        user_sid=windows_current_user_sid(),
        is_dir=is_dir,
    )


__all__ = ["restrict_permissions", "windows_current_user_sid"]
