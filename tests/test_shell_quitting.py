"""Quit coordination: one path for menu bar, Dock, ⌘Q and logout."""
from __future__ import annotations

import time

import pytest

from tuomin_gateway.shell.quitting import TERMINATE_LATER, TERMINATE_NOW, QuitCoordinator


def make(recent=False, confirm=True, **extra):
    log = {"confirm": [], "prepared": 0, "replies": [], "status": [], "hard": 0, "inspections": 0, "exited": 0}

    def check():
        log["inspections"] += 1
        if isinstance(recent, Exception):
            raise recent
        return recent

    def ask(unknown):
        log["confirm"].append(unknown)
        if isinstance(confirm, Exception):
            raise confirm
        return confirm

    coordinator = QuitCoordinator(
        check_recent=check, confirm=ask,
        prepare_exit=lambda: log.__setitem__("prepared", log["prepared"] + 1),
        reply=log["replies"].append, call_after=lambda callback: callback(),
        exit_process=lambda: log.__setitem__("exited", log["exited"] + 1),
        status=log["status"].append, start_thread=lambda target: target(),
        hard_exit=lambda: log.__setitem__("hard", log["hard"] + 1), hard_exit_after=extra.pop("hard_exit_after", 60), **extra)
    return coordinator, log


def test_idle_quit_exits_without_asking_and_waits_for_reply():
    coordinator, log = make(recent=False)
    assert coordinator.should_terminate() == TERMINATE_LATER  # logout keeps waiting, not cancelled
    # Exits itself after cleanup (Cocoa's exit() aborts while torch threads run).
    assert log["confirm"] == [] and log["prepared"] == 1 and log["exited"] == 1 and log["replies"] == []
    assert log["status"][0] == "正在退出…"
    assert coordinator.should_terminate() == TERMINATE_NOW


@pytest.mark.parametrize("recent,unknown", [(True, False), (None, True), (RuntimeError("timeout"), True)])
def test_recent_or_unknown_use_asks_in_front(recent, unknown):
    coordinator, log = make(recent=recent, confirm=True)
    coordinator.should_terminate()
    assert log["confirm"] == [unknown] and log["exited"] == 1 and log["prepared"] == 1


def test_choosing_keep_running_cancels_and_allows_a_later_quit():
    coordinator, log = make(recent=True, confirm=False)
    assert coordinator.should_terminate() == TERMINATE_LATER
    assert log["replies"] == [False] and log["prepared"] == 0 and log["exited"] == 0 and log["status"][-1] == ""
    assert coordinator.should_terminate() == TERMINATE_LATER and log["inspections"] == 2


def test_broken_dialog_never_traps_the_user():
    coordinator, log = make(recent=True, confirm=RuntimeError("no window server"))
    coordinator.should_terminate()
    assert log["exited"] == 1 and log["prepared"] == 1


def test_repeated_requests_while_checking_start_one_inspection():
    log_calls = []
    coordinator = QuitCoordinator(check_recent=lambda: False, confirm=lambda unknown: True, prepare_exit=lambda: None,
                                  reply=log_calls.append, call_after=lambda callback: None,
                                  start_thread=lambda target: log_calls.append("thread"), hard_exit=lambda: None, hard_exit_after=60)
    assert coordinator.should_terminate() == TERMINATE_LATER
    assert coordinator.should_terminate() == TERMINATE_LATER
    assert log_calls == ["thread"]


def test_force_skips_check_and_dialog_and_hard_exit_backs_it_up():
    coordinator, log = make(recent=True, hard_exit_after=0.05)
    coordinator.force()
    coordinator.force()
    assert log["prepared"] == 1 and log["exited"] == 1 and log["confirm"] == [] and log["inspections"] == 0
    assert coordinator.should_terminate() == TERMINATE_NOW
    time.sleep(0.3)
    assert log["hard"] == 1


def test_terminate_handler_replaces_pywebview_close_veto():
    pytest.importorskip("objc")
    from AppKit import NSApplication
    NSApplication.sharedApplication()
    from webview.platforms import cocoa
    from tuomin_gateway.shell.window import install_terminate_handler
    seen = []
    assert install_terminate_handler(lambda app: seen.append(app) or TERMINATE_LATER) is True
    delegate = cocoa.BrowserView.AppDelegate.alloc().init()
    assert delegate.applicationShouldTerminate_(None) == TERMINATE_LATER and seen == [None]


def test_failed_cleanup_still_ends_the_process():
    exited = []
    coordinator = QuitCoordinator(check_recent=lambda: False, confirm=lambda unknown: True,
                                  prepare_exit=lambda: (_ for _ in ()).throw(RuntimeError("hotkeys")),
                                  reply=lambda proceed: None, call_after=lambda callback: callback(),
                                  start_thread=lambda target: target(), exit_process=lambda: exited.append(1),
                                  hard_exit=lambda: None, hard_exit_after=60)
    with pytest.raises(RuntimeError):
        coordinator.should_terminate()
    assert exited == [1]
