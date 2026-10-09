"use strict";

const fs = require("node:fs");
const path = require("node:path");

const DEFAULT_BASE_URL = "http://127.0.0.1:8765";
const DEFAULT_TIMEOUT_MS = 5000;

module.exports = class TuominTransformer {
  constructor(options = {}) {
    this.name = "tuomin";
    this.baseUrl = stripTrailingSlash(
      options.baseUrl || process.env.TUOMIN_BASE_URL || DEFAULT_BASE_URL
    );
    this.appId = options.appId || process.env.TUOMIN_APP_ID || "agent";
    this.profile = options.profile || process.env.TUOMIN_PROFILE || undefined;
    this.includeSystem = Boolean(options.includeSystem);
    this.failOnBlocked = options.failOnBlocked !== false;
    this.failOnUnknownPlaceholders = options.failOnUnknownPlaceholders !== false;
    this.requestTimeoutMs = Number(options.requestTimeoutMs || process.env.TUOMIN_TIMEOUT_MS || DEFAULT_TIMEOUT_MS);
    this.sessionId = options.sessionId || null;
    this.outboundSnapshotDir = resolveOutboundSnapshotDir(options);
    this._snapshotCounter = 0;
    this._openingSession = null;
    this.logger = null;
  }

  async transformRequestIn(request) {
    if (!request || !Array.isArray(request.messages)) {
      return request;
    }
    const messages = [];
    for (const message of request.messages) {
      const shouldMask = this._shouldMaskMessage(message);
      const nextMessage = {
        ...message,
        content: await this._maskContent(message.content, shouldMask),
      };
      if (Array.isArray(message.tool_calls) && shouldMask) {
        nextMessage.tool_calls = await this._maskToolCalls(message.tool_calls);
      }
      messages.push(nextMessage);
    }
    const transformedRequest = { ...request, messages };
    // Anthropic top-level `system` lives outside messages; apply the same
    // policy as system-role messages (masked only with includeSystem).
    if (transformedRequest.system != null && this._shouldMaskMessage({ role: "system" })) {
      transformedRequest.system = await this._maskContent(transformedRequest.system, true);
    }
    if (Array.isArray(transformedRequest.tools)) {
      transformedRequest.tools = await this._maskToolDefinitions(transformedRequest.tools);
    }
    this._writeOutboundSnapshot(transformedRequest);
    return transformedRequest;
  }

  async transformResponseOut(response) {
    if (!response || !this.sessionId) {
      return response;
    }
    const contentType = response.headers?.get?.("content-type") || "";
    if (contentType.includes("text/event-stream") || contentType.includes("stream")) {
      return this._transformEventStream(response);
    }
    if (!contentType.includes("application/json")) {
      return response;
    }

    const raw = await response.text();
    let payload;
    try {
      payload = JSON.parse(raw);
    } catch (error) {
      return cloneResponse(response, raw);
    }

    const stats = { restoredCount: 0, unknownCount: 0 };
    const transformed = await this._refillJson(payload, stats);
    const headers = new Headers(response.headers);
    headers.delete("content-length");
    headers.set("x-tuomin-refill-restored-count", String(stats.restoredCount));
    headers.set("x-tuomin-refill-unknown-count", String(stats.unknownCount));

    return new Response(JSON.stringify(transformed), {
      status: response.status,
      statusText: response.statusText,
      headers,
    });
  }

  _shouldMaskMessage(message) {
    if (!message) {
      return false;
    }
    if (message.role === "system" && !this.includeSystem) {
      return false;
    }
    return true;
  }

  async _maskContent(content, shouldMask) {
    if (!shouldMask || content == null) {
      return content;
    }
    if (typeof content === "string") {
      return this._maskText(content);
    }
    if (Array.isArray(content)) {
      const blocks = [];
      for (const block of content) {
        blocks.push(await this._maskContentBlock(block));
      }
      return blocks;
    }
    return content;
  }

  async _maskContentBlock(block) {
    if (!block || typeof block !== "object") {
      return block;
    }
    if (block.type === "text" && typeof block.text === "string") {
      return { ...block, text: await this._maskText(block.text) };
    }
    if (block.type === "tool_result" && typeof block.content === "string") {
      return { ...block, content: await this._maskText(block.content) };
    }
    if (block.type === "tool_result" && Array.isArray(block.content)) {
      const nested = [];
      for (const item of block.content) {
        nested.push(await this._maskContentBlock(item));
      }
      return { ...block, content: nested };
    }
    if (block.type === "tool_use" && block.input && typeof block.input === "object") {
      // Anthropic tool_use: mask every string leaf of `input` while keeping the
      // JSON shape (and `name`/`id`) intact so the provider call still matches.
      return { ...block, input: await this._maskStringLeaves(block.input) };
    }
    return block;
  }

  async _maskStringLeaves(value) {
    if (typeof value === "string") {
      return this._maskText(value);
    }
    if (Array.isArray(value)) {
      const items = [];
      for (const item of value) {
        items.push(await this._maskStringLeaves(item));
      }
      return items;
    }
    if (value && typeof value === "object") {
      const next = {};
      for (const [key, child] of Object.entries(value)) {
        next[key] = await this._maskStringLeaves(child);
      }
      return next;
    }
    return value;
  }

  // Tool definitions: only free-form `description` leaves are masked. `name`,
  // `type`, schema property keys, `enum`/`required` values stay verbatim —
  // the provider and the model match them exactly, so masking them would
  // break tool invocation, while descriptions are prose that can carry PII.
  async _maskToolDefinitions(tools) {
    const masked = [];
    for (const tool of tools) {
      masked.push(await this._maskDescriptions(tool));
    }
    return masked;
  }

  async _maskDescriptions(value) {
    if (Array.isArray(value)) {
      const items = [];
      for (const item of value) {
        items.push(await this._maskDescriptions(item));
      }
      return items;
    }
    if (value && typeof value === "object") {
      const next = {};
      for (const [key, child] of Object.entries(value)) {
        if (key === "description" && typeof child === "string") {
          next[key] = await this._maskText(child);
        } else {
          next[key] = await this._maskDescriptions(child);
        }
      }
      return next;
    }
    return value;
  }

  async _maskToolCalls(toolCalls) {
    const maskedCalls = [];
    for (const call of toolCalls) {
      if (!call || typeof call !== "object") {
        maskedCalls.push(call);
        continue;
      }
      const nextCall = { ...call };
      if (call.function && typeof call.function === "object") {
        nextCall.function = { ...call.function };
        if (typeof call.function.arguments === "string") {
          nextCall.function.arguments = await this._maskText(call.function.arguments);
        } else if (call.function.arguments && typeof call.function.arguments === "object") {
          // Some routers hand us already-parsed arguments; mask string leaves.
          nextCall.function.arguments = await this._maskStringLeaves(call.function.arguments);
        }
      }
      maskedCalls.push(nextCall);
    }
    return maskedCalls;
  }

  async _maskText(text) {
    if (!text) {
      return text;
    }
    const sessionId = await this._ensureSession();
    try {
      return await this._maskOnce(sessionId, text);
    } catch (error) {
      if (error && error.tuominStatus === 404) {
        // Session evaporated (TTL / close / gateway restart): reopen once and
        // retry once. Masking under the new session is safe — it mints fresh
        // placeholders for new text. A second 404 means the gateway itself is
        // broken and stays fail-closed.
        const freshId = await this._reopenSession(sessionId);
        return this._maskOnce(freshId, text);
      }
      throw error;
    }
  }

  async _maskOnce(sessionId, text) {
    const payload = await this._postJson(`/session/${encodeURIComponent(sessionId)}/mask`, { text });
    const blockedLabels = Array.isArray(payload.blocked_labels) ? payload.blocked_labels : [];
    if (this.failOnBlocked && blockedLabels.length > 0) {
      throw this._failClosed(`blocked labels: ${blockedLabels.join(", ")}`);
    }
    if (typeof payload.masked !== "string") {
      throw this._failClosed("mask response missing masked text");
    }
    return payload.masked;
  }

  async _refillJson(value, stats) {
    if (Array.isArray(value)) {
      const items = [];
      for (const item of value) {
        items.push(await this._refillJson(item, stats));
      }
      return items;
    }
    if (!value || typeof value !== "object") {
      return value;
    }
    const next = { ...value };
    for (const [key, child] of Object.entries(value)) {
      if ((key === "content" || key === "text") && typeof child === "string") {
        next[key] = await this._refillText(child, stats);
      } else {
        next[key] = await this._refillJson(child, stats);
      }
    }
    return next;
  }

  async _refillText(text, stats) {
    if (!text || !this.sessionId) {
      return text;
    }
    const sessionId = this.sessionId;
    let payload;
    try {
      payload = await this._refillOnce(sessionId, text);
    } catch (error) {
      if (error && error.tuominStatus === 404) {
        // Session gone: reopen once and retry once. Placeholders minted by the
        // DEAD session are unknown to the fresh one, so the integrity checks
        // below then block (fail-closed) — correct, because the old mapping is
        // unrecoverable. Placeholder-free text refills fine and the response
        // survives a gateway restart.
        const freshId = await this._reopenSession(sessionId);
        payload = await this._refillOnce(freshId, text);
      } else {
        throw error;
      }
    }
    stats.restoredCount += Number(payload.restored_count || 0);
    const unknownPlaceholders = arrayField(payload.unknown_placeholders);
    const alteredPlaceholders = arrayField(payload.altered_placeholders);
    const missingPlaceholders = arrayField(payload.missing_placeholders);
    stats.unknownCount += unknownPlaceholders.length;
    if (this.failOnUnknownPlaceholders && unknownPlaceholders.length > 0) {
      throw this._failClosed(`unknown placeholders: ${unknownPlaceholders.join(", ")}`);
    }
    if (this.failOnUnknownPlaceholders && alteredPlaceholders.length > 0) {
      throw this._failClosed(`altered placeholders: ${alteredPlaceholders.join(", ")}`);
    }
    if (this.failOnUnknownPlaceholders && missingPlaceholders.length > 0) {
      throw this._failClosed(`missing placeholders: ${missingPlaceholders.join(", ")}`);
    }
    if (payload.status === "blocked") {
      const errorTypes = arrayField(payload.error_types).join(", ") || "placeholder integrity failure";
      throw this._failClosed(`refill blocked: ${errorTypes}`);
    }
    // Success whitelist: the session refill contract defines exactly "ok" and
    // "blocked" (see SessionRedactor.refill in src/tuomin_gateway/session.py).
    // A missing or unknown status can no longer fall through to the raw text.
    if (payload.status !== "ok") {
      throw this._failClosed(`refill returned unexpected status: ${String(payload.status)}`);
    }
    if (typeof payload.text !== "string") {
      throw this._failClosed("refill response missing text");
    }
    return payload.text;
  }

  async _refillOnce(sessionId, text) {
    return this._postJson(`/session/${encodeURIComponent(sessionId)}/refill`, { text });
  }

  _transformEventStream(response) {
    if (!response.body) {
      return response;
    }
    const decoder = new TextDecoder();
    const encoder = new TextEncoder();
    const stats = { restoredCount: 0, unknownCount: 0 };
    const headers = new Headers(response.headers);
    headers.delete("content-length");
    headers.set("x-tuomin-streaming", "true");

    const transformedStream = new ReadableStream({
      start: async (controller) => {
        const reader = response.body.getReader();
        let eventBuffer = "";
        let textBuffer = "";
        let lastTextChunk = null;
        let lastTextPath = null;
        let lastPrefixLines = [];

        const enqueue = (text) => {
          controller.enqueue(encoder.encode(text));
        };

        const emitEvent = (prefixLines, data) => {
          const lines = [...prefixLines];
          if (data !== null) {
            lines.push(`data: ${data}`);
          }
          enqueue(`${lines.join("\n")}\n\n`);
        };

        const flushPendingText = async () => {
          if (!textBuffer) {
            return;
          }
          const chunk = lastTextChunk ? cloneJson(lastTextChunk) : defaultStreamingChunk();
          const path = lastTextPath || ["choices", 0, "delta", "content"];
          setAtPath(chunk, path, await this._refillText(textBuffer, stats));
          textBuffer = "";
          emitEvent(lastPrefixLines, JSON.stringify(chunk));
        };

        const processEvent = async (event) => {
          if (!event) {
            return;
          }
          const prefixLines = [];
          const dataParts = [];
          for (const line of event.split("\n")) {
            if (line.startsWith("data:")) {
              // SSE: one optional space after "data:" is not part of the value.
              dataParts.push(line.slice(5).replace(/^ /, ""));
            } else {
              prefixLines.push(line);
            }
          }
          if (dataParts.length === 0) {
            enqueue(`${event}\n\n`);
            return;
          }
          // SSE: multi-line data is joined with "\n" before interpretation.
          const data = dataParts.join("\n");
          if (data.trim() === "[DONE]") {
            await flushPendingText();
            emitEvent(prefixLines, "[DONE]");
            return;
          }

          let chunk;
          try {
            chunk = JSON.parse(data);
          } catch (error) {
            enqueue(`${event}\n\n`);
            return;
          }

          const target = firstStreamingTextTarget(chunk);
          if (!target) {
            emitEvent(prefixLines, JSON.stringify(chunk));
            return;
          }

          textBuffer += getAtPath(chunk, target.path);
          lastTextChunk = chunk;
          lastTextPath = target.path;
          lastPrefixLines = prefixLines;

          const split = splitSafeText(textBuffer);
          textBuffer = split.rest;
          if (!split.safe) {
            return;
          }

          setAtPath(chunk, target.path, await this._refillText(split.safe, stats));
          emitEvent(prefixLines, JSON.stringify(chunk));
        };

        try {
          while (true) {
            const { done, value } = await reader.read();
            if (done) {
              break;
            }
            // Normalize CRLF before splitting so LF, CRLF and mixed streams —
            // including a CRLF pair split across chunks — behave identically
            // (mirrors src/tuomin_gateway/service/proxy.py).
            eventBuffer = (eventBuffer + decoder.decode(value, { stream: true })).replace(/\r\n/g, "\n");
            let index = eventBuffer.indexOf("\n\n");
            while (index !== -1) {
              const event = eventBuffer.slice(0, index);
              eventBuffer = eventBuffer.slice(index + 2);
              await processEvent(event);
              index = eventBuffer.indexOf("\n\n");
            }
          }
          eventBuffer = (eventBuffer + decoder.decode()).replace(/\r\n/g, "\n");
          if (eventBuffer.trim()) {
            await processEvent(eventBuffer.trimEnd());
          }
          await flushPendingText();
        } catch (error) {
          controller.error(error);
          return;
        } finally {
          reader.releaseLock();
        }
        controller.close();
      },
    });

    return new Response(transformedStream, {
      status: response.status,
      statusText: response.statusText,
      headers,
    });
  }

  _writeOutboundSnapshot(payload) {
    if (!this.outboundSnapshotDir) {
      return;
    }
    try {
      fs.mkdirSync(this.outboundSnapshotDir, { recursive: true });
      this._snapshotCounter += 1;
      const stamp = new Date().toISOString().replace(/[:.]/g, "-");
      const name = `outbound-to-provider-${stamp}-${process.pid}-${this._snapshotCounter}.json`;
      const snapshot = {
        direction: "outbound_to_provider",
        created_at: new Date().toISOString(),
        note: "Local-only snapshot of the request body after tuomin masking, before the upstream provider call.",
        session_id: this.sessionId,
        payload,
      };
      fs.writeFileSync(path.join(this.outboundSnapshotDir, name), `${JSON.stringify(snapshot, null, 2)}\n`, "utf8");
    } catch (error) {
      throw this._failClosed(`outbound snapshot write failed: ${error.message}`);
    }
  }

  async _ensureSession() {
    if (this.sessionId) {
      return this.sessionId;
    }
    if (!this._openingSession) {
      this._openingSession = this._openSession().finally(() => {
        this._openingSession = null;
      });
    }
    return this._openingSession;
  }

  async _reopenSession(staleId) {
    // The gateway forgot our session (TTL eviction, explicit close, or a
    // gateway restart — sessions live in gateway memory only). Open a fresh
    // one instead of failing every later request forever. If a concurrent
    // request already reopened (sessionId moved on from the stale id), reuse
    // that session rather than opening yet another one.
    if (this.sessionId && this.sessionId !== staleId) {
      return this.sessionId;
    }
    this.sessionId = null;
    return this._ensureSession();
  }

  async _openSession() {
    const payload = { app_id: this.appId };
    if (this.profile) {
      payload.profile = this.profile;
    }
    const result = await this._postJson("/session/open", payload);
    if (!result.session_id) {
      throw this._failClosed("tuomin session_open response missing session_id");
    }
    this.sessionId = result.session_id;
    return this.sessionId;
  }

  async _postJson(path, payload) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), this.requestTimeoutMs);
    try {
      const response = await fetch(`${this.baseUrl}${path}`, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify(payload),
        signal: controller.signal,
      });
      const text = await response.text();
      let data = {};
      if (text) {
        try {
          data = JSON.parse(text);
        } catch (error) {
          throw this._failClosed(`invalid JSON from tuomin ${path}`);
        }
      }
      if (!response.ok) {
        const error = this._failClosed(`tuomin ${path} returned HTTP ${response.status}`);
        // Keep the HTTP status machine-readable so session endpoints can
        // distinguish "session gone" (404 -> reopen + retry once) from real
        // gateway failures (everything else -> fail closed immediately).
        error.tuominStatus = response.status;
        throw error;
      }
      return data;
    } catch (error) {
      if (isFailClosedError(error)) {
        throw error;
      }
      throw this._failClosed(`tuomin request failed for ${path}: ${error.message}`);
    } finally {
      clearTimeout(timer);
    }
  }

  _failClosed(message) {
    const error = new Error(`tuomin fail-closed: ${message}`);
    error.code = "TUOMIN_FAIL_CLOSED";
    return error;
  }
};

function arrayField(value) {
  return Array.isArray(value) ? value : [];
}

function resolveOutboundSnapshotDir(options) {
  const explicitDir = options.outboundSnapshotDir || process.env.TUOMIN_OUTBOUND_SNAPSHOT_DIR;
  if (explicitDir) {
    return path.resolve(String(explicitDir));
  }
  const enabled = options.outboundSnapshot || process.env.TUOMIN_OUTBOUND_SNAPSHOT;
  if (enabled === true || enabled === "1" || enabled === "true") {
    return path.resolve("out-snapshots");
  }
  return null;
}

function stripTrailingSlash(value) {
  return String(value || DEFAULT_BASE_URL).replace(/\/+$/, "");
}

function isFailClosedError(error) {
  return error && error.code === "TUOMIN_FAIL_CLOSED";
}

function cloneResponse(response, body) {
  return new Response(body, {
    status: response.status,
    statusText: response.statusText,
    headers: response.headers,
  });
}

function cloneJson(value) {
  return JSON.parse(JSON.stringify(value));
}

function defaultStreamingChunk() {
  return { choices: [{ delta: { content: "" } }] };
}

function firstStreamingTextTarget(chunk) {
  if (Array.isArray(chunk?.choices)) {
    for (let index = 0; index < chunk.choices.length; index += 1) {
      const choice = chunk.choices[index];
      if (typeof choice?.delta?.content === "string") {
        return { path: ["choices", index, "delta", "content"] };
      }
      if (typeof choice?.delta?.text === "string") {
        return { path: ["choices", index, "delta", "text"] };
      }
      if (typeof choice?.message?.content === "string") {
        return { path: ["choices", index, "message", "content"] };
      }
      if (typeof choice?.text === "string") {
        return { path: ["choices", index, "text"] };
      }
    }
  }
  if (typeof chunk?.delta?.text === "string") {
    return { path: ["delta", "text"] };
  }
  if (typeof chunk?.content === "string") {
    return { path: ["content"] };
  }
  if (typeof chunk?.text === "string") {
    return { path: ["text"] };
  }
  return null;
}

function getAtPath(value, path) {
  let current = value;
  for (const segment of path) {
    current = current[segment];
  }
  return current;
}

function setAtPath(value, path, nextValue) {
  let current = value;
  for (let index = 0; index < path.length - 1; index += 1) {
    current = current[path[index]];
  }
  current[path[path.length - 1]] = nextValue;
}

// Longest tail held back while waiting for a possible placeholder to complete.
// Real placeholders are a few dozen chars; past this limit the tail is released
// so an unclosed run can neither stall the stream nor grow the buffer without
// bound — refill then judges the fragment (unknown/altered → fail-closed).
const MAX_PENDING_PLACEHOLDER_CHARS = 64;

// Unclosed "<...": chars that may still grow into a placeholder of the gateway
// grammar, including the loose altered forms (optional whitespace after "<",
// lowercase letters, "-"). A "< 5" tail can never match and is released at once.
const UNCLOSED_ANGLE_TAIL_RE = /^<\s*([A-Za-z][A-Za-z0-9_\s-]*)?$/;
// Bare placeholder tail (no angle brackets): an uppercase-led word that may
// still grow into LABEL_001 across chunk boundaries.
const BARE_PLACEHOLDER_TAIL_RE = /[A-Z][A-Z0-9_-]*$/;

function splitSafeText(text) {
  const lastOpen = text.lastIndexOf("<");
  if (lastOpen !== -1) {
    const suffix = text.slice(lastOpen);
    if (!suffix.includes(">") && UNCLOSED_ANGLE_TAIL_RE.test(suffix)) {
      if (suffix.length > MAX_PENDING_PLACEHOLDER_CHARS) {
        return { safe: text, rest: "" };
      }
      return { safe: text.slice(0, lastOpen), rest: suffix };
    }
  }
  const bareTail = text.match(BARE_PLACEHOLDER_TAIL_RE);
  if (bareTail) {
    if (bareTail[0].length > MAX_PENDING_PLACEHOLDER_CHARS) {
      return { safe: text, rest: "" };
    }
    return { safe: text.slice(0, text.length - bareTail[0].length), rest: bareTail[0] };
  }
  return { safe: text, rest: "" };
}
