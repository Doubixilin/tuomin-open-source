"""One quit path for the menu bar item, the Dock menu, ⌘Q and macOS logout.

Every quit becomes ``NSApp.terminate:``; pywebview's app delegate hands
``applicationShouldTerminate:`` to :meth:`QuitCoordinator.should_terminate`.
The coordinator answers *later* (so logout/shutdown keeps waiting instead of
being cancelled), checks off the main thread whether external tools used the
gateway in the last seconds, asks in a frontmost dialog only when needed, then
cleans up and ends the process itself. Cocoa's own exit path runs C++ static
destructors (torch) while worker threads may still be redacting, which aborts
the process and shows a crash report; ``exit_process`` (``os._exit``) after an
explicit cleanup avoids that. Closing the window only hides it.
No AppKit import here: the native pieces are injected.
"""
from __future__ import annotations

import os
import threading
from typing import Callable

TERMINATE_CANCEL, TERMINATE_NOW, TERMINATE_LATER = 0, 1, 2


class QuitCoordinator:
    def __init__(self, *, check_recent: Callable[[], bool | None], confirm: Callable[[bool], bool],
                 prepare_exit: Callable[[], None], reply: Callable[[bool], None],
                 exit_process: Callable[[], None] | None = None,
                 call_after: Callable[[Callable[[], None]], None], status: Callable[[str], None] = lambda text: None,
                 start_thread=None, hard_exit: Callable[[], None] | None = None, hard_exit_after: float = 6.0):
        self.check_recent, self.confirm, self.prepare_exit = check_recent, confirm, prepare_exit
        self.reply, self.call_after, self.status = reply, call_after, status
        self.start_thread = start_thread or (lambda target: threading.Thread(target=target, name="tuomin-quit-check", daemon=True).start())
        self.hard_exit = hard_exit or (lambda: os._exit(0))
        self.exit_process = exit_process or self.hard_exit
        self.hard_exit_after = hard_exit_after
        self.approved = False
        self.pending = False
        self._lock = threading.Lock()

    def should_terminate(self, _app=None) -> int:
        """Answer for ``applicationShouldTerminate:`` (always on the main thread)."""
        with self._lock:
            if self.approved:
                return TERMINATE_NOW
            if self.pending:
                return TERMINATE_LATER
            self.pending = True
        self.status("正在退出…")

        def inspect():
            try:
                recent = self.check_recent()
            except Exception:
                recent = None  # unknown: ask rather than silently cut tools off
            self.call_after(lambda: self._decide(recent))

        self.start_thread(inspect)
        return TERMINATE_LATER

    def _decide(self, recent: bool | None) -> None:
        try:
            proceed = recent is False or bool(self.confirm(recent is None))
        except Exception:
            proceed = True  # a broken dialog must never trap the user in the app
        with self._lock:
            self.pending = False
            self.approved = proceed
        if proceed:
            self._exit_now()
        else:
            self.status("")
            self.reply(False)

    def force(self) -> None:
        """Signals and fatal paths: no check, no dialog."""
        with self._lock:
            if self.approved:
                return
            self.approved = True
        self._exit_now()

    def _exit_now(self) -> None:
        # Backstop first: whatever happens below, the process ends.
        timer = threading.Timer(self.hard_exit_after, self.hard_exit)
        timer.daemon = True
        timer.start()
        try:
            self.prepare_exit()
        finally:
            self.exit_process()
