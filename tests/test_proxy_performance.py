"""Optimization must preserve masking/refill and remain cancellable under load."""
import asyncio
from copy import deepcopy
import json
import threading
import time

import httpx
import pytest
from fastapi import FastAPI

from tuomin_gateway import cancellation
from tuomin_gateway.audit import AuditLog
from tuomin_gateway.detectors.ner import CluenerNerDetector
from tuomin_gateway.ner_cache import NerCache
from tuomin_gateway.profiles import PRESETS
from tuomin_gateway.service.proxy import ForwardResult, mask_request, optimized_mask_request, register_proxy
from tuomin_gateway.service.proxy_work import ProxyWorkQueue, ProxyBusy
from tuomin_gateway.service.registry import AppRegistry, _profile_from_spec
from tuomin_gateway.session import SessionRedactor, build_detectors, RequiredDetectorUnavailable


@pytest.fixture
def model(monkeypatch):
    calls = []
    detector = CluenerNerDetector()

    def pipe(texts, batch_size=None):
        calls.append(list(texts))
        results = []
        for text in texts:
            found = []
            for word, kind in [("北辰测试制造有限公司", "company"), ("张三", "name")]:
                start = text.find(word)
                if start >= 0:
                    found.append({"start": start, "end": start + len(word), "score": .99,
                                  "entity_group": kind, "word": word})
            results.append(found)
        return results

    detector._pipe = pipe
    monkeypatch.setattr("tuomin_gateway.detectors.ner._NER_SINGLETON", detector)
    return detector, calls


def fixture_payload():
    text = "张三在北辰测试制造有限公司工作，电话13900001111。"
    return {"tools": [{"type": "function", "function": {"name": "lookup", "description": text,
                     "parameters": {"type": "object", "properties": {"q": {"type": "string", "description": text}}}}}],
            "messages": [{"role": "system", "content": "Keep schema intact."},
                         {"role": "user", "content": text},
                         {"role": "assistant", "content": "已联系张三。", "reasoning_content": text,
                          "tool_calls": [{"function": {"name": "lookup", "arguments": json.dumps({"q": text}, ensure_ascii=False)}}]},
                         {"role": "tool", "content": "记录：" + text}]}


def redact(payload, cache=None, scope="a", entries=None):
    profile = _profile_from_spec({"base": "strict", "refill_strict": False})
    redactor = SessionRedactor(build_detectors(entries or []), profile, identity_mode="surface")
    metrics = {}
    if cache is None:
        out = mask_request(deepcopy(payload), redactor)
    else:
        out = optimized_mask_request(deepcopy(payload), redactor, cache=cache, scope=scope, metrics=metrics)
    return out, redactor, metrics


def test_cache_keeps_order_mapping_guard_floor_and_refill(model):
    detector, calls = model
    original, baseline, _ = redact(fixture_payload())
    cache = NerCache()
    for _ in range(2):
        out, redactor, stats = redact(fixture_payload(), cache)
        assert out == original
        assert redactor.mapping == baseline.mapping
        assert redactor.trace_entries() == baseline.trace_entries()
        assert redactor.blocked_labels == baseline.blocked_labels
        assert redactor.last_readiness == baseline.last_readiness
        for placeholder, raw in redactor.mapping.items():
            assert redactor.refill(placeholder, allow_missing=True)["text"] == raw
    assert stats["cache_misses"] == 0
    before = len(calls)
    redact(fixture_payload(), cache, scope="another-app")
    assert len(calls) > before
    before = len(calls)
    detector.threshold = .8
    redact(fixture_payload(), cache)
    assert len(calls) > before


def test_changed_dictionary_still_applies_on_model_cache_hit(model):
    cache = NerCache()
    payload = {"messages": [{"role": "user", "content": "专用项目名称"}]}
    out, _, _ = redact(payload, cache)
    assert out == payload
    dictionary = [{"canonical_value": "专用项目名称", "label": "ORG"}]
    actual, redactor, stats = redact(payload, cache, entries=dictionary)
    expected, baseline, _ = redact(payload, entries=dictionary)
    assert actual == expected != payload
    assert redactor.mapping == baseline.mapping
    assert stats["cache_misses"] == 0


def test_failure_or_missing_runtime_cannot_be_hidden_by_cache(model, monkeypatch):
    detector, _ = model
    cache = NerCache()
    redact(fixture_payload(), cache)
    def broken():
        raise RuntimeError("runtime disappeared")
    monkeypatch.setattr(detector, "_pipeline", broken)
    with pytest.raises(RequiredDetectorUnavailable):
        redact(fixture_payload(), cache)


def test_partial_batch_fails_closed_and_not_cached(model, monkeypatch):
    detector, _ = model
    original = detector.detect_many
    cache = NerCache()
    monkeypatch.setattr(detector, "detect_many", lambda texts, **kwargs: [])
    with pytest.raises(RequiredDetectorUnavailable):
        redact(fixture_payload(), cache)
    monkeypatch.setattr(detector, "detect_many", original)
    _, _, stats = redact(fixture_payload(), cache)
    assert stats["cache_hits"] == 0


def test_batch_preserves_long_text_boundaries_and_offsets(model):
    detector, _ = model
    texts = ["", " ", "张三", "的" * 375 + "北辰测试制造有限公司" + "的" * 700 + "张三",
             "张三\n北辰测试制造有限公司", "📄张三，电话13900001111。"]
    assert detector.detect_many(texts) == [detector.detect(t) for t in texts]


def test_cache_bounds_expiry_and_copy_isolation(monkeypatch):
    now = [0.0]
    monkeypatch.setattr("tuomin_gateway.ner_cache.time.monotonic", lambda: now[0])
    cache = NerCache(max_entries=1, ttl=10)
    first = cache.key("app", (1,), "secret")
    second = cache.key("app", (1,), "other")
    cache.put(first, "secret", [{"metadata": {"raw": "secret"}}])
    cache.get(first)[0]["metadata"]["raw"] = "changed"
    assert cache.get(first)[0]["metadata"]["raw"] == "secret"
    assert b"secret" not in first
    cache.put(second, "other", [])
    assert cache.get(first) is None
    assert cache.get(second) == []
    now[0] = 11
    assert cache.get(second) is None


def test_cache_hits_extend_expiry_and_prune_keeps_lru_order(monkeypatch):
    now = [0.0]
    monkeypatch.setattr("tuomin_gateway.ner_cache.time.monotonic", lambda: now[0])
    cache = NerCache(ttl=10)
    hot, cold, late = (cache.key("app", (1,), t) for t in ("hot", "cold", "late"))
    cache.put(hot, "hot", ["h"])
    cache.put(cold, "cold", ["c"])
    for step in range(1, 4):
        now[0] = step * 8  # each hit lands before the previous expiry
        assert cache.get(hot) == ["h"]
    assert cache.get(cold) is None  # never hit: expired at 10 although queued behind "hot"
    now[0] = 30
    cache.put(late, "late", ["l"])
    now[0] = 35  # hot's last hit at 24 expired at 34; late lives until 40
    assert cache.get(hot) is None and cache.get(late) == ["l"]
    assert len(cache._items) == 1 and cache._bytes == cache._items[late][2]


def test_batches_group_similar_lengths_and_return_caller_order(model):
    detector, calls = model
    texts = [("张三" if i % 2 else "北辰测试制造有限公司") + "的" * (i * 7) for i in range(40)]
    expected = [detector.detect(t) for t in texts]
    calls.clear()
    assert detector.detect_many(texts) == expected
    batches = [[len(t) for t in batch] for batch in calls]
    assert [len(b) for b in batches] == [16, 16, 8]
    assert all(max(a) <= min(b) for a, b in zip(batches, batches[1:]))


def test_growing_long_text_reuses_windows_and_matches_full_detection(model):
    detector, calls = model
    base = "".join(f"第{i}段：张三在北辰测试制造有限公司负责资料整理。\n" for i in range(60))
    cache = NerCache()
    payload = lambda text: {"messages": [{"role": "user", "content": text}]}
    redact(payload(base), cache)
    grown = base + "补充：张三已联系北辰测试制造有限公司。\n"
    expected, baseline, _ = redact(payload(grown))
    calls.clear()
    out, redactor, stats = redact(payload(grown), cache)
    assert out == expected and redactor.mapping == baseline.mapping
    reused = stats["window_cache_hits"]
    assert reused >= 3 and stats["window_cache_misses"] >= 1
    windows = [w for batch in calls for w in batch]
    assert len(windows) == stats["window_cache_misses"]
    # A short field never fills the window layer; whole-text caching covers it.
    _, _, short = redact(payload("张三在北辰测试制造有限公司工作。"), cache)
    assert short["window_cache_hits"] == short["window_cache_misses"] == 0


def test_identical_windows_in_one_request_are_inferred_once(model):
    detector, calls = model
    line = "张三在北辰测试制造有限公司工作，需要按原格式整理资料并保留全部正文。\n"
    text = line * 60
    calls.clear()
    expected = detector.detect(text)
    every_window = sum(len(batch) for batch in calls)
    calls.clear()
    assert detector.detect_many([text]) == [expected]
    unique = [w for batch in calls for w in batch]
    assert len(unique) == len(set(unique)) < every_window


class Request:
    disconnected = False
    async def is_disconnected(self):
        return self.disconnected


def test_queue_does_not_block_loop_and_disconnect_stops_work():
    async def scenario():
        queue, request, event = ProxyWorkQueue(capacity=1), Request(), threading.Event()
        entered, exited = threading.Event(), threading.Event()
        def work():
            entered.set()
            try:
                while True:
                    cancellation.check()
                    time.sleep(.005)
            finally:
                exited.set()
        task = asyncio.create_task(queue.run(request, event, work, {}))
        for _ in range(100):
            if entered.is_set(): break
            await asyncio.sleep(.01)
        assert entered.is_set()
        with pytest.raises(ProxyBusy):
            await queue.run(Request(), threading.Event(), lambda: None, {})
        request.disconnected = True
        with pytest.raises(cancellation.Cancelled):
            await asyncio.wait_for(task, 1)
        for _ in range(100):
            if exited.is_set(): break
            await asyncio.sleep(.01)
        assert exited.is_set()
        assert await queue.run(Request(), threading.Event(), lambda: 42, {}) == 42
    asyncio.run(scenario())


def test_proxy_timing_and_live_health_while_masking(monkeypatch, tmp_path):
    from tuomin_gateway.service import proxy
    original = proxy.optimized_mask_request
    entered = threading.Event()
    def slow(*args, **kwargs):
        entered.set()
        time.sleep(.15)
        return original(*args, **kwargs)
    monkeypatch.setattr(proxy, "optimized_mask_request", slow)
    app = FastAPI()
    app.get("/healthz")(lambda: {"status": "ok"})
    async def forward(*args, **kwargs):
        return ForwardResult(200, body=b'{"choices":[]}')
    register_proxy(app, AppRegistry({"test": {"profile": "kb", "proxy_upstreams": {"openai": "https://example.com/chat"}}}),
                   forwarder=forward, audit_log=AuditLog(tmp_path / "audit"))
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://local") as client:
            task = asyncio.create_task(client.post("/apps/test/v1/chat/completions", json={"messages": [{"role": "user", "content": "fixture"}]}))
            while not entered.is_set(): await asyncio.sleep(.005)
            assert (await client.get("/healthz")).status_code == 200
            assert not task.done()
            response = await task
            assert response.status_code == 200
            assert "mask;dur=" in response.headers["server-timing"]
    asyncio.run(scenario())
    log = (tmp_path / "audit/proxy-performance.jsonl").read_text()
    assert "fixture" not in log and "example.com" not in log
    assert '"mask_ms"' in log and '"upstream_headers_ms"' in log


@pytest.mark.parametrize("mode", ["masked", "demo", "auto"])
def test_same_frame_reasoning_and_content_refill_independently(model, mode):
    from fastapi.testclient import TestClient
    app = FastAPI()
    text = "张三在北辰测试制造有限公司工作，电话13900001111。"
    sent = []
    async def forward(url, headers, body, *, stream):
        masked = json.loads(body)["messages"][0]["content"]
        sent.append(masked)
        async def chunks():
            for start in range(0, len(masked), 7):
                fragment = masked[start:start+7]
                yield ("data: " + json.dumps({"choices": [{"delta": {"content": fragment, "reasoning_content": fragment}}]}) + "\n\n").encode()
            yield b"data: [DONE]\n\n"
        return ForwardResult(200, aiter=chunks())
    register_proxy(app, AppRegistry({"test": {"profile": {"base": "strict", "refill_strict": False},
                   "allow_auto_refill": True, "proxy_upstreams": {"openai": "https://example.com/chat"}}}), forwarder=forward)
    segment = "" if mode == "masked" else mode + "/"
    with TestClient(app) as client:
        response = client.post(f"/apps/test/{segment}v1/chat/completions", json={"stream": True, "messages": [{"role": "user", "content": text}]})
    assert response.status_code == 200 and "data: [DONE]" in response.text
    fields = {"content": "", "reasoning_content": ""}
    for line in response.text.splitlines():
        if line.startswith("data: ") and line != "data: [DONE]":
            for choice in json.loads(line[6:]).get("choices", []):
                for field in fields:
                    fields[field] += choice.get("delta", {}).get(field, "")
    assert fields["content"] == (sent[0] if mode == "masked" else text)
    assert fields["reasoning_content"] == (text if mode == "auto" else sent[0])
