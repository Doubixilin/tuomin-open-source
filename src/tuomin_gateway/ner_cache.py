"""Bounded, memory-only NER results. Never cache policies or placeholder mappings."""
from __future__ import annotations

from collections import OrderedDict
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
import hashlib
import hmac
import secrets
import threading
import time

from tuomin_gateway.cancellation import Cancelled, check

_prepared: ContextVar[tuple | None] = ContextVar("tuomin_prepared_ner", default=None)


class NerCache:
    def __init__(self, *, max_entries=2048, max_bytes=16 * 1024 * 1024, ttl=600):
        self.max_entries, self.max_bytes, self.ttl = max_entries, max_bytes, ttl
        self._salt = secrets.token_bytes(32)
        self._items = OrderedDict()
        self._bytes = 0
        self._lock = threading.Lock()

    def key(self, scope, identity, text):
        value = repr((scope, identity)).encode() + b"\0" + text.encode("utf-8")
        return hmac.new(self._salt, value, hashlib.sha256).digest()

    def get(self, key):
        now = time.monotonic()
        with self._lock:
            self._prune(now)
            item = self._items.get(key)
            if item is None:
                return None
            # Sliding expiry: tool definitions resent with every request stay
            # warm for the whole session instead of re-running NER each TTL.
            self._items[key] = (now + self.ttl, item[1], item[2])
            self._items.move_to_end(key)
            return deepcopy(item[1])

    def _prune(self, now):
        # Writes and hits both move an entry to the end with a fresh expiry, so
        # expiries ascend from the front: stop at the first live entry.
        while self._items:
            key, item = next(iter(self._items.items()))
            if item[0] > now:
                return
            self._bytes -= self._items.pop(key)[2]

    def put(self, key, text, spans):
        # Conservative accounting plus an entry cap; metadata may contain raw
        # values, so it is copied, bounded and never serialized to disk/logs.
        size = 512 + 4 * len(text.encode("utf-8")) + 4 * len(repr(spans).encode("utf-8"))
        if size > self.max_bytes or self.max_entries <= 0:
            return
        with self._lock:
            self._prune(time.monotonic())
            old = self._items.pop(key, None)
            if old:
                self._bytes -= old[2]
            while self._items and (len(self._items) >= self.max_entries or self._bytes + size > self.max_bytes):
                self._bytes -= self._items.popitem(last=False)[1][2]
            self._items[key] = (time.monotonic() + self.ttl, deepcopy(spans), size)
            self._bytes += size


def detect(text, detector):
    prepared = _prepared.get()
    if prepared is not None and prepared[0] is detector:
        if prepared[2] is not None:
            raise prepared[2]
        if text in prepared[1]:
            return deepcopy(prepared[1][text])
    return detector.detect(text)


class _WindowCache:
    """Raw per-window predictions, keyed apart from whole-text results."""

    def __init__(self, cache, scope, identity):
        self.cache, self.scope, self.identity = cache, scope, (*identity, "window")
        self.hits = self.misses = 0

    def get(self, window):
        hit = self.cache.get(self.cache.key(self.scope, self.identity, window))
        if hit is None:
            self.misses += 1
        else:
            self.hits += 1
        return hit

    def put(self, window, predictions):
        self.cache.put(self.cache.key(self.scope, self.identity, window), window, predictions)


@contextmanager
def prepare(texts, *, enabled, cache, scope, metrics):
    """Precompute only model spans; downstream rules/policies run on every field."""
    if not enabled:
        yield
        return
    from tuomin_gateway.detectors.ner import get_ner_detector
    detector = get_ner_detector()
    if not hasattr(detector, "detect_many") or not hasattr(detector, "cache_identity"):
        yield  # support injected/custom detectors without changing their contract
        return
    texts = [t for t in texts if isinstance(t, str) and t]
    unique = list(dict.fromkeys(texts))
    metrics.update(text_fields=len(texts), unique_texts=len(unique), ner_chars=sum(map(len, unique)))
    ready_start = time.perf_counter()
    results, error = {}, None
    try:
        check()
        identity = detector.cache_identity()
        metrics["ner_ready_ms"] = round((time.perf_counter() - ready_start) * 1000, 3)
        keys = {t: cache.key(scope, identity, t) for t in unique}
        missing = []
        for text in unique:
            check()
            hit = cache.get(keys[text])
            if hit is None:
                missing.append(text)
            else:
                results[text] = hit
        metrics.update(cache_hits=len(unique) - len(missing), cache_misses=len(missing))
        infer_start = time.perf_counter()
        windows = _WindowCache(cache, scope, identity)
        inferred = detector.detect_many(missing, window_cache=windows) if missing else []
        metrics.update(window_cache_hits=windows.hits, window_cache_misses=windows.misses)
        if len(inferred) != len(missing):
            raise RuntimeError("incomplete NER batch")
        check()
        for text, spans in zip(missing, inferred):
            results[text] = spans
            cache.put(keys[text], text, spans)
        metrics["ner_infer_ms"] = round((time.perf_counter() - infer_start) * 1000, 3)
    except Cancelled:
        raise
    except Exception as exc:
        # Feed the failure through the existing required/optional readiness
        # logic. Never turn a failed or partial batch into a negative cache hit.
        error = exc
    token = _prepared.set((detector, results, error))
    try:
        yield
    finally:
        _prepared.reset(token)
