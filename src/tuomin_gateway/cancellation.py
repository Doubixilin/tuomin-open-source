"""Cooperative cancellation for long-running document requests.

A client that may want to abort a slow redaction sends ``X-Tuomin-Request-Id``
(an opaque token it generated). The HTTP layer opens a cancellation scope for
that id; CPU-heavy loops call :func:`check` between units of work; a later
``POST /api/v1/documents/cancel`` with the same id makes the next check raise
:class:`Cancelled`, which the service turns into a ``409 cancelled`` response.
Nothing here persists: an unknown or finished id simply cancels nothing.
"""
from __future__ import annotations

import contextvars
import re
import threading
import time
from contextlib import contextmanager
from typing import Iterator

_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")
_MAX_ACTIVE = 256
_STALE_SECONDS = 6 * 3600

_current: contextvars.ContextVar[threading.Event | None] = contextvars.ContextVar(
    "tuomin_cancel_event", default=None
)
_registry: dict[str, tuple[threading.Event, float]] = {}
_lock = threading.Lock()


class Cancelled(Exception):
    """Raised inside a cancelled scope by :func:`check`."""


def valid_request_id(request_id: object) -> bool:
    return isinstance(request_id, str) and _ID_RE.match(request_id) is not None


def _prune_locked(now: float) -> None:
    stale = [key for key, (_event, started) in _registry.items() if now - started > _STALE_SECONDS]
    for key in stale:
        _registry.pop(key, None)
    while len(_registry) >= _MAX_ACTIVE:
        oldest = min(_registry, key=lambda key: _registry[key][1])
        _registry.pop(oldest, None)


@contextmanager
def begin(request_id: object) -> Iterator[threading.Event | None]:
    """Open a cancellation scope for ``request_id`` in the current context."""
    if not valid_request_id(request_id):
        yield None
        return
    event = threading.Event()
    now = time.time()
    with _lock:
        _prune_locked(now)
        _registry[str(request_id)] = (event, now)
    token = _current.set(event)
    try:
        yield event
    finally:
        _current.reset(token)
        with _lock:
            if _registry.get(str(request_id), (None,))[0] is event:
                _registry.pop(str(request_id), None)


def cancel(request_id: object) -> bool:
    """Flag the scope for ``request_id``; True when such a scope was active."""
    if not valid_request_id(request_id):
        return False
    with _lock:
        item = _registry.get(str(request_id))
    if item is None:
        return False
    item[0].set()
    return True


def check() -> None:
    """Raise :class:`Cancelled` if the current scope was cancelled (cheap)."""
    event = _current.get()
    if event is not None and event.is_set():
        raise Cancelled()


def active_count() -> int:
    with _lock:
        return len(_registry)


def current_event() -> threading.Event | None:
    return _current.get()


@contextmanager
def use_event(event: threading.Event):
    token = _current.set(event)
    try:
        yield
    finally:
        _current.reset(token)
