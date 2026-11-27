"""Synchronous main-thread calls with cancellable queued operations."""
from __future__ import annotations

from concurrent.futures import Future, TimeoutError
import sys
import threading


def call_on_main(callback, *, call_after=None, timeout=10):
    # Windows adapters own their message loops, and pywebview marshals its own
    # window operations. Only Cocoa requires dispatch to Python's main thread.
    if sys.platform != "darwin" and call_after is None:
        return callback()
    if threading.current_thread() is threading.main_thread():
        return callback()
    if call_after is None:
        from PyObjCTools import AppHelper
        call_after = AppHelper.callAfter
    future = Future()

    def execute():
        if not future.set_running_or_notify_cancel():
            return
        try:
            future.set_result(callback())
        except BaseException as error:
            future.set_exception(error)

    call_after(execute)
    try:
        return future.result(timeout=timeout)
    except TimeoutError:
        # A queued clipboard write must not happen after its caller gave up.
        # If execution already began, wait for that small native operation so
        # the caller receives its real outcome rather than an ambiguous error.
        if future.cancel():
            raise RuntimeError("应用主线程未响应，请重新打开工作台") from None
        return future.result()
