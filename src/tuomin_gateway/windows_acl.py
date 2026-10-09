"""Native Windows DACL application for private runtime paths."""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import os
from pathlib import Path


_DACL_SECURITY_INFORMATION = 0x00000004
_PROTECTED_DACL_SECURITY_INFORMATION = 0x80000000
_SDDL_REVISION_1 = 1
_SE_FILE_OBJECT = 1


def restrict_windows_dacl(path: str | Path, *, user_sid: str, is_dir: bool) -> None:
    """Replace a filesystem DACL with current-user and SYSTEM full control."""
    if os.name != "nt":
        raise OSError("Windows DACLs are only available on Windows")

    target = Path(path).resolve()
    inheritance = "OICI" if is_dir else ""
    sddl = (
        f"D:P(A;{inheritance};FA;;;{user_sid})"
        f"(A;{inheritance};FA;;;SY)"
    )

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    convert_security_descriptor = (
        advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW
    )
    convert_security_descriptor.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.LPVOID),
        ctypes.POINTER(wintypes.DWORD),
    ]
    convert_security_descriptor.restype = wintypes.BOOL

    get_dacl = advapi32.GetSecurityDescriptorDacl
    get_dacl.argtypes = [
        wintypes.LPVOID,
        ctypes.POINTER(wintypes.BOOL),
        ctypes.POINTER(wintypes.LPVOID),
        ctypes.POINTER(wintypes.BOOL),
    ]
    get_dacl.restype = wintypes.BOOL

    set_named_security = advapi32.SetNamedSecurityInfoW
    set_named_security.argtypes = [
        wintypes.LPWSTR,
        ctypes.c_int,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.LPVOID,
        wintypes.LPVOID,
        wintypes.LPVOID,
    ]
    set_named_security.restype = wintypes.DWORD

    local_free = kernel32.LocalFree
    local_free.argtypes = [wintypes.LPVOID]
    local_free.restype = wintypes.LPVOID

    descriptor = wintypes.LPVOID()
    descriptor_size = wintypes.DWORD()
    if not convert_security_descriptor(
        sddl,
        _SDDL_REVISION_1,
        ctypes.byref(descriptor),
        ctypes.byref(descriptor_size),
    ):
        raise ctypes.WinError(ctypes.get_last_error())

    try:
        dacl_present = wintypes.BOOL()
        dacl_defaulted = wintypes.BOOL()
        dacl = wintypes.LPVOID()
        if not get_dacl(
            descriptor,
            ctypes.byref(dacl_present),
            ctypes.byref(dacl),
            ctypes.byref(dacl_defaulted),
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        if not dacl_present.value:
            raise OSError("generated Windows security descriptor has no DACL")

        error = set_named_security(
            str(target),
            _SE_FILE_OBJECT,
            _DACL_SECURITY_INFORMATION | _PROTECTED_DACL_SECURITY_INFORMATION,
            None,
            None,
            dacl,
            None,
        )
        if error:
            raise ctypes.WinError(error)
    finally:
        local_free(descriptor)


__all__ = ["restrict_windows_dacl"]
