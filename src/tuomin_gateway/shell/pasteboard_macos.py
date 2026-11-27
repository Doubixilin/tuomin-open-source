"""Only NSPasteboard I/O; no detection, persistence or implicit monitoring."""
from __future__ import annotations

from .main_thread import call_on_main

TEXT_TYPE = "public.utf8-plain-text"
CONCEALED_TYPE = "org.nspasteboard.ConcealedType"


class Pasteboard:
    def __init__(self, *, board=None, call_after=None):
        self._board = board
        self._call_after = call_after

    def _native_board(self):
        if self._board is None:
            from AppKit import NSPasteboard
            self._board = NSPasteboard.generalPasteboard()
        return self._board

    def read_text(self):
        def read():
            board = self._native_board()
            if TEXT_TYPE not in (board.types() or ()):
                return None
            value = board.stringForType_(TEXT_TYPE)
            return str(value) if value else None
        return call_on_main(read, call_after=self._call_after)

    def write_text(self, text, concealed=False):
        if not isinstance(text, str):
            raise TypeError("剪贴板写入内容必须是文本")

        def write():
            board = self._native_board()
            board.clearContents()
            if concealed:
                # Add the marker before any restored text is available to a
                # clipboard-history reader. An empty string is a valid marker.
                if not board.setString_forType_("", CONCEALED_TYPE):
                    raise RuntimeError("无法设置剪贴板隐私标记")
            if not board.setString_forType_(text, TEXT_TYPE):
                raise RuntimeError("无法写入系统剪贴板")
        return call_on_main(write, call_after=self._call_after)

    def change_count(self) -> int:
        return int(call_on_main(lambda: self._native_board().changeCount(), call_after=self._call_after))

    def clear(self) -> None:
        call_on_main(lambda: self._native_board().clearContents(), call_after=self._call_after)
