"""Unified local reverse proxy (pillar 1).

One local endpoint many agents can point their base URL at. It masks the
OUTBOUND request with the neutral engine (in-process, no HTTP hop), forwards to
the real upstream provider, and returns the response MASKED by default — a
registered app reads its mapping handle from the response headers and refills
via the trusted v1 API. The old automatic refill (non-streaming JSON and SSE
alike, incl. placeholders split across stream chunks) only runs under
``TUOMIN_PROXY_LEGACY_AUTO_REFILL=1``.

Per-app project endpoints (e.g. for WorkBuddy-style desktop agents that can
only configure a URL + API key): ``/apps/{app_id}/v1/...`` pins the app via
the URL path — the operator-deployed path beats both the process-wide
``TUOMIN_PROXY_APP_ID`` pin and client headers, an unknown app id fails closed
(404, no strict-profile fallback), and responses stay masked.
``/apps/{app_id}/auto/v1/...`` transparently refills in the proxy, but only
when the app registry entry declares ``allow_auto_refill: true``; otherwise it
fails closed with 403 before any upstream call.
``/apps/{app_id}/demo/v1/...`` (same gate) refills content/tool calls but
keeps chain-of-thought masked as visible redaction evidence; reasoning is a
display-only payload and is dropped from outbound history entirely.

The input/output guard runs on every request; warn/block alerts ride safe
response headers where possible and ALWAYS land in the durable guard-alerts
audit stream (the only channel a streaming response can reach).

Format-preserving by design (v1): ``/v1/messages`` proxies an Anthropic-style
upstream, ``/v1/chat/completions`` an OpenAI-style one. We mask/refill where the
text lives in each shape; we do NOT translate between the two formats (that can
come later). Claude Code keeps using the JS CCR transformer; this proxy is the
agent-agnostic path for everything else.

Fail-closed: any masking/forward/refill error returns an error and never leaks
plaintext. Secrets/keys in headers are forwarded to the upstream but never
logged or written to snapshots.
"""
from __future__ import annotations

import codecs
from collections import Counter
import ipaddress
import json
import os
import re
import secrets
import socket
import threading
import time
from typing import Any, AsyncIterator, Awaitable, Callable
from urllib.parse import urlparse

from fastapi import Body, FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

from tuomin_gateway import guard
from tuomin_gateway.inspection import InspectionVault
from tuomin_gateway.audit import AUDIT_STREAM_GUARD, AuditLog
from tuomin_gateway.policy import POLICY_SCHEMA_VERSION, PROFILE_COMPATIBILITY
from tuomin_gateway.placeholders import (
    PLACEHOLDER_RE,
    reserved_placeholder_conflict,
    substitute,
)
from tuomin_gateway.service.limits import max_request_bytes
from tuomin_gateway.service.registry import (
    AppRegistry,
    CapabilityDenied,
    PROXY_UPSTREAM_ENV,
    ProfileOverrideDenied,
    RegistryConfigurationError,
)
from tuomin_gateway.session import (
    RequiredDetectorUnavailable,
    SessionRedactor,
    build_detectors,
)
from tuomin_gateway.store import MappingStore
from tuomin_gateway.vault import MappingVault

# A forwarder sends the (already masked) request upstream and returns the raw
# response. Injected so tests can supply a fake upstream with no network.
#   non-stream -> ForwardResult(status, headers, body=bytes)
#   stream     -> ForwardResult(status, headers, aiter=async-iterator-of-bytes)
Forwarder = Callable[..., Awaitable["ForwardResult"]]

_PLACEHOLDER_PREFIX_RE = re.compile(r"^<[A-Z][A-Z0-9_]*$")
# App ids in the /apps/{app_id}/... proxy path: conservative charset so a
# crafted path segment can never smuggle odd characters into logs/audit.
_PATH_APP_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
# Only these request headers are forwarded upstream — an allowlist, so a client's
# cookies and arbitrary headers are never relayed to the provider (or to an
# attacker-chosen host). Auth values pass through but are never logged/persisted.
_FORWARD_REQUEST_HEADERS = {
    "authorization", "x-api-key", "content-type",
    "anthropic-version", "anthropic-beta",
    "openai-organization", "openai-beta",
}


class PlaceholderIntegrityError(RuntimeError):
    pass


class ForwardResult:
    def __init__(
        self,
        status: int,
        headers: dict[str, str] | None = None,
        body: bytes | None = None,
        aiter: AsyncIterator[bytes] | None = None,
    ) -> None:
        self.status = status
        self.headers = headers or {}
        self.body = body
        self.aiter = aiter


# --------------------------------------------------------------------------
# SSE split-placeholder buffering (ported from integrations/ccr/tuomin-transformer.cjs)
# --------------------------------------------------------------------------
def split_safe_text(text: str) -> tuple[str, str]:
    """Return (safe, rest): hold back a trailing partial placeholder like ``<ORG_``.

    Anything before an incomplete ``<...`` (no closing ``>`` yet, and shaped like
    a placeholder prefix) is safe to refill now; the partial tail is buffered
    until the next chunk completes it.
    """
    last_open = text.rfind("<")
    if last_open == -1:
        return text, ""
    suffix = text[last_open:]
    if ">" in suffix or not _looks_like_placeholder_prefix(suffix):
        return text, ""
    return text[:last_open], suffix


def _looks_like_placeholder_prefix(value: str) -> bool:
    return value == "<" or bool(_PLACEHOLDER_PREFIX_RE.match(value))


# --------------------------------------------------------------------------
# Request masking (Anthropic + OpenAI message shapes)
# --------------------------------------------------------------------------
def _mask_text(redactor: SessionRedactor, text: Any) -> Any:
    if not isinstance(text, str) or not text:
        return text
    return redactor.mask(text)


def _mask_string_leaves(redactor: SessionRedactor, value: Any) -> Any:
    """Mask prose values inside tool inputs without changing their JSON shape."""
    if isinstance(value, str):
        return _mask_text(redactor, value)
    if isinstance(value, list):
        return [_mask_string_leaves(redactor, item) for item in value]
    if isinstance(value, dict):
        return {
            key: _mask_string_leaves(redactor, child)
            for key, child in value.items()
        }
    return value


def _mask_descriptions(redactor: SessionRedactor, value: Any) -> Any:
    """Mask only free-form tool descriptions; keep protocol/schema fields exact."""
    if isinstance(value, list):
        return [_mask_descriptions(redactor, item) for item in value]
    if isinstance(value, dict):
        return {
            key: (
                _mask_text(redactor, child)
                if key == "description" and isinstance(child, str)
                else _mask_descriptions(redactor, child)
            )
            for key, child in value.items()
        }
    return value


def _mask_blocks(redactor: SessionRedactor, content: Any) -> Any:
    """Mask a message ``content`` that is either a string or a list of blocks."""
    if isinstance(content, str):
        return _mask_text(redactor, content)
    if isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype in ("text", "input_text", "output_text") and "text" in block:
                block["text"] = _mask_text(redactor, block["text"])
            elif btype == "tool_result":
                block["content"] = _mask_blocks(redactor, block.get("content"))
            elif btype == "tool_use" and isinstance(block.get("input"), dict):
                block["input"] = _mask_string_leaves(redactor, block["input"])
    return content


def mask_request(payload: dict, redactor: SessionRedactor, *, include_system: bool = False) -> dict:
    """Mask user/assistant/tool message text in place. System left clear by default
    (placeholders in system instructions tend to confuse the model)."""
    if include_system and "system" in payload:
        payload["system"] = _mask_blocks(redactor, payload["system"])
    if isinstance(payload.get("tools"), list):
        payload["tools"] = _mask_descriptions(redactor, payload["tools"])
    for message in payload.get("messages", []) or []:
        if not isinstance(message, dict):
            continue
        if not include_system and message.get("role") == "system":
            continue
        if "content" in message:
            message["content"] = _mask_blocks(redactor, message["content"])
        # DeepSeek-style reasoning echo in history: it was refilled to real
        # values before the client saw it, so it must be re-masked upstream.
        reasoning = message.get("reasoning_content")
        if isinstance(reasoning, str):
            message["reasoning_content"] = _mask_text(redactor, reasoning)
        # OpenAI assistant tool calls carry args as a JSON string.
        for call in message.get("tool_calls", []) or []:
            fn = call.get("function") if isinstance(call, dict) else None
            if isinstance(fn, dict):
                arguments = fn.get("arguments")
                if isinstance(arguments, str):
                    fn["arguments"] = _mask_text(redactor, arguments)
                elif isinstance(arguments, (dict, list)):
                    fn["arguments"] = _mask_string_leaves(redactor, arguments)
    return payload


# --------------------------------------------------------------------------
# Response refill (recursive, safe — only known placeholders are substituted)
# --------------------------------------------------------------------------
def _refill_string(
    redactor: SessionRedactor,
    text: str,
    stats: dict[str, int],
    *,
    allow_missing: bool = False,
) -> str:
    result = redactor.refill(text, allow_missing=allow_missing)
    stats["restored"] += result["restored_count"]
    for ph in result["unknown_placeholders"]:
        stats["unknown"].add(ph)
    if result.get("status") == "blocked":
        error_types = ", ".join(result.get("error_types") or ["placeholder_integrity"])
        raise PlaceholderIntegrityError(error_types)
    return result["text"]


def refill_json(
    obj: Any,
    redactor: SessionRedactor,
    stats: dict[str, int],
    *,
    allow_missing: bool = False,
    skip_keys: frozenset[str] | None = None,
) -> Any:
    """Validate one complete JSON response, then refill all string leaves.

    Strict refill is document-wide: validating each leaf independently makes a
    normal field such as ``id`` look as if it omitted every known placeholder.
    We therefore validate the union of all string leaves once, then perform only
    known-placeholder substitution while preserving the original JSON shape.
    ``skip_keys`` (demo mode: reasoning/thinking fields) are excluded from both
    validation and refill, reaching the client still masked.
    """
    strings: list[str] = []
    skipped = skip_keys or frozenset()

    def collect(value: Any, skip: bool = False) -> None:
        if skip:
            return
        if isinstance(value, str):
            strings.append(value)
        elif isinstance(value, list):
            for item in value:
                collect(item)
        elif isinstance(value, dict):
            for key, item in value.items():
                collect(item, skip=key in skipped)

    collect(obj)
    validation = redactor.refill("\n".join(strings), allow_missing=allow_missing)
    for ph in validation["unknown_placeholders"]:
        stats["unknown"].add(ph)
    if validation.get("status") == "blocked":
        error_types = ", ".join(validation.get("error_types") or ["placeholder_integrity"])
        raise PlaceholderIntegrityError(error_types)

    def restore(value: Any, skip: bool = False) -> Any:
        if skip:
            return value
        if isinstance(value, str):
            restored, count = substitute(value, redactor.mapping)
            stats["restored"] += count
            return restored
        if isinstance(value, list):
            return [restore(item) for item in value]
        if isinstance(value, dict):
            return {
                key: restore(item, skip=key in skipped)
                for key, item in value.items()
            }
        return value

    return restore(obj)


def _stream_text_paths(chunk: dict, *, include_reasoning: bool = True) -> list[list]:
    """Return all generated-text paths in an OpenAI/Anthropic stream chunk.

    ``include_reasoning=False`` (demo mode) skips chain-of-thought fields so
    they reach the local client still masked, as visible redaction evidence."""
    paths: list[list] = []
    choices = chunk.get("choices")
    if isinstance(choices, list):
        for i, choice in enumerate(choices):
            if not isinstance(choice, dict):
                continue
            delta = choice.get("delta")
            if isinstance(delta, dict):
                if isinstance(delta.get("content"), str):
                    paths.append(["choices", i, "delta", "content"])
                if include_reasoning and isinstance(delta.get("reasoning_content"), str):
                    # DeepSeek-style chain-of-thought: generated in masked
                    # space, so it must be refilled for local display too.
                    paths.append(["choices", i, "delta", "reasoning_content"])
                if isinstance(delta.get("text"), str):
                    paths.append(["choices", i, "delta", "text"])
                tool_calls = delta.get("tool_calls")
                if isinstance(tool_calls, list):
                    for j, call in enumerate(tool_calls):
                        function = (
                            call.get("function")
                            if isinstance(call, dict)
                            else None
                        )
                        if isinstance(function, dict) and isinstance(
                            function.get("arguments"), str
                        ):
                            paths.append(
                                [
                                    "choices",
                                    i,
                                    "delta",
                                    "tool_calls",
                                    j,
                                    "function",
                                    "arguments",
                                ]
                            )
            message = choice.get("message")
            if isinstance(message, dict) and isinstance(message.get("content"), str):
                paths.append(["choices", i, "message", "content"])
                continue
            if isinstance(choice.get("text"), str):
                paths.append(["choices", i, "text"])
    if paths:
        return paths
    delta = chunk.get("delta")
    if isinstance(delta, dict) and isinstance(delta.get("text"), str):
        paths.append(["delta", "text"])
    if isinstance(delta, dict) and isinstance(delta.get("partial_json"), str):
        paths.append(["delta", "partial_json"])
    if isinstance(delta, dict) and isinstance(delta.get("thinking"), str):
        if include_reasoning:
            paths.append(["delta", "thinking"])
    if isinstance(chunk.get("content"), str):
        paths.append(["content"])
    if isinstance(chunk.get("text"), str):
        paths.append(["text"])
    return paths


def _first_stream_text(chunk: dict, *, include_reasoning: bool = True) -> list | None:
    """Compatibility helper for the lenient, low-latency stream path."""
    paths = _stream_text_paths(chunk, include_reasoning=include_reasoning)
    return paths[0] if paths else None


def _get_at(obj: Any, path: list) -> Any:
    for seg in path:
        obj = obj[seg]
    return obj


def _set_at(obj: Any, path: list, value: Any) -> None:
    for seg in path[:-1]:
        obj = obj[seg]
    obj[path[-1]] = value


def _parse_sse_block(block: str) -> tuple[list[str], str | None]:
    """Split one SSE event block into its non-data lines and its joined data.

    Handles the Anthropic shape ``event: <type>\\ndata: {...}`` (the block starts
    with ``event:``, not ``data:``) and multi-line ``data:`` per the SSE spec.
    Returns ``(prefix_lines, data)`` where ``data`` is None for a dataless block.
    """
    prefix: list[str] = []
    data_parts: list[str] = []
    for line in block.split("\n"):
        if line.startswith("data:"):
            data_parts.append(line[len("data:"):].lstrip(" "))
        else:
            prefix.append(line)
    return prefix, ("\n".join(data_parts) if data_parts else None)


def _emit_sse(prefix_lines: list[str], data: str | None) -> bytes:
    lines = [ln for ln in prefix_lines if ln != ""]
    if data is not None:
        lines.append(f"data: {data}")
    return ("\n".join(lines) + "\n\n").encode("utf-8")


def _placeholder_integrity_event(error_types: list[str]) -> bytes:
    return _emit_sse(
        ["event: error"],
        json.dumps(
            {
                "error": {
                    "code": "placeholder_integrity",
                    "message": "tuomin fail-closed: streamed response failed placeholder integrity",
                    "error_types": error_types,
                }
            },
            ensure_ascii=False,
        ),
    )


def _guard_stream_block_event(events) -> bytes:
    return _emit_sse(
        ["event: error"],
        json.dumps(
            {"error": {"message": "tuomin guard blocked streamed response"},
             "alerts": [e.to_safe_dict() for e in events]},
            ensure_ascii=False,
        ),
    )


def _audit_guard_events(
    audit_log: AuditLog | None,
    events,
    *,
    channel: str,
    app_id: str | None = None,
) -> None:
    """Persist guard alert events (warn AND block) to the durable audit log.

    This is the WARN channel for cases response headers cannot reach — above
    all streaming, where headers are already sent before the output guard sees
    a single byte. Events carry only safe dicts (hashes/counts, no raw text).
    Audit failures must never break the request path.
    """
    if audit_log is None or not events:
        return
    for event in events:
        record = {"event": "guard_alert", "channel": channel, **event.to_safe_dict()}
        if app_id:
            record["app_id"] = app_id
        try:
            audit_log.write(AUDIT_STREAM_GUARD, record)
        except OSError:
            pass


class _StreamOutputGuard:
    """Rolling-window output scanner for SSE streams.

    A secret can straddle chunk boundaries (``sk-`` in one delta, the rest in
    the next), so each emitted text segment is scanned together with a tail of
    the text that came before it. The guard only watches — frames pass through
    byte-identical and nothing is held back, so it adds no latency.
    """

    def __init__(self, profile, window: int = 1024) -> None:
        self._profile = profile
        self._window = window
        self._tail = ""

    def scan(self, text: str) -> guard.GuardOutcome:
        outcome = guard.scan_output(self._tail + text, self._profile)
        self._tail = (self._tail + text)[-self._window:]
        return outcome


async def _guarded_passthrough_stream(
    aiter: AsyncIterator[bytes],
    profile,
    on_guard_events: Callable[[list], None] | None = None,
) -> AsyncIterator[bytes]:
    """Default masked streaming mode: frames pass through unchanged while the
    output guard watches the generated text (the model may still emit real
    secrets). A blocking hit ends the stream with an ``event: error`` block —
    the buffered bytes carrying the secret are dropped, never forwarded.
    Warn-level events cannot ride response headers here (already sent), so
    they are reported via ``on_guard_events`` (durable audit) instead."""
    rolling = _StreamOutputGuard(profile)
    event_buffer = ""
    decoder = codecs.getincrementaldecoder("utf-8")()

    def inspect(block: str) -> list:
        """Scan one SSE block's generated text; report all alert events via the
        callback and return the blocking subset."""
        _prefix, data = _parse_sse_block(block)
        if data is None or data.strip() == "[DONE]":
            return []
        try:
            chunk = json.loads(data)
        except json.JSONDecodeError:
            return []
        events: list = []
        for path in _stream_text_paths(chunk):
            outcome = rolling.scan(_get_at(chunk, path))
            events.extend(outcome.events)
        if events and on_guard_events is not None:
            on_guard_events(events)
        return [e for e in events if e.action == "block"]

    async for raw in aiter:
        event_buffer = (event_buffer + decoder.decode(raw)).replace("\r\n", "\n")
        while "\n\n" in event_buffer:
            block, event_buffer = event_buffer.split("\n\n", 1)
            blocking = inspect(block)
            if blocking:
                yield _guard_stream_block_event(blocking)
                return
        yield raw
    event_buffer = (event_buffer + decoder.decode(b"", final=True)).replace("\r\n", "\n")
    while event_buffer.strip():
        block, sep, event_buffer = event_buffer.partition("\n\n")
        blocking = inspect(block)
        if blocking:
            yield _guard_stream_block_event(blocking)
            return
        if not sep:
            break


async def _strict_refill_event_stream(
    aiter: AsyncIterator[bytes],
    redactor: SessionRedactor,
    stats: dict[str, int],
    profile,
    on_guard_events: Callable[[list], None] | None = None,
    allow_missing: bool = False,
    include_reasoning: bool = True,
) -> AsyncIterator[bytes]:
    """Buffer strict SSE until document-wide integrity is known.

    A streaming response cannot be retracted after bytes are emitted. Strict
    profiles therefore trade latency for correctness: hold the complete stream,
    validate the concatenated generated text once, then replay it through the
    regular split-placeholder refiller with per-fragment missing checks disabled.
    ``include_reasoning=False`` (demo mode) excludes chain-of-thought fields
    from validation and refill so they reach the client still masked.
    """
    raw_chunks: list[bytes] = []
    async for raw in aiter:
        raw_chunks.append(raw)

    try:
        normalized = b"".join(raw_chunks).decode("utf-8").replace("\r\n", "\n")
    except UnicodeDecodeError:
        yield _placeholder_integrity_event(["invalid_utf8"])
        return

    parsed_events: list[tuple[list[str], str | None, dict | None, list[list]]] = []
    generated_by_path: dict[tuple, list[str]] = {}
    for block in normalized.split("\n\n"):
        if not block.strip():
            continue
        prefix, data = _parse_sse_block(block)
        if data is None or data.strip() == "[DONE]":
            parsed_events.append((prefix, data, None, []))
            continue
        try:
            chunk = json.loads(data)
        except json.JSONDecodeError:
            yield _placeholder_integrity_event(["invalid_sse_json"])
            return
        paths = _stream_text_paths(chunk, include_reasoning=include_reasoning)
        parsed_events.append((prefix, data, chunk, paths))
        for path in paths:
            generated_by_path.setdefault(tuple(path), []).append(_get_at(chunk, path))

    complete_texts = {
        path: "".join(parts) for path, parts in generated_by_path.items()
    }
    if any(split_safe_text(value)[1] for value in complete_texts.values()):
        yield _placeholder_integrity_event(["altered_placeholder"])
        return

    validation = redactor.refill(
        "\n".join(complete_texts.values()), allow_missing=allow_missing
    )
    for ph in validation["unknown_placeholders"]:
        stats["unknown"].add(ph)
    if validation.get("status") == "blocked":
        yield _placeholder_integrity_event(
            validation.get("error_types") or ["placeholder_integrity"]
        )
        return

    strict_guard = guard.scan_output("\n".join(complete_texts.values()), profile)
    if strict_guard.events and on_guard_events is not None:
        on_guard_events(strict_guard.events)
    if strict_guard.blocking:
        yield _emit_sse(
            ["event: error"],
            json.dumps(
                {
                    "error": {"message": "tuomin guard blocked streamed response"},
                    "alerts": [event.to_safe_dict() for event in strict_guard.blocking],
                },
                ensure_ascii=False,
            ),
        )
        return

    buffers: dict[tuple, str] = {}
    for prefix, data, original_chunk, paths in parsed_events:
        if data is None:
            yield _emit_sse(prefix, None)
            continue
        if data.strip() == "[DONE]":
            yield _emit_sse(prefix, "[DONE]")
            continue
        if original_chunk is None:  # defensive; invalid JSON returned above
            continue
        chunk = json.loads(json.dumps(original_chunk))
        emitted_text = False
        for path in paths:
            key = tuple(path)
            buffers[key] = buffers.get(key, "") + _get_at(chunk, path)
            safe, rest = split_safe_text(buffers[key])
            buffers[key] = rest
            if safe:
                _set_at(
                    chunk,
                    path,
                    _refill_string(redactor, safe, stats, allow_missing=True),
                )
                emitted_text = True
            else:
                _set_at(chunk, path, "")
        # Never drop a protocol frame merely because its text fragment is
        # empty or temporarily buffered.  OpenAI tool-call streams put the
        # call ``id`` and function name in an initial frame whose
        # ``arguments`` is often ``""``; dropping that frame leaves clients
        # unable to assemble the later argument deltas.
        yield _emit_sse(prefix, json.dumps(chunk, ensure_ascii=False))


async def _refill_event_stream(
    aiter: AsyncIterator[bytes],
    redactor: SessionRedactor,
    stats: dict[str, int],
    profile=None,
    *,
    document_validated: bool = False,
    on_guard_events: Callable[[list], None] | None = None,
    include_reasoning: bool = True,
) -> AsyncIterator[bytes]:
    """Refill an SSE stream, buffering placeholders split across chunks, and run
    the output guard on each emitted segment with a rolling window (a secret
    split across chunks is still caught). A blocking guard hit ends the stream
    with an ``event: error`` block rather than passing the text on; warn-level
    events are reported via ``on_guard_events`` (durable audit).
    ``include_reasoning=False`` (demo mode) passes chain-of-thought fields
    through still masked, as visible redaction evidence."""
    event_buffer = ""
    text_buffer = ""
    last_chunk: dict | None = None
    last_path: list | None = None
    last_prefix: list[str] | None = None
    blocked = False
    decoder = codecs.getincrementaldecoder("utf-8")()
    rolling = _StreamOutputGuard(profile) if profile is not None else None

    def flush_pending() -> bytes | None:
        nonlocal text_buffer
        if not text_buffer:
            return None
        chunk = json.loads(json.dumps(last_chunk)) if last_chunk else {"choices": [{"delta": {"content": ""}}]}
        path = last_path or ["choices", 0, "delta", "content"]
        _set_at(
            chunk,
            path,
            _refill_string(
                redactor, text_buffer, stats, allow_missing=document_validated
            ),
        )
        text_buffer = ""
        return _emit_sse(last_prefix or [], json.dumps(chunk, ensure_ascii=False))

    async def process_event(block: str) -> AsyncIterator[bytes]:
        nonlocal text_buffer, last_chunk, last_path, last_prefix, blocked
        if not block.strip():
            return
        prefix, data = _parse_sse_block(block)
        if data is None:
            yield _emit_sse(prefix, None)
            return
        if data.strip() == "[DONE]":
            pending = flush_pending()
            if pending:
                yield pending
            yield _emit_sse(prefix, "[DONE]")
            return
        try:
            chunk = json.loads(data)
        except json.JSONDecodeError:
            yield _emit_sse(prefix, data)
            return
        path = _first_stream_text(chunk, include_reasoning=include_reasoning)
        if path is None:
            yield _emit_sse(prefix, json.dumps(chunk, ensure_ascii=False))
            return
        text_buffer += _get_at(chunk, path)
        last_chunk, last_path, last_prefix = chunk, path, prefix
        safe, rest = split_safe_text(text_buffer)
        text_buffer = rest
        if not safe:
            # Preserve non-text stream metadata (notably tool-call id/name)
            # while holding back a partial placeholder or empty argument
            # fragment.  An empty text delta is protocol-safe.
            _set_at(chunk, path, "")
            yield _emit_sse(prefix, json.dumps(chunk, ensure_ascii=False))
            return
        # Output guard on the masked-space segment (model-emitted secrets are in
        # clear here; placeholders are refilled below). Blocking ends the stream.
        if rolling is not None:
            seg_guard = rolling.scan(safe)
            if seg_guard.events and on_guard_events is not None:
                on_guard_events(seg_guard.events)
            if seg_guard.blocking:
                blocked = True
                yield _guard_stream_block_event(seg_guard.blocking)
                return
        _set_at(
            chunk,
            path,
            _refill_string(redactor, safe, stats, allow_missing=document_validated),
        )
        yield _emit_sse(prefix, json.dumps(chunk, ensure_ascii=False))

    async for raw in aiter:
        if blocked:
            break
        event_buffer = (event_buffer + decoder.decode(raw)).replace("\r\n", "\n")
        while "\n\n" in event_buffer:
            event, event_buffer = event_buffer.split("\n\n", 1)
            async for out in process_event(event):
                yield out
            if blocked:
                return
    event_buffer = (event_buffer + decoder.decode(b"", final=True)).replace("\r\n", "\n")
    if event_buffer.strip():
        async for out in process_event(event_buffer):
            yield out
        if blocked:
            return
    pending = flush_pending()
    if pending:
        yield pending


# --------------------------------------------------------------------------
# Wiring
# --------------------------------------------------------------------------
def collect_text(payload: dict) -> str:
    """Concatenate all natural-language text (system + messages + tool args) for
    the input guard to inspect — on the ORIGINAL text, before masking."""
    parts: list[str] = []

    def walk(content: Any) -> None:
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict):
                    if isinstance(block.get("text"), str):
                        parts.append(block["text"])
                    if "content" in block:
                        walk(block.get("content"))
                    if block.get("type") == "tool_use":
                        walk_string_leaves(block.get("input"))

    def walk_string_leaves(value: Any) -> None:
        if isinstance(value, str):
            parts.append(value)
        elif isinstance(value, list):
            for item in value:
                walk_string_leaves(item)
        elif isinstance(value, dict):
            for item in value.values():
                walk_string_leaves(item)

    def walk_descriptions(value: Any) -> None:
        if isinstance(value, list):
            for item in value:
                walk_descriptions(item)
        elif isinstance(value, dict):
            for key, item in value.items():
                if key == "description" and isinstance(item, str):
                    parts.append(item)
                else:
                    walk_descriptions(item)

    if payload.get("system") is not None:
        walk(payload["system"])
    walk_descriptions(payload.get("tools"))
    for message in payload.get("messages", []) or []:
        if not isinstance(message, dict):
            continue
        if "content" in message:
            walk(message["content"])
        reasoning = message.get("reasoning_content")
        if isinstance(reasoning, str):
            parts.append(reasoning)
        for call in message.get("tool_calls", []) or []:
            fn = call.get("function") if isinstance(call, dict) else None
            if isinstance(fn, dict):
                walk_string_leaves(fn.get("arguments"))
    return "\n".join(parts)


def collect_masked_text(payload: dict, *, include_system: bool = False) -> str:
    """Concatenate exactly the text ``mask_request`` would transform, on the
    ORIGINAL payload — used for the reserved-placeholder forgery check. System
    content is excluded by default (it is not masked, so a literal placeholder
    there cannot be confused with a real one downstream)."""
    parts: list[str] = []

    def walk(content: Any) -> None:
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    continue
                btype = block.get("type")
                if btype in ("text", "input_text", "output_text") and isinstance(block.get("text"), str):
                    parts.append(block["text"])
                elif btype == "tool_result":
                    walk(block.get("content"))
                elif btype == "tool_use":
                    walk_string_leaves(block.get("input"))

    def walk_string_leaves(value: Any) -> None:
        if isinstance(value, str):
            parts.append(value)
        elif isinstance(value, list):
            for item in value:
                walk_string_leaves(item)
        elif isinstance(value, dict):
            for item in value.values():
                walk_string_leaves(item)

    def walk_descriptions(value: Any) -> None:
        if isinstance(value, list):
            for item in value:
                walk_descriptions(item)
        elif isinstance(value, dict):
            for key, item in value.items():
                if key == "description" and isinstance(item, str):
                    parts.append(item)
                else:
                    walk_descriptions(item)

    if include_system and "system" in payload:
        walk(payload["system"])
    walk_descriptions(payload.get("tools"))
    for message in payload.get("messages", []) or []:
        if not isinstance(message, dict):
            continue
        if not include_system and message.get("role") == "system":
            continue
        if "content" in message:
            walk(message["content"])
        reasoning = message.get("reasoning_content")
        if isinstance(reasoning, str):
            parts.append(reasoning)
        for call in message.get("tool_calls", []) or []:
            fn = call.get("function") if isinstance(call, dict) else None
            if isinstance(fn, dict):
                walk_string_leaves(fn.get("arguments"))
    return "\n".join(parts)


def _upstream_headers(request: Request) -> dict[str, str]:
    """Forward only an allowlist of auth/content headers to the upstream. Cookies
    and arbitrary client headers are never relayed. Auth values pass through but
    are never logged or persisted here."""
    headers = {
        key: value
        for key, value in request.headers.items()
        if key.lower() in _FORWARD_REQUEST_HEADERS
    }
    headers.setdefault("content-type", "application/json")
    return headers


def _upstream_allowlist() -> set[str]:
    raw = os.environ.get("TUOMIN_UPSTREAM_ALLOWLIST", "")
    return {host.strip().lower() for host in raw.split(",") if host.strip()}


def _is_blocked_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return (
        ip.is_loopback
        or ip.is_private
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
    )


def _is_blocked_upstream_host(host: str) -> bool:
    """Block loopback/private/link-local/reserved/multicast upstream targets so
    the proxy can't be used as an SSRF pivot to localhost services or cloud
    metadata. ``ipaddress`` only parses canonical literals, so anything else —
    inet_aton forms the OS resolver accepts (``127.1``, ``2130706433``,
    ``0x7f000001``) as well as DNS names — is resolved via getaddrinfo and
    EVERY returned address is checked. A resolution failure blocks (fail-closed:
    an unresolvable host could not be forwarded anyway).

    DNS rebinding TOCTOU remains: the address checked here can differ from the
    one httpx later dials. Pin egress with TUOMIN_UPSTREAM_ALLOWLIST for a hard
    guarantee."""
    h = host.lower().rstrip(".")
    if h == "localhost" or h.endswith(".localhost"):
        return True
    try:
        return _is_blocked_ip(ipaddress.ip_address(h))
    except ValueError:
        pass
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return True  # fail-closed on DNS errors
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except (ValueError, IndexError, TypeError):
            return True
        if _is_blocked_ip(ip):
            return True
    return False


def _resolve_upstream(request: Request, env_key: str) -> str | None:
    """Resolve the upstream URL. A client-supplied ``x-tuomin-upstream`` is
    validated (scheme + SSRF guard + optional allowlist); the env-configured
    upstream is operator-set and trusted as-is. Raises ValueError on a rejected
    client value."""
    header_url = request.headers.get("x-tuomin-upstream")
    if header_url:
        parsed = urlparse(header_url)
        if parsed.scheme not in ("http", "https"):
            raise ValueError("tuomin: upstream scheme must be http or https")
        host = parsed.hostname or ""
        allowlist = _upstream_allowlist()
        if allowlist:
            if host.lower() not in allowlist:
                raise ValueError(f"tuomin: upstream host not in TUOMIN_UPSTREAM_ALLOWLIST: {host}")
        elif _is_blocked_upstream_host(host):
            raise ValueError(f"tuomin: refusing loopback/private upstream host: {host}")
        return header_url
    return os.environ.get(env_key)


def _configured_proxy_upstream(
    registry: AppRegistry, app_id: str, route: str
) -> str | None:
    env_key = PROXY_UPSTREAM_ENV[route]
    return os.environ.get(env_key) or registry.resolve_proxy_upstream(app_id, route)


def register_proxy(
    app: FastAPI,
    registry: AppRegistry,
    *,
    forwarder: Forwarder | None = None,
    store: MappingStore | None = None,
    legacy_auto_refill: bool | None = None,
    audit_log: AuditLog | None = None,
    inspection_vault: InspectionVault | None = None,
    proxy_session_ttl: int | None = None,
    job_store: Any | None = None,
) -> None:
    """Mount /v1/messages (Anthropic) and /v1/chat/completions (OpenAI)."""
    forward = forwarder or _httpx_forward
    if legacy_auto_refill is None:
        legacy_auto_refill = os.environ.get(
            "TUOMIN_PROXY_LEGACY_AUTO_REFILL", ""
        ).strip().lower() in {"1", "true", "yes"}
    mapping_vault = MappingVault(store) if store is not None else None
    if audit_log is None and mapping_vault is not None:
        audit_log = mapping_vault.audit_log

    def _record_proxy_call(
        app_id: str | None,
        started: float,
        *,
        char_count: int,
        redactor: Any | None = None,
        blocked: bool = False,
    ) -> None:
        """Value-free call-ledger row for the WebUI outbound-record panel.
        Ledger failures must never break the proxy path."""
        if job_store is None:
            return
        try:
            label_counts = (
                dict(sorted(Counter(e.label for e in redactor.mapping_entries()).items()))
                if redactor is not None
                else {}
            )
            job_store.record_call(
                entry="proxy",
                app_id=app_id or "",
                duration_ms=max(0, int((time.monotonic() - started) * 1000)),
                char_count=char_count,
                label_counts=label_counts,
                blocked=blocked,
                policy_version=POLICY_SCHEMA_VERSION,
            )
        except Exception:  # noqa: BLE001 - ledger must never break the proxy
            pass
    proxy_session_ttl = proxy_session_ttl or int(
        os.environ.get("TUOMIN_PROXY_SESSION_TTL", "3600")
    )
    if proxy_session_ttl <= 0:
        raise ValueError("proxy session TTL must be greater than zero")
    proxy_sessions: dict[str, dict[str, Any]] = {}
    proxy_sessions_lock = threading.RLock()

    def _proxy_session_token(request: Request) -> str:
        return request.headers.get("x-tuomin-proxy-session-token", "")

    def _authorize_proxy_session(
        session_id: str,
        request: Request,
        *,
        protocol: str | None = None,
    ) -> dict[str, Any] | None:
        now = time.time()
        with proxy_sessions_lock:
            expired = [
                sid
                for sid, value in proxy_sessions.items()
                if now >= value["expires_at"]
            ]
            for sid in expired:
                proxy_sessions.pop(sid, None)
            session = proxy_sessions.get(session_id)
            if session is None:
                return None
            if protocol is not None and session["provider_route"] != protocol:
                return None
            supplied = _proxy_session_token(request)
            if not supplied or not secrets.compare_digest(
                supplied, session["token"]
            ):
                return None
            session["expires_at"] = now + proxy_session_ttl
            return session

    def _close_proxy_session(session: dict[str, Any]) -> dict[str, Any]:
        snapshot: dict[str, Any] | None = None
        with session["inspection_lock"]:
            if inspection_vault is not None:
                snapshot = inspection_vault.create(
                    app_id=session["app_id"],
                    project_id=session["project_id"],
                    run_id=session["run_id"],
                    scope="followup",
                    terminal_status="closed",
                    entries=session["redactor"].mapping_entries(),
                    trace=session["redactor"].trace_entries(),
                    coverage_items=list(session["coverage_items"]),
                    detectors=session["detectors"],
                    profile=session["profile"],
                    inspection_id=session.get("inspection_id") or None,
                    coverage_complete=True,
                )
        return snapshot or {
            "inspection_id": "",
            "entry_count": 0,
            "generalization_count": 0,
            "coverage_finding_count": 0,
        }

    async def handle(
        request: Request,
        env_key: str,
        *,
        proxy_session: dict[str, Any] | None = None,
        path_app_id: str | None = None,
        path_auto_refill: bool = False,
        path_reasoning_masked: bool = False,
    ) -> Any:
        started = time.monotonic()
        raw_body = await request.body()
        if len(raw_body) > max_request_bytes():
            return JSONResponse(
                {"error": {"code": "request_too_large",
                           "message": f"request body exceeds the {max_request_bytes()} byte limit"}},
                status_code=413,
            )
        try:
            payload = json.loads(raw_body)
        except Exception:
            return JSONResponse({"error": {"message": "invalid JSON body"}}, status_code=400)

        if path_reasoning_masked:
            # Demo mode: the client keeps chain-of-thought MASKED as visible
            # redaction evidence. Reasoning is a display-only payload the
            # upstream never needs, so drop it from outbound history entirely —
            # its placeholders belong to an earlier request's mapping and would
            # otherwise trip the reserved-placeholder conflict check (and, once
            # refilled client-side, real values must never go upstream).
            for message in payload.get("messages", []) or []:
                if not isinstance(message, dict):
                    continue
                message.pop("reasoning_content", None)
                content = message.get("content")
                if isinstance(content, list):
                    message["content"] = [
                        block for block in content
                        if not (isinstance(block, dict) and block.get("type") == "thinking")
                    ]

        pinned_app_id = os.environ.get("TUOMIN_PROXY_APP_ID")
        if proxy_session is not None:
            app_id = proxy_session["app_id"]
            profile_name = None
            route = proxy_session["provider_route"]
        elif path_app_id is not None:
            # URL-pinned project endpoint: the operator-deployed path selects
            # the app; client app/profile headers and the process-wide env pin
            # are ignored. An unknown/odd id fails closed (404) instead of
            # silently falling back to the strict profile floor.
            if not _PATH_APP_ID_RE.match(path_app_id) or path_app_id not in registry.known_apps():
                return JSONResponse(
                    {"error": {"code": "unknown_app",
                               "message": "unknown app in proxy path"}},
                    status_code=404,
                )
            app_id = path_app_id
            profile_name = None
            route = next(
                key for key, value in PROXY_UPSTREAM_ENV.items() if value == env_key
            )
        elif pinned_app_id:
            # Operator-pinned policy: client app/profile headers are ignored.
            app_id = pinned_app_id
            profile_name = None
            route = next(
                key for key, value in PROXY_UPSTREAM_ENV.items() if value == env_key
            )
        else:
            app_id = request.headers.get("x-tuomin-app-id")
            profile_name = request.headers.get("x-tuomin-profile")
            route = next(
                key for key, value in PROXY_UPSTREAM_ENV.items() if value == env_key
            )
        try:
            profile = registry.resolve_profile(app_id, profile_name)
            entries = registry.resolve_dictionary(app_id)
            if proxy_session is not None:
                # Transparent sessions pin an operator-owned destination at
                # creation.  Never let a later request header or process-wide
                # route override send another provider's credentials elsewhere.
                upstream = proxy_session["upstream"]
            else:
                upstream = _resolve_upstream(request, env_key)
                if not upstream and app_id:
                    upstream = registry.resolve_proxy_upstream(app_id, route)
        except ProfileOverrideDenied as exc:
            return JSONResponse(
                {"error": {"code": exc.code, "message": "request profile override denied"}},
                status_code=400,
            )
        except (KeyError, TypeError, ValueError):
            return JSONResponse(
                {"error": {"code": "unknown_profile", "message": "unknown or invalid profile"}},
                status_code=400,
            )
        except RegistryConfigurationError as exc:
            return JSONResponse(
                {"error": {"code": exc.code, "message": str(exc)}},
                status_code=503,
            )
        except ValueError as exc:
            return JSONResponse({"error": {"message": str(exc)}}, status_code=400)
        if not upstream:
            return JSONResponse(
                {
                    "error": {
                        "code": "proxy_upstream_unavailable",
                        "message": f"no upstream configured ({env_key}, registry, or x-tuomin-upstream)",
                    }
                },
                status_code=502,
            )

        # Response mode: transparent refill is the proxy-session default, a
        # per-app capability on /apps/{app}/auto/... (registry-gated, checked
        # before any upstream call), or the deprecated global env switch on the
        # unpinned legacy routes. Path-pinned plain routes stay masked.
        if proxy_session is not None:
            effective_auto_refill = True
            response_mode = "trusted-local-transparent"
        elif path_app_id is not None:
            if path_auto_refill or path_reasoning_masked:
                try:
                    allowed = registry.auto_refill_allowed(app_id)
                except RegistryConfigurationError as exc:
                    return JSONResponse(
                        {"error": {"code": exc.code, "message": str(exc)}},
                        status_code=503,
                    )
                if not allowed:
                    return JSONResponse(
                        {"error": {"code": "auto_refill_not_allowed",
                                   "message": "app is not authorized for transparent proxy refill"}},
                        status_code=403,
                    )
                effective_auto_refill = True
                response_mode = (
                    "app-demo-refill" if path_reasoning_masked else "app-auto-refill"
                )
            else:
                effective_auto_refill = False
                response_mode = "masked"
        else:
            effective_auto_refill = legacy_auto_refill
            response_mode = "legacy-auto-refill"

        if path_app_id is not None:
            # Operator-pinned upstream model: rewrite the request's ``model``
            # field so per-mode client entries may use free display names while
            # the upstream always receives the operator-chosen model.
            try:
                pinned_model = registry.pinned_upstream_model(app_id)
            except RegistryConfigurationError as exc:
                return JSONResponse(
                    {"error": {"code": exc.code, "message": str(exc)}},
                    status_code=503,
                )
            if pinned_model is not None:
                payload["model"] = pinned_model

        redactor = (
            proxy_session["redactor"]
            if proxy_session is not None
            else SessionRedactor(
                build_detectors(entries), profile, identity_mode="surface"
            )
        )

        # --- input guard (pillars 5 & 6): scan ORIGINAL text before masking ---
        guard_in = guard.scan_input(collect_text(payload), profile)
        _audit_guard_events(audit_log, guard_in.events, channel="proxy_input", app_id=app_id)
        if guard_in.blocking:
            return JSONResponse(
                {"error": {"message": "tuomin guard blocked input"},
                 "alerts": [e.to_safe_dict() for e in guard_in.blocking]},
                status_code=409, headers=guard_in.headers(),
            )

        # --- reserved placeholder grammar may not be forged by raw input ---
        # Same scope as mask_request (system excluded by default): a literal
        # <ORG_001> there would be indistinguishable from a real placeholder.
        if reserved_placeholder_conflict(
            collect_masked_text(
                payload, include_system=proxy_session is not None
            )
        ):
            return JSONResponse(
                {"error": {"code": "reserved_placeholder_conflict",
                           "message": "input contains reserved placeholder syntax"}},
                status_code=409,
            )

        # --- mask outbound (fail-closed) ---
        try:
            masked = mask_request(
                payload,
                redactor,
                include_system=proxy_session is not None,
            )
        except RequiredDetectorUnavailable as exc:
            return JSONResponse(
                {
                    "error": {
                        "code": "required_detector_unavailable",
                        "message": "required detector unavailable",
                    },
                    "detectors": exc.readiness.to_safe_dict(),
                },
                status_code=503,
            )
        except Exception:  # pragma: no cover - defensive
            return JSONResponse({"error": {"message": "tuomin mask failed"}}, status_code=502)

        detector_headers = {
            "x-tuomin-detectors-degraded": str(
                bool(redactor.last_readiness and redactor.last_readiness.to_safe_dict()["degraded"])
            ).lower(),
            "x-tuomin-policy-schema": POLICY_SCHEMA_VERSION,
            "x-tuomin-profile-compatibility": PROFILE_COMPATIBILITY,
        }
        if proxy_session is not None:
            with proxy_session["inspection_lock"]:
                with proxy_sessions_lock:
                    sequence = len(proxy_session["coverage_items"]) + 1
                    proxy_session["coverage_items"].append(
                        {
                            "id": f"request-{sequence:04d}",
                            "text": collect_masked_text(
                                masked, include_system=True
                            ),
                        }
                    )
                if inspection_vault is not None:
                    try:
                        inspection_vault.create(
                            app_id=proxy_session["app_id"],
                            project_id=proxy_session["project_id"],
                            run_id=proxy_session["run_id"],
                            scope="followup",
                            terminal_status="active",
                            entries=proxy_session["redactor"].mapping_entries(),
                            trace=proxy_session["redactor"].trace_entries(),
                            coverage_items=list(proxy_session["coverage_items"]),
                            detectors=proxy_session["detectors"],
                            profile=proxy_session["profile"],
                            inspection_id=proxy_session["inspection_id"],
                            coverage_complete=False,
                        )
                    except Exception:
                        return JSONResponse(
                            {
                                "error": {
                                    "code": "inspection_update_failed",
                                    "message": "inspection update failed",
                                }
                            },
                            status_code=502,
                        )
        if pinned_app_id:
            detector_headers["x-tuomin-profile-pinned"] = "env"
        mapping_handle = None
        if (
            proxy_session is None
            and mapping_vault is not None
            and app_id
            and redactor.mapping
        ):
            if "document" not in registry.allowed_mapping_scopes(app_id):
                return JSONResponse(
                    {"error": {"code": "mapping_scope_denied",
                               "message": "document mapping scope denied"}},
                    status_code=403,
                )
            serialized_masked = json.dumps(masked, ensure_ascii=False)
            mapping_handle = mapping_vault.create(
                app_id=app_id,
                scope="document",
                entries=redactor.mapping_entries(),
                expected_counts=dict(Counter(PLACEHOLDER_RE.findall(serialized_masked))),
            )
            detector_headers["x-tuomin-mapping-handle"] = mapping_handle

        if redactor.blocked_labels and _should_block(profile):
            _record_proxy_call(
                app_id, started,
                char_count=len(raw_body), redactor=redactor, blocked=True,
            )
            return JSONResponse(
                {"error": {"message": "tuomin fail-closed: blocked labels present",
                           "blocked_labels": sorted(redactor.blocked_labels)}},
                status_code=409,
            )

        _write_outbound_snapshot(masked)  # opt-in (TUOMIN_PROXY_SNAPSHOT_DIR), masked body only

        stream = bool(masked.get("stream"))
        body = json.dumps(masked, ensure_ascii=False).encode("utf-8")
        headers = _upstream_headers(request)

        try:
            result = await forward(upstream, headers, body, stream=stream)
        except Exception as exc:
            return JSONResponse({"error": {"message": f"tuomin upstream forward failed: {exc}"}}, status_code=502)

        _record_proxy_call(app_id, started, char_count=len(body), redactor=redactor)

        stats = {"restored": 0, "unknown": set()}

        def _on_stream_guard_events(events) -> None:
            _audit_guard_events(audit_log, events, channel="proxy_stream", app_id=app_id)

        if stream and result.aiter is not None:
            out_headers = _passthrough_response_headers(result.headers)
            out_headers["x-tuomin-streaming"] = "true"
            out_headers.update(detector_headers)
            out_headers.update(guard_in.headers())  # output guard runs per-segment in the generator
            auto_refill = effective_auto_refill
            if not auto_refill:
                out_headers["x-tuomin-response-mode"] = "masked"
                return StreamingResponse(
                    _guarded_passthrough_stream(
                        result.aiter, profile, on_guard_events=_on_stream_guard_events
                    ),
                    status_code=result.status,
                    headers=out_headers,
                    media_type="text/event-stream",
                )
            out_headers["x-tuomin-response-mode"] = response_mode
            if profile.refill_strict or proxy_session is not None:
                generator = _strict_refill_event_stream(
                    result.aiter, redactor, stats, profile,
                    on_guard_events=_on_stream_guard_events,
                    allow_missing=proxy_session is not None,
                    include_reasoning=not path_reasoning_masked,
                )
            else:
                generator = _refill_event_stream(
                    result.aiter, redactor, stats, profile,
                    document_validated=proxy_session is not None,
                    on_guard_events=_on_stream_guard_events,
                    include_reasoning=not path_reasoning_masked,
                )
            return StreamingResponse(generator, status_code=result.status, headers=out_headers,
                                     media_type="text/event-stream")

        # non-streaming
        raw = result.body or b""
        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            # not JSON we understand — return as-is rather than risk corrupting it
            return JSONResponse({"error": {"message": "tuomin: non-JSON upstream response"}},
                                status_code=502)
        auto_refill = effective_auto_refill
        if not auto_refill:
            found = set(PLACEHOLDER_RE.findall(raw.decode("utf-8", "replace")))
            unknown = found - set(redactor.mapping)
            guard_out = guard.scan_output(
                raw.decode("utf-8", "replace"),
                profile,
                unknown_placeholders=unknown,
            )
            _audit_guard_events(audit_log, guard_out.events, channel="proxy_output", app_id=app_id)
            combined = guard.GuardOutcome(guard_in.events + guard_out.events)
            if guard_out.blocking:
                return JSONResponse(
                    {
                        "error": {"message": "tuomin guard blocked response"},
                        "alerts": [event.to_safe_dict() for event in guard_out.blocking],
                    },
                    status_code=502,
                    headers=combined.headers(),
                )
            out_headers = {
                **detector_headers,
                "x-tuomin-response-mode": "masked",
            }
            out_headers.update(combined.headers())
            return JSONResponse(parsed, status_code=result.status, headers=out_headers)
        try:
            refilled = refill_json(
                parsed,
                redactor,
                stats,
                allow_missing=proxy_session is not None,
                skip_keys=(
                    frozenset({"reasoning_content", "thinking"})
                    if path_reasoning_masked
                    else None
                ),
            )
        except PlaceholderIntegrityError as exc:
            return JSONResponse(
                {"error": {"message": f"tuomin fail-closed: {exc}"}},
                status_code=502,
            )

        # --- output guard: scan RAW response (pre-refill, masked-space) for
        # model-emitted secrets + hallucinated placeholders the model invented ---
        guard_out = guard.scan_output(
            raw.decode("utf-8", "replace"), profile, unknown_placeholders=stats["unknown"]
        )
        _audit_guard_events(audit_log, guard_out.events, channel="proxy_output", app_id=app_id)
        combined = guard.GuardOutcome(guard_in.events + guard_out.events)
        if guard_out.blocking:
            return JSONResponse(
                {"error": {"message": "tuomin guard blocked response"},
                 "alerts": [e.to_safe_dict() for e in guard_out.blocking]},
                status_code=502, headers=combined.headers(),
            )
        out_headers = {
            "x-tuomin-refill-restored-count": str(stats["restored"]),
            "x-tuomin-refill-unknown-count": str(len(stats["unknown"])),
        }
        out_headers.update(detector_headers)
        out_headers["x-tuomin-response-mode"] = response_mode
        if proxy_session is None and path_app_id is None:
            out_headers["deprecation"] = "true"
        out_headers.update(combined.headers())
        return JSONResponse(refilled, status_code=result.status, headers=out_headers)

    @app.post("/v1/messages")
    async def anthropic_messages(request: Request) -> Any:
        return await handle(request, "TUOMIN_UPSTREAM_ANTHROPIC")

    @app.post("/v1/chat/completions")
    async def openai_chat(request: Request) -> Any:
        return await handle(request, "TUOMIN_UPSTREAM_OPENAI")

    # Per-app project endpoints: the URL path pins the app (for agents like
    # WorkBuddy that can only configure a base URL + API key). The plain form
    # stays masked; the ``auto`` form transparently refills only when the app
    # registry entry declares ``allow_auto_refill: true``.
    @app.post("/apps/{app_id}/v1/messages")
    async def app_anthropic_messages(app_id: str, request: Request) -> Any:
        return await handle(
            request, "TUOMIN_UPSTREAM_ANTHROPIC", path_app_id=app_id
        )

    @app.post("/apps/{app_id}/auto/v1/messages")
    async def app_anthropic_messages_auto(app_id: str, request: Request) -> Any:
        return await handle(
            request, "TUOMIN_UPSTREAM_ANTHROPIC",
            path_app_id=app_id, path_auto_refill=True,
        )

    @app.post("/apps/{app_id}/v1/chat/completions")
    async def app_openai_chat(app_id: str, request: Request) -> Any:
        return await handle(
            request, "TUOMIN_UPSTREAM_OPENAI", path_app_id=app_id
        )

    @app.post("/apps/{app_id}/auto/v1/chat/completions")
    async def app_openai_chat_auto(app_id: str, request: Request) -> Any:
        return await handle(
            request, "TUOMIN_UPSTREAM_OPENAI",
            path_app_id=app_id, path_auto_refill=True,
        )

    # Demo mode: content/tool-calls refill transparently while chain-of-thought
    # reaches the client still masked — visible redaction evidence for
    # presentations. Same registry gate (allow_auto_refill) as the auto form.
    @app.post("/apps/{app_id}/demo/v1/messages")
    async def app_anthropic_messages_demo(app_id: str, request: Request) -> Any:
        return await handle(
            request, "TUOMIN_UPSTREAM_ANTHROPIC",
            path_app_id=app_id, path_reasoning_masked=True,
        )

    @app.post("/apps/{app_id}/demo/v1/chat/completions")
    async def app_openai_chat_demo(app_id: str, request: Request) -> Any:
        return await handle(
            request, "TUOMIN_UPSTREAM_OPENAI",
            path_app_id=app_id, path_reasoning_masked=True,
        )

    @app.post("/api/v1/proxy-sessions")
    def create_proxy_session(
        request: Request, payload: dict = Body(default={})
    ) -> Any:
        app_id = payload.get("app_id")
        if not isinstance(app_id, str) or not app_id:
            return JSONResponse(
                {"error": {"code": "app_id_required", "message": "app_id is required"}},
                status_code=400,
            )
        try:
            registry.authorize(
                app_id,
                "proxy_session",
                request.headers.get("x-tuomin-capability-token"),
            )
            route = payload.get("provider_route")
            if route not in registry.allowed_proxy_routes(app_id):
                return JSONResponse(
                    {"error": {"code": "proxy_route_denied", "message": "proxy route denied"}},
                    status_code=403,
                )
            upstream_id = payload.get("upstream_id")
            targets = registry.proxy_targets(app_id)
            if upstream_id is not None:
                if (
                    not isinstance(upstream_id, str)
                    or re.fullmatch(r"[A-Za-z0-9._:-]{1,100}", upstream_id) is None
                ):
                    return JSONResponse(
                        {"error": {"code": "proxy_target_invalid", "message": "proxy target invalid"}},
                        status_code=400,
                    )
                upstream = registry.resolve_proxy_target(
                    app_id, target_id=upstream_id, route=route
                )
            elif targets:
                return JSONResponse(
                    {"error": {"code": "proxy_target_required", "message": "proxy target required"}},
                    status_code=400,
                )
            else:
                upstream = _configured_proxy_upstream(registry, app_id, route)
                upstream_id = ""
                if not upstream:
                    return JSONResponse(
                        {
                            "error": {
                                "code": "proxy_upstream_unavailable",
                                "message": "proxy upstream unavailable",
                            }
                        },
                        status_code=503,
                    )
            project_id = payload.get("project_id")
            run_id = payload.get("run_id")
            thread_id = payload.get("thread_id")
            identity = re.compile(r"[A-Za-z0-9._:-]{1,200}")
            if any(
                not isinstance(value, str) or identity.fullmatch(value) is None
                for value in (project_id, run_id, thread_id)
            ):
                return JSONResponse(
                    {"error": {"code": "proxy_session_invalid", "message": "session identity invalid"}},
                    status_code=400,
                )
            profile = registry.resolve_profile(app_id)
            dictionary = registry.resolve_dictionary_state(app_id)
            detectors = build_detectors(
                dictionary.entries,
                dictionary_version=dictionary.version,
            )
        except CapabilityDenied:
            return JSONResponse(
                {"error": {"code": "capability_denied", "message": "capability denied"}},
                status_code=403,
            )
        except RegistryConfigurationError as exc:
            return JSONResponse(
                {"error": {"code": exc.code, "message": str(exc)}},
                status_code=503,
            )
        session_id = f"ps_{secrets.token_urlsafe(24)}"
        token = secrets.token_urlsafe(32)
        expires_at = time.time() + proxy_session_ttl
        session = {
            "proxy_session_id": session_id,
            "token": token,
            "app_id": app_id,
            "project_id": project_id,
            "run_id": run_id,
            "thread_id": thread_id,
            "provider_route": route,
            "upstream_id": upstream_id,
            "upstream": upstream,
            "profile": profile,
            "detectors": detectors,
            "redactor": SessionRedactor(
                detectors, profile, identity_mode="surface"
            ),
            "coverage_items": [],
            "inspection_lock": threading.RLock(),
            "expires_at": expires_at,
        }
        inspection_summary: dict[str, Any] = {
            "inspection_id": "",
            "terminal_status": "active",
            "entry_count": 0,
            "generalization_count": 0,
            "coverage_finding_count": 0,
            "coverage_complete": False,
        }
        if inspection_vault is not None:
            try:
                inspection_summary = inspection_vault.create(
                    app_id=app_id,
                    project_id=project_id,
                    run_id=run_id,
                    scope="followup",
                    terminal_status="active",
                    entries=[],
                    trace=[],
                    coverage_items=[],
                    detectors=detectors,
                    profile=profile,
                    coverage_complete=False,
                )
            except Exception:
                return JSONResponse(
                    {
                        "error": {
                            "code": "inspection_snapshot_failed",
                            "message": "inspection snapshot failed",
                        }
                    },
                    status_code=502,
                )
        session["inspection_id"] = inspection_summary["inspection_id"]
        with proxy_sessions_lock:
            proxy_sessions[session_id] = session
        base = str(request.base_url).rstrip("/")
        return {
            "status": "ok",
            "proxy_session_id": session_id,
            "proxy_session_token": token,
            "base_url": f"{base}/proxy-sessions/{session_id}/v1",
            "expires_at": int(expires_at),
            "response_mode": "trusted-local-transparent",
            "upstream_id": upstream_id,
            **inspection_summary,
        }

    @app.get("/api/v1/proxy-targets")
    def proxy_target_status(request: Request, app_id: str) -> Any:
        try:
            registry.authorize(
                app_id,
                "proxy_session",
                request.headers.get("x-tuomin-capability-token"),
            )
            targets = registry.proxy_targets(app_id)
        except CapabilityDenied:
            return JSONResponse(
                {"error": {"code": "capability_denied", "message": "capability denied"}},
                status_code=403,
            )
        except RegistryConfigurationError as exc:
            return JSONResponse(
                {"error": {"code": exc.code, "message": str(exc)}},
                status_code=503,
            )
        return {
            "status": "ok",
            "targets": [
                {"upstream_id": target_id, "provider_route": spec["route"]}
                for target_id, spec in sorted(targets.items())
            ],
        }

    @app.get("/api/v1/proxy-sessions/{session_id}")
    def proxy_session_status(session_id: str, request: Request) -> Any:
        session = _authorize_proxy_session(session_id, request)
        if session is None:
            return JSONResponse(
                {"error": {"code": "proxy_session_unavailable", "message": "proxy session unavailable"}},
                status_code=404,
            )
        return {
            "status": "ok",
            "proxy_session_id": session_id,
            "provider_route": session["provider_route"],
            "upstream_id": session["upstream_id"],
            "expires_at": int(session["expires_at"]),
            "request_count": len(session["coverage_items"]),
            "inspection_id": session["inspection_id"],
        }

    @app.post("/api/v1/proxy-sessions/{session_id}/close")
    def close_proxy_session(session_id: str, request: Request) -> Any:
        session = _authorize_proxy_session(session_id, request)
        if session is None:
            return JSONResponse(
                {"error": {"code": "proxy_session_unavailable", "message": "proxy session unavailable"}},
                status_code=404,
            )
        try:
            snapshot = _close_proxy_session(session)
        except Exception:
            with proxy_sessions_lock:
                proxy_sessions.pop(session_id, None)
            return JSONResponse(
                {"error": {"code": "inspection_snapshot_failed", "message": "inspection snapshot failed"}},
                status_code=502,
            )
        with proxy_sessions_lock:
            proxy_sessions.pop(session_id, None)
        return {"status": "ok", "closed": True, **snapshot}

    @app.post("/proxy-sessions/{session_id}/v1/messages")
    async def proxy_session_anthropic(session_id: str, request: Request) -> Any:
        session = _authorize_proxy_session(
            session_id, request, protocol="anthropic"
        )
        if session is None:
            return JSONResponse(
                {"error": {"code": "proxy_session_unavailable", "message": "proxy session unavailable"}},
                status_code=404,
            )
        return await handle(
            request, "TUOMIN_UPSTREAM_ANTHROPIC", proxy_session=session
        )

    @app.post("/proxy-sessions/{session_id}/v1/chat/completions")
    async def proxy_session_openai(session_id: str, request: Request) -> Any:
        session = _authorize_proxy_session(
            session_id, request, protocol="openai"
        )
        if session is None:
            return JSONResponse(
                {"error": {"code": "proxy_session_unavailable", "message": "proxy session unavailable"}},
                status_code=404,
            )
        return await handle(
            request, "TUOMIN_UPSTREAM_OPENAI", proxy_session=session
        )


_SNAPSHOT_COUNTER = 0


def _write_outbound_snapshot(masked: dict) -> None:
    """Opt-in: write the MASKED request body (what would go to the provider) to a
    local file for leak verification. Off unless TUOMIN_PROXY_SNAPSHOT_DIR is set.
    Contains no auth headers and no raw values (the body is already masked)."""
    snap_dir = os.environ.get("TUOMIN_PROXY_SNAPSHOT_DIR")
    if not snap_dir:
        return
    global _SNAPSHOT_COUNTER
    _SNAPSHOT_COUNTER += 1
    os.makedirs(snap_dir, exist_ok=True)
    name = f"outbound-{os.getpid()}-{_SNAPSHOT_COUNTER:04d}.json"
    payload = {
        "direction": "outbound_to_provider",
        "note": "Masked request body before the upstream call. Local-only; no API key, no raw values.",
        "payload": masked,
    }
    with open(os.path.join(snap_dir, name), "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)


def _should_block(profile) -> bool:
    return getattr(profile, "block_min_severity", None) is not None


def _passthrough_response_headers(headers: dict[str, str]) -> dict[str, str]:
    drop = {"content-length", "content-encoding", "transfer-encoding", "connection"}
    return {k: v for k, v in headers.items() if k.lower() not in drop}


async def _httpx_forward(url: str, headers: dict[str, str], body: bytes, *, stream: bool) -> ForwardResult:
    """Default forwarder using httpx (lazy import; only needed when proxying).

    ``trust_env=False`` is a correctness/security pin, not a preference: a
    localhost privacy gateway must not route upstream traffic through ambient
    system/env proxies (macOS system proxy, HTTP(S)_PROXY). A local VPN-style
    proxy cannot forward loopback upstreams and hangs the request; honoring
    ambient proxies would also silently reroute provider traffic.
    """
    import httpx

    timeout = httpx.Timeout(float(os.environ.get("TUOMIN_PROXY_TIMEOUT", "180")))
    if not stream:
        async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
            resp = await client.post(url, headers=headers, content=body)
            return ForwardResult(resp.status_code, dict(resp.headers), body=resp.content)

    # Open the stream and read the REAL status/headers (available as soon as the
    # response head arrives) before constructing the result, so an upstream
    # 401/429/500 is surfaced to the client as itself, not a fake 200.
    client = httpx.AsyncClient(timeout=timeout, trust_env=False)
    stream_cm = client.stream("POST", url, headers=headers, content=body)
    resp = await stream_cm.__aenter__()

    async def _aiter() -> AsyncIterator[bytes]:
        try:
            async for chunk in resp.aiter_bytes():
                yield chunk
        finally:
            await stream_cm.__aexit__(None, None, None)
            await client.aclose()

    return ForwardResult(resp.status_code, dict(resp.headers), aiter=_aiter())
