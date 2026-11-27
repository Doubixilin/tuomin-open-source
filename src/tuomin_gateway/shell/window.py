"""A hidden-until-authenticated pywebview window; no service dependencies."""
from __future__ import annotations

from functools import wraps
import json
import sys
from urllib.parse import urlsplit


def bootstrap_script(base_url: str, admin_token: str, *, demo: bool = False) -> str:
    """Build the in-process bootstrap without placing credentials in a URL."""
    origin = base_url.rstrip("/")
    parsed = urlsplit(origin)
    if parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or parsed.path:
        raise ValueError("The shell requires a loopback HTTP origin")
    if parsed.query or parsed.fragment or parsed.username or parsed.password:
        raise ValueError("The shell requires a loopback HTTP origin")
    return """(() => {
  if (location.origin !== %s) return false;
  localStorage.setItem("tuomin_admin_token", %s);
  window.__tuominShell = true;
  window.__tuominDemo = %s;
  if (typeof refreshTokenMeta === "function") refreshTokenMeta();
  if (typeof refreshAll === "function") refreshAll();
  window.dispatchEvent(new CustomEvent("tuomin-shell-ready"));
  return true;
})()""" % (json.dumps(origin), json.dumps(admin_token), json.dumps(demo))


def allow_native_context_menu() -> bool:
    """Keep WebKit's own Cut/Copy/Paste context menu in the native window.

    pywebview's Cocoa host clears every context-menu item unless it runs in
    debug mode, which would also expose the Web Inspector. Replace just that
    override so text fields get the ordinary editing menu back."""
    if sys.platform != 'darwin':
        return False
    try:
        import objc
        from webview.platforms import cocoa
    except Exception:
        return False
    host = cocoa.BrowserView.WebKitHost

    def willOpenMenu_withEvent_(self, menu, event):  # noqa: N802 - ObjC selector
        return None

    try:
        objc.classAddMethods(host, [willOpenMenu_withEvent_])
    except Exception:
        return False
    return True


def install_terminate_handler(handler) -> bool:
    """Route ``applicationShouldTerminate:`` (Dock 退出, ⌘Q, logout) to ``handler``.

    pywebview answers it by firing the window's *closing* event, which this
    shell vetoes to keep "close window" meaning "hide": the system quit was
    silently refused. ``handler(app)`` returns an NSApplicationTerminateReply."""
    try:
        import objc
        from webview.platforms import cocoa
    except Exception:
        return False

    def applicationShouldTerminate_(self, app):  # noqa: N802 - ObjC selector
        return handler(app)

    try:
        objc.classAddMethods(cocoa.BrowserView.AppDelegate, [applicationShouldTerminate_])
    except Exception:
        return False
    return True


class WorkbenchWindow:
    def __init__(self, base_url, admin_token, *, demo=False, hidden=False,
                 webview_module=None, on_ready=None, on_error=None, js_api=None):
        if webview_module is None:
            import webview as webview_module
            allow_native_context_menu()
        self._script = bootstrap_script(base_url, admin_token, demo=demo)
        self._ready = False
        self._show_when_ready = not hidden
        self._allow_close = False
        self._on_ready = on_ready
        self._on_error = on_error
        self._windows_context_menu_installed = False
        self.window = webview_module.create_window(
            "Tuomin 本地脱敏工作台", base_url.rstrip("/") + "/ui/",
            width=1200, height=820, min_size=(900, 600), hidden=True,
            confirm_close=False, text_select=True,
        )
        if js_api is not None:
            # Expose only explicit methods. Passing the controller as js_api
            # would let backend attribute lookup traverse private objects.
            exposed = [js_api.get_shell_settings, js_api.save_shell_settings, js_api.clipboard_action]
            for name in ("save_file", "reveal_file", "accessibility_action"):
                method = getattr(js_api, name, None)
                if callable(method):
                    exposed.append(method)
            self.window.expose(*exposed)
        if sys.platform == "win32":
            self.window.events.before_show += self._install_windows_context_menu
        self.window.events.loaded += self._loaded
        self.window.events.closing += self._closing

    def _install_windows_context_menu(self):
        # pywebview 5.4 sets menus=False during WebView2 initialization, then
        # calls load_url/load_html. WebView2 applies menu settings at navigation
        # start: before_load (which follows NavigationCompleted) is too late.
        # before_show runs on the UI thread before its message loop starts.
        # Wrap this window's navigation methods so the setting is applied after
        # initialization defaults, but BEFORE Source/NavigateToString is called.
        if self._windows_context_menu_installed:
            return
        browser = self.window.native.browser
        for name in ("load_url", "load_html"):
            original = getattr(browser, name)

            @wraps(original)
            def navigate(*args, _original=original, **kwargs):
                core = browser.webview.CoreWebView2
                if core is not None:
                    core.Settings.AreDefaultContextMenusEnabled = True
                return _original(*args, **kwargs)

            setattr(browser, name, navigate)
        self._windows_context_menu_installed = True

    def _loaded(self):
        # pywebview's loaded event runs on a worker. evaluate_js itself posts
        # WKWebView work to the main thread and waits; calling it on main would
        # deadlock Cocoa. The window stays hidden until this round trip finishes.
        try:
            if self.window.evaluate_js(self._script) is not True:
                raise RuntimeError("Unexpected window origin")
            self._ready = True
            if self._show_when_ready:
                self.window.show()
            if self._on_ready:
                self._on_ready()
        except Exception:
            # A backend exception can include the evaluated script. Never expose
            # its message or traceback, which might contain the admin credential.
            if self._on_error:
                self._on_error("无法初始化工作台窗口")

    def _closing(self):
        if self._allow_close:
            return True
        self.hide()
        return False

    def show(self):
        self._show_when_ready = True
        if self._ready:
            self.window.show()

    def hide(self):
        self._show_when_ready = False
        self.window.hide()

    def request_exit(self):
        self._allow_close = True
        self.window.destroy()
