"""Window lifecycle tests using a fake backend; Cocoa is imported lazily."""
from __future__ import annotations

import json
import secrets
import threading
from types import SimpleNamespace

import pytest

from tuomin_gateway.shell.window import WorkbenchWindow, bootstrap_script


class Event:
    def __init__(self):
        self.callbacks = []

    def __iadd__(self, callback):
        self.callbacks.append(callback)
        return self

    def fire(self):
        return [callback() for callback in self.callbacks]


class FakeWebview:
    def __init__(self, result=True):
        self.calls = []
        self.result = result

    def create_window(self, title, url, **kwargs):
        self.options = kwargs
        self.url = url
        self.window = SimpleNamespace(
            events=SimpleNamespace(before_show=Event(), before_load=Event(), loaded=Event(), closing=Event()),
            evaluate_js=self.evaluate_js,
            expose=lambda *functions: setattr(self, "exposed", functions),
            show=lambda: self.calls.append("show"),
            hide=lambda: self.calls.append("hide"),
            destroy=lambda: self.calls.append("destroy"),
        )
        return self.window

    def evaluate_js(self, script):
        self.calls.append("evaluate")
        self.script = script
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def test_token_is_in_script_only_and_json_encoded():
    token = secrets.token_urlsafe(24) + '\\"\n'
    backend = FakeWebview()
    shell = WorkbenchWindow("http://127.0.0.1:8765", token, webview_module=backend)
    assert backend.options["hidden"] is True
    assert backend.options["text_select"] is True
    assert token not in backend.url
    assert backend.calls == []
    shell.window.events.loaded.fire()
    assert backend.calls == ["evaluate", "show"]
    assert json.dumps(token) in backend.script
    assert 'localStorage.setItem("tuomin_admin_token"' in backend.script
    assert "window.__tuominShell = true" in backend.script
    assert 'location.origin !== "http://127.0.0.1:8765"' in backend.script


@pytest.mark.parametrize("origin", [
    "https://example.test", "http://localhost:8765", "http://127.0.0.1:8765/ui/",
    "http://127.0.0.1:8765?credential=value", "http://u:p@127.0.0.1:8765",
])
def test_rejects_non_loopback_or_non_origin_url(origin):
    with pytest.raises(ValueError):
        bootstrap_script(origin, secrets.token_urlsafe(24))


def test_close_hides_and_explicit_exit_allows_destroy():
    backend = FakeWebview()
    shell = WorkbenchWindow("http://127.0.0.1:8765", secrets.token_urlsafe(24),
                            webview_module=backend)
    shell.window.events.loaded.fire()
    assert shell.window.events.closing.fire() == [False]
    assert backend.calls[-1] == "hide"
    shell.show()
    assert backend.calls[-1] == "show"
    shell.request_exit()
    assert backend.calls[-1] == "destroy"
    assert shell.window.events.closing.fire() == [True]


def test_selftest_window_remains_hidden_but_signals_ready():
    backend = FakeWebview()
    ready = []
    shell = WorkbenchWindow("http://127.0.0.1:8765", secrets.token_urlsafe(24),
                            hidden=True, demo=True, webview_module=backend,
                            on_ready=lambda: ready.append(True))
    shell.window.events.loaded.fire()
    assert ready == [True]
    assert backend.calls == ["evaluate"]
    assert "window.__tuominDemo = true" in backend.script


def test_open_before_load_does_not_expose_an_unauthenticated_frame():
    backend = FakeWebview()
    shell = WorkbenchWindow("http://127.0.0.1:8765", secrets.token_urlsafe(24),
                            hidden=True, webview_module=backend)
    shell.show()
    assert backend.calls == []
    shell.window.events.loaded.fire()
    assert backend.calls == ["evaluate", "show"]


@pytest.mark.parametrize("result", [False, RuntimeError(secrets.token_urlsafe(24))])
def test_failed_bootstrap_stays_hidden_and_emits_only_safe_error(result):
    backend = FakeWebview(result)
    errors = []
    shell = WorkbenchWindow("http://127.0.0.1:8765", secrets.token_urlsafe(24),
                            webview_module=backend, on_error=errors.append)
    shell.window.events.loaded.fire()
    assert backend.calls == ["evaluate"]
    assert errors == ["无法初始化工作台窗口"]


def test_only_explicit_bridge_methods_are_exposed():
    backend = FakeWebview()
    api = SimpleNamespace(get_shell_settings=lambda: {}, save_shell_settings=lambda value: {}, clipboard_action=lambda action: False)
    WorkbenchWindow("http://127.0.0.1:8765", secrets.token_urlsafe(32), webview_module=backend, js_api=api)
    assert "js_api" not in backend.options
    assert backend.exposed == (api.get_shell_settings, api.save_shell_settings, api.clipboard_action)


def test_windows_restores_native_edit_menu_before_first_navigation_without_debug(monkeypatch):
    NativeEvent = pytest.importorskip("webview.event", reason="requires pywebview event lifecycle").Event
    from tuomin_gateway.shell import window

    monkeypatch.setattr(window.sys, "platform", "win32")
    ui_thread = threading.get_ident()
    writes = []

    class Settings:
        AreDevToolsEnabled = False
        AreBrowserAcceleratorKeysEnabled = False
        AreDefaultContextMenusEnabled = False

        def __setattr__(self, name, value):
            assert threading.get_ident() == ui_thread
            writes.append((name, value))
            object.__setattr__(self, name, value)

    class WindowsWebview(FakeWebview):
        def create_window(self, *args, **kwargs):
            result = super().create_window(*args, **kwargs)
            # Match pywebview 5.4: the native control exists only after startup,
            # and before_show is synchronous before the native message loop.
            result.native = None
            result.events.before_show = NativeEvent(result, True)
            return result

    backend = WindowsWebview()
    shell = WorkbenchWindow("http://127.0.0.1:8765", secrets.token_urlsafe(24),
                            webview_module=backend)
    settings = Settings()
    assert writes == []
    control = SimpleNamespace(CoreWebView2=None)
    navigations = []

    def load_url(url):
        navigations.append(("url", url, settings.AreDefaultContextMenusEnabled))
        return "url-result"

    def load_html(html, base_uri):
        navigations.append(("html", base_uri, settings.AreDefaultContextMenusEnabled))
        return "html-result"

    browser = SimpleNamespace(webview=control, load_url=load_url, load_html=load_html)
    shell.window.native = SimpleNamespace(browser=browser)
    shell.window.events.before_show.set()
    wrapped_url = browser.load_url
    shell.window.events.before_show.set()
    assert browser.load_url is wrapped_url
    assert writes == []  # Do not access WebView2 settings before initialization.
    control.CoreWebView2 = SimpleNamespace(Settings=settings)
    # The backend's initialization defaults are still false when it calls
    # load_url. Reading true only after NavigationCompleted misses this bug.
    assert settings.AreDefaultContextMenusEnabled is False
    assert browser.load_url("http://127.0.0.1:8765/ui/") == "url-result"
    assert navigations == [("url", "http://127.0.0.1:8765/ui/", True)]
    assert backend.calls == []
    shell.window.events.loaded.fire()
    assert backend.calls == ["evaluate", "show"]
    # HTML and repeated URL navigation also set the option before navigating.
    assert browser.load_html("<textarea></textarea>", "") == "html-result"
    browser.load_url("http://127.0.0.1:8765/ui/")
    assert all(navigation[2] is True for navigation in navigations)
    assert writes == [("AreDefaultContextMenusEnabled", True)] * 3
    assert shell.window.events.before_load.callbacks == []
    assert settings.AreDevToolsEnabled is False
    assert settings.AreBrowserAcceleratorKeysEnabled is False


@pytest.mark.parametrize("platform", ["darwin", "linux"])
def test_other_platforms_do_not_access_windows_native_control(monkeypatch, platform):
    from tuomin_gateway.shell import window

    monkeypatch.setattr(window.sys, "platform", platform)
    backend = FakeWebview()
    shell = WorkbenchWindow("http://127.0.0.1:8765", secrets.token_urlsafe(24),
                            webview_module=backend)
    assert shell.window.events.before_show.callbacks == []
    shell.window.events.before_show.fire()
    shell.window.events.loaded.fire()
    assert backend.calls == ["evaluate", "show"]
