"""Explicit per-user login items; constructing/reading never changes settings."""
from __future__ import annotations

import os
from pathlib import Path
import sys
import tempfile
import threading


class LoginItem:
    def __init__(self, executable=None, *, startup_dir=None):
        self.executable = Path(executable or sys.executable)
        self._startup_dir = Path(startup_dir) if startup_dir is not None else None

    def _windows_path(self):
        if self._startup_dir is not None:
            return self._startup_dir / "Tuomin.vbs"
        roaming = os.environ.get("APPDATA")
        if not roaming:
            raise RuntimeError("无法定位当前用户的启动目录")
        return Path(roaming) / "Microsoft/Windows/Start Menu/Programs/Startup/Tuomin.vbs"

    def _windows_contents(self):
        executable = self.executable.resolve()
        # No shell interpolation: ShellExecute receives a literal executable
        # path, arguments and working directory as separate VBScript strings.
        def literal(value):
            value = str(value)
            if any(char in value for char in "\r\n\0"):
                raise ValueError("登录启动路径无效")
            return '"' + value.replace('"', '""') + '"'
        return ("' Tuomin managed login item; created only by an explicit setting.\r\n"
                'CreateObject("Shell.Application").ShellExecute '
                + literal(executable) + ', "--shell", ' + literal(executable.parent)
                + ', "open", 1\r\n')

    def _windows_owned(self, path):
        if path.is_symlink():
            raise RuntimeError("登录启动文件不能是符号链接")
        if not path.exists():
            return False
        try:
            owned = path.read_text(encoding="utf-16").replace("\r\n", "\n") == self._windows_contents().replace("\r\n", "\n")
        except (OSError, UnicodeError):
            owned = False
        if not owned:
            raise RuntimeError("启动目录中已有其他 Tuomin 登录项，请先在系统设置中核对")
        return True

    @property
    def available(self):
        if sys.platform == "win32":
            # Never install a development interpreter as a login application.
            return (self.executable.suffix.lower() == ".exe"
                    and self.executable.stem.lower() not in {"python", "pythonw"}
                    and self.executable.is_file()
                    and bool(self._startup_dir or os.environ.get("APPDATA")))
        parent = self.executable.parent
        return (sys.platform == "darwin" and parent.name == "MacOS"
                and parent.parent.name == "Contents"
                and parent.parent.parent.suffix == ".app")

    def _service(self):
        if not self.available:
            raise RuntimeError("登录时启动仅支持应用包")
        if threading.current_thread() is not threading.main_thread():
            raise RuntimeError("登录项操作必须在主线程执行")
        import objc
        from Foundation import NSBundle

        bundle = NSBundle.bundleWithPath_(
            "/System/Library/Frameworks/ServiceManagement.framework"
        )
        if bundle is None or not bundle.load():
            raise RuntimeError("系统不支持登录项设置")
        # Dynamic lookup has no PyObjC framework metadata for NSError **. Mark
        # the parameter as out so failures return (success, error) reliably.
        for selector in (b"registerAndReturnError:", b"unregisterAndReturnError:"):
            objc.registerMetaDataForSelector(
                b"SMAppService", selector,
                {"arguments": {2: {"type_modifier": objc._C_OUT}}},
            )
        return objc.lookUpClass("SMAppService").mainAppService()

    def status(self):
        if sys.platform == "win32":
            if not self.available:
                return 0
            try:
                return int(self._windows_owned(self._windows_path()))
            except (OSError, RuntimeError):
                # A stale entry from a moved installation must not break the
                # tray or settings merely by querying its checked state. Keep
                # writes strict: set_enabled still refuses foreign entries.
                return 2
        return int(self._service().status()) if self.available else 0

    def enabled(self):
        return self.status() == 1

    def set_enabled(self, enabled):
        if sys.platform == "win32":
            if not self.available:
                raise RuntimeError("登录时启动仅支持打包后的 Windows 应用")
            path = self._windows_path()
            owned = self._windows_owned(path)
            if not enabled:
                if owned:
                    path.unlink()
                return 0
            if owned:
                return 1
            from tuomin_gateway.platform_security import restrict_permissions
            path.parent.mkdir(parents=True, exist_ok=True)
            fd, temporary = tempfile.mkstemp(prefix=".Tuomin-", suffix=".tmp", dir=path.parent)
            try:
                restrict_permissions(temporary, is_dir=False)
                with os.fdopen(fd, "w", encoding="utf-16", newline="") as stream:
                    fd = -1
                    stream.write(self._windows_contents())
                    stream.flush()
                    os.fsync(stream.fileno())
                # A concurrent registration must not overwrite an unrelated file.
                if path.exists() or path.is_symlink():
                    self._windows_owned(path)
                else:
                    os.rename(temporary, path)  # On Windows refuses an existing target.
            finally:
                if fd != -1:
                    os.close(fd)
                if os.path.exists(temporary):
                    os.unlink(temporary)
            return 1
        service = self._service()
        method = service.registerAndReturnError_ if enabled else service.unregisterAndReturnError_
        result = method(None)
        success = result[0] if isinstance(result, tuple) else result
        if not success:
            raise RuntimeError("无法修改登录项，请在系统设置中检查登录项权限")
        return int(service.status())
