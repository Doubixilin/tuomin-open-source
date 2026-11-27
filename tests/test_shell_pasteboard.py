"""Pure fake tests plus isolated, synthetic NSPasteboard smoke coverage."""
from __future__ import annotations

import queue
import secrets
import threading

import pytest

from tuomin_gateway.shell.main_thread import call_on_main
from tuomin_gateway.shell.pasteboard_macos import CONCEALED_TYPE, TEXT_TYPE, Pasteboard


class Board:
    def __init__(self, contents=None):
        self.contents = contents or {}
        self.writes = []

    def types(self):
        return list(self.contents)

    def stringForType_(self, kind):
        return self.contents.get(kind)

    def clearContents(self):
        self.contents.clear()
        self.writes.append("clear")

    def setString_forType_(self, text, kind):
        self.writes.append(kind)
        self.contents[kind] = text
        return True


@pytest.mark.parametrize("contents", [{}, {TEXT_TYPE: ""}, {"public.html": "<p>合成</p>"}])
def test_non_plain_or_empty_clipboard_is_not_text(contents):
    assert Pasteboard(board=Board(contents)).read_text() is None


def test_replaces_rich_flavors_and_adds_marker_before_restored_text():
    board = Board({"public.html": "<p>合成</p>", "public.rtf": "synthetic"})
    pasteboard = Pasteboard(board=board)
    pasteboard.write_text("合成文本", concealed=True)
    assert board.contents == {TEXT_TYPE: "合成文本", CONCEALED_TYPE: ""}
    assert board.writes == ["clear", CONCEALED_TYPE, TEXT_TYPE]
    pasteboard.write_text("<PERSON_001>")
    assert board.contents == {TEXT_TYPE: "<PERSON_001>"}


def test_worker_read_waits_for_main_thread_dispatch():
    pending = queue.Queue()
    result = []
    pasteboard = Pasteboard(board=Board({TEXT_TYPE: "合成文本"}), call_after=pending.put)
    worker = threading.Thread(target=lambda: result.append(pasteboard.read_text()))
    worker.start()
    callback = pending.get(timeout=2)
    assert not result
    callback()
    worker.join(timeout=2)
    assert result == ["合成文本"]


def test_timed_out_queued_write_is_cancelled():
    pending, errors, mutations = queue.Queue(), [], []
    def worker():
        try:
            call_on_main(lambda: mutations.append(True), call_after=pending.put, timeout=0.01)
        except RuntimeError as error:
            errors.append(str(error))
    thread = threading.Thread(target=worker)
    thread.start()
    queued = pending.get(timeout=2)
    thread.join(timeout=2)
    queued()
    assert errors == ["应用主线程未响应，请重新打开工作台"]
    assert not mutations


def test_main_thread_calls_are_direct_without_native_import():
    assert call_on_main(lambda: 42, call_after=lambda fn: pytest.fail("must not dispatch")) == 42


def test_marker_failure_never_writes_restored_text():
    board = Board()
    board.setString_forType_ = lambda text, kind: False
    with pytest.raises(RuntimeError, match="隐私标记"):
        Pasteboard(board=board).write_text("合成", concealed=True)
    assert not board.contents


def test_native_named_pasteboard_does_not_touch_general_clipboard():
    appkit = pytest.importorskip("AppKit")
    board = appkit.NSPasteboard.pasteboardWithUniqueName()
    try:
        pasteboard = Pasteboard(board=board)
        synthetic = secrets.token_urlsafe(12)
        pasteboard.write_text(synthetic, concealed=True)
        assert pasteboard.read_text() == synthetic
        assert {TEXT_TYPE, CONCEALED_TYPE} <= set(board.types())
        assert set(board.types()) <= {TEXT_TYPE, CONCEALED_TYPE, "NSStringPboardType"}
        pasteboard.write_text("<PERSON_001>")
        assert TEXT_TYPE in board.types()
        assert set(board.types()) <= {TEXT_TYPE, "NSStringPboardType"}
    finally:
        board.releaseGlobally()
