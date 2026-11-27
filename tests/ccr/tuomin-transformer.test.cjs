const { afterEach, beforeEach, test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const http = require("node:http");
const os = require("node:os");
const path = require("node:path");

const TuominTransformer = require("../../integrations/ccr/tuomin-transformer.cjs");

let server;
let baseUrl;
let requests;

function readJson(req) {
  return new Promise((resolve, reject) => {
    let body = "";
    req.setEncoding("utf8");
    req.on("data", (chunk) => {
      body += chunk;
    });
    req.on("end", () => {
      try {
        resolve(body ? JSON.parse(body) : {});
      } catch (error) {
        reject(error);
      }
    });
    req.on("error", reject);
  });
}

function writeJson(res, statusCode, payload) {
  res.writeHead(statusCode, { "content-type": "application/json" });
  res.end(JSON.stringify(payload));
}

function writeRaw(res, statusCode, body) {
  res.writeHead(statusCode, { "content-type": "application/json" });
  res.end(body);
}

async function startTuominStub(handler) {
  requests = [];
  server = http.createServer(async (req, res) => {
    try {
      const payload = await readJson(req);
      requests.push({ method: req.method, url: req.url, payload });
      await handler(req, res, payload);
    } catch (error) {
      writeJson(res, 500, { detail: error.message });
    }
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  const { port } = server.address();
  baseUrl = `http://127.0.0.1:${port}`;
}

function defaultHandler(req, res, payload) {
  if (req.method === "POST" && req.url === "/session/open") {
    writeJson(res, 200, { session_id: "sess_test", profile: payload.profile || "agent" });
    return;
  }
  if (req.method === "POST" && req.url === "/session/sess_test/mask") {
    if (payload.text.includes("绝密策略")) {
      writeJson(res, 200, { masked: "<LEGAL_STRATEGY_001>", blocked_labels: ["LEGAL_STRATEGY"] });
      return;
    }
    const masked = payload.text
      .replaceAll("绿洲集团", "<ORG_001>")
      .replaceAll("13800138000", "<PHONE_001>");
    writeJson(res, 200, { masked, blocked_labels: [] });
    return;
  }
  if (req.method === "POST" && req.url === "/session/sess_test/refill") {
    // Mirror the real contract (SessionRedactor.refill): status "ok"/"blocked",
    // altered covers both angle-bracket damage and bare placeholder tokens.
    const altered = [];
    if (payload.text.includes("<ORG-001>")) {
      altered.push("<ORG-001>");
    }
    if (/(?<!<)\bORG_001\b(?!>)/.test(payload.text)) {
      altered.push("ORG_001");
    }
    if (altered.length > 0) {
      writeJson(res, 200, {
        status: "blocked",
        text: null,
        error_types: ["altered_placeholder"],
        altered_placeholders: altered,
        unknown_placeholders: [],
        missing_placeholders: [],
        restored_count: 0
      });
      return;
    }
    const text = payload.text.replaceAll("<ORG_001>", "绿洲集团").replaceAll("<PHONE_001>", "13800138000");
    writeJson(res, 200, {
      status: "ok",
      text,
      unknown_placeholders: [],
      altered_placeholders: [],
      missing_placeholders: [],
      restored_count: text === payload.text ? 0 : 1
    });
    return;
  }
  writeJson(res, 404, { detail: "not found" });
}

beforeEach(async () => {
  await startTuominStub(defaultHandler);
});

afterEach(async () => {
  if (server) {
    await new Promise((resolve) => server.close(resolve));
  }
  server = undefined;
  requests = [];
});

test("masks outbound text fields while preserving system and non-text blocks", async () => {
  const transformer = new TuominTransformer({ baseUrl, requestTimeoutMs: 1000 });
  const imageBlock = { type: "image", source: { type: "base64", media_type: "image/png", data: "abc" } };
  const request = {
    model: "demo",
    messages: [
      { role: "system", content: "系统提示：绿洲集团仍保持原样" },
      { role: "user", content: "请分析绿洲集团，电话13800138000。" },
      { role: "user", content: [{ type: "text", text: "绿洲集团的项目" }, imageBlock] },
      { role: "assistant", content: "上一轮提到绿洲集团" },
      { role: "tool", content: "工具结果包含绿洲集团" }
    ]
  };

  const result = await transformer.transformRequestIn(request, {}, {});

  assert.equal(result.messages[0].content, "系统提示：绿洲集团仍保持原样");
  assert.equal(result.messages[1].content, "请分析<ORG_001>，电话<PHONE_001>。");
  assert.equal(result.messages[2].content[0].text, "<ORG_001>的项目");
  assert.equal(result.messages[2].content[1], imageBlock);
  assert.equal(result.messages[3].content, "上一轮提到<ORG_001>");
  assert.equal(result.messages[4].content, "工具结果包含<ORG_001>");
  assert.equal(requests.filter((entry) => entry.url === "/session/open").length, 1);
});

test("masks tool call argument strings before they are sent upstream", async () => {
  const transformer = new TuominTransformer({ baseUrl, requestTimeoutMs: 1000 });
  const request = {
    messages: [
      {
        role: "assistant",
        content: null,
        tool_calls: [
          {
            id: "call_1",
            type: "function",
            function: {
              name: "lookup_contract",
              arguments: "{\"company\":\"绿洲集团\",\"phone\":\"13800138000\"}"
            }
          }
        ]
      }
    ]
  };

  const result = await transformer.transformRequestIn(request, {}, {});

  assert.equal(
    result.messages[0].tool_calls[0].function.arguments,
    "{\"company\":\"<ORG_001>\",\"phone\":\"<PHONE_001>\"}"
  );
}
);

test("can mask system prompts when includeSystem is enabled", async () => {
  const transformer = new TuominTransformer({ baseUrl, includeSystem: true });
  const request = { messages: [{ role: "system", content: "系统提示：绿洲集团" }] };

  const result = await transformer.transformRequestIn(request, {}, {});

  assert.equal(result.messages[0].content, "系统提示：<ORG_001>");
});

test("refills non-streaming JSON responses and records safe refill headers", async () => {
  const transformer = new TuominTransformer({ baseUrl });
  await transformer.transformRequestIn({ messages: [{ role: "user", content: "绿洲集团" }] }, {}, {});
  const response = new Response(
    JSON.stringify({
      id: "chatcmpl_1",
      choices: [{ message: { role: "assistant", content: "结论涉及<ORG_001>。" } }]
    }),
    { status: 200, headers: { "content-type": "application/json" } }
  );

  const transformed = await transformer.transformResponseOut(response, {});
  const payload = await transformed.json();

  assert.equal(payload.choices[0].message.content, "结论涉及绿洲集团。");
  assert.equal(transformed.headers.get("x-tuomin-refill-restored-count"), "1");
  assert.equal(transformed.headers.get("x-tuomin-refill-unknown-count"), "0");
});

test("fails closed when refill reports unknown placeholders", async () => {
  await new Promise((resolve) => server.close(resolve));
  await startTuominStub((req, res, payload) => {
    if (req.url === "/session/open") {
      writeJson(res, 200, { session_id: "sess_test" });
      return;
    }
    if (req.url === "/session/sess_test/mask") {
      writeJson(res, 200, { masked: payload.text.replaceAll("绿洲集团", "<ORG_001>"), blocked_labels: [] });
      return;
    }
    if (req.url === "/session/sess_test/refill") {
      writeJson(res, 200, { text: payload.text, unknown_placeholders: ["<PERSON_999>"], restored_count: 0 });
      return;
    }
    writeJson(res, 404, { detail: "not found" });
  });
  const transformer = new TuominTransformer({ baseUrl });
  await transformer.transformRequestIn({ messages: [{ role: "user", content: "绿洲集团" }] }, {}, {});
  const response = new Response(
    JSON.stringify({ choices: [{ message: { role: "assistant", content: "涉及<PERSON_999>。" } }] }),
    { status: 200, headers: { "content-type": "application/json" } }
  );

  await assert.rejects(() => transformer.transformResponseOut(response, {}), /unknown placeholders: <PERSON_999>/);
});

test("fails closed when refill reports altered placeholders", async () => {
  await new Promise((resolve) => server.close(resolve));
  await startTuominStub((req, res, payload) => {
    if (req.url === "/session/open") {
      writeJson(res, 200, { session_id: "sess_test" });
      return;
    }
    if (req.url === "/session/sess_test/mask") {
      writeJson(res, 200, { masked: payload.text.replaceAll("绿洲集团", "<ORG_001>"), blocked_labels: [] });
      return;
    }
    if (req.url === "/session/sess_test/refill") {
      writeJson(res, 200, {
        status: "blocked",
        text: null,
        error_types: ["altered_placeholder"],
        altered_placeholders: ["<ORG-001>"],
        unknown_placeholders: [],
        missing_placeholders: [],
        restored_count: 0
      });
      return;
    }
    writeJson(res, 404, { detail: "not found" });
  });
  const transformer = new TuominTransformer({ baseUrl });
  await transformer.transformRequestIn({ messages: [{ role: "user", content: "绿洲集团" }] }, {}, {});
  const response = new Response(
    JSON.stringify({ choices: [{ message: { role: "assistant", content: "涉及<ORG-001>。" } }] }),
    { status: 200, headers: { "content-type": "application/json" } }
  );

  await assert.rejects(() => transformer.transformResponseOut(response, {}), /altered placeholders: <ORG-001>/);
});


test("refills streaming SSE content when a placeholder is split across chunks", async () => {
  const transformer = new TuominTransformer({ baseUrl });
  await transformer.transformRequestIn({ messages: [{ role: "user", content: "绿洲集团" }] }, {}, {});
  const stream = new ReadableStream({
    start(controller) {
      const encoder = new TextEncoder();
      controller.enqueue(encoder.encode('data: {"choices":[{"delta":{"content":"结论涉及<ORG_"}}]}\n\n'));
      controller.enqueue(encoder.encode('data: {"choices":[{"delta":{"content":"001>完成"}}]}\n\n'));
      controller.enqueue(encoder.encode("data: [DONE]\n\n"));
      controller.close();
    }
  });
  const response = new Response(stream, { headers: { "content-type": "text/event-stream" } });

  const transformed = await transformer.transformResponseOut(response, {});
  const text = await transformed.text();

  assert.match(text, /结论涉及/);
  assert.match(text, /绿洲集团完成/);
  assert.doesNotMatch(text, /<ORG_/);
  assert.match(text, /data: \[DONE\]/);
});

test("fails closed when tuomin service cannot open a session", async () => {
  await new Promise((resolve) => server.close(resolve));
  await startTuominStub((req, res) => writeJson(res, 503, { detail: "service unavailable" }));
  const transformer = new TuominTransformer({ baseUrl, requestTimeoutMs: 1000 });

  await assert.rejects(
    () => transformer.transformRequestIn({ messages: [{ role: "user", content: "绿洲集团" }] }, {}, {}),
    /tuomin fail-closed/
  );
});

test("fails closed when tuomin request times out", async () => {
  await new Promise((resolve) => server.close(resolve));
  await startTuominStub(() => {
    // Intentionally leave the HTTP response open so the transformer timeout path is exercised.
  });
  const transformer = new TuominTransformer({ baseUrl, requestTimeoutMs: 20 });

  await assert.rejects(
    () => transformer.transformRequestIn({ messages: [{ role: "user", content: "绿洲集团" }] }, {}, {}),
    /tuomin fail-closed/
  );
});

test("fails closed when mask returns invalid JSON", async () => {
  await new Promise((resolve) => server.close(resolve));
  await startTuominStub((req, res) => {
    if (req.url === "/session/open") {
      writeJson(res, 200, { session_id: "sess_test" });
      return;
    }
    writeRaw(res, 200, "not json");
  });
  const transformer = new TuominTransformer({ baseUrl, requestTimeoutMs: 1000 });

  await assert.rejects(
    () => transformer.transformRequestIn({ messages: [{ role: "user", content: "绿洲集团" }] }, {}, {}),
    /invalid JSON/
  );
});

test("fails closed when mask returns an HTTP error", async () => {
  await new Promise((resolve) => server.close(resolve));
  await startTuominStub((req, res) => {
    if (req.url === "/session/open") {
      writeJson(res, 200, { session_id: "sess_test" });
      return;
    }
    if (req.url === "/session/sess_test/mask") {
      writeJson(res, 503, { detail: "mask unavailable" });
      return;
    }
    writeJson(res, 404, { detail: "not found" });
  });
  const transformer = new TuominTransformer({ baseUrl, requestTimeoutMs: 1000 });

  await assert.rejects(
    () => transformer.transformRequestIn({ messages: [{ role: "user", content: "绿洲集团" }] }, {}, {}),
    /tuomin fail-closed/
  );
});

test("fails closed when non-stream refill returns an HTTP error", async () => {
  await new Promise((resolve) => server.close(resolve));
  await startTuominStub((req, res, payload) => {
    if (req.url === "/session/open") {
      writeJson(res, 200, { session_id: "sess_test" });
      return;
    }
    if (req.url === "/session/sess_test/mask") {
      writeJson(res, 200, { masked: payload.text.replaceAll("绿洲集团", "<ORG_001>"), blocked_labels: [] });
      return;
    }
    if (req.url === "/session/sess_test/refill") {
      writeJson(res, 503, { detail: "refill unavailable" });
      return;
    }
    writeJson(res, 404, { detail: "not found" });
  });
  const transformer = new TuominTransformer({ baseUrl, requestTimeoutMs: 1000 });
  await transformer.transformRequestIn({ messages: [{ role: "user", content: "绿洲集团" }] }, {}, {});
  const response = new Response(
    JSON.stringify({ choices: [{ message: { role: "assistant", content: "涉及<ORG_001>。" } }] }),
    { status: 200, headers: { "content-type": "application/json" } }
  );

  await assert.rejects(() => transformer.transformResponseOut(response, {}), /tuomin fail-closed/);
});

test("fails closed while reading a stream when refill returns an HTTP error", async () => {
  await new Promise((resolve) => server.close(resolve));
  await startTuominStub((req, res) => {
    if (req.url === "/session/open") {
      writeJson(res, 200, { session_id: "sess_test" });
      return;
    }
    if (req.url === "/session/sess_test/mask") {
      writeJson(res, 200, { masked: payloadSafeMask(req), blocked_labels: [] });
      return;
    }
    if (req.url === "/session/sess_test/refill") {
      writeJson(res, 503, { detail: "refill unavailable" });
      return;
    }
    writeJson(res, 404, { detail: "not found" });
  });
  const transformer = new TuominTransformer({ baseUrl, requestTimeoutMs: 1000 });
  await transformer.transformRequestIn({ messages: [{ role: "user", content: "绿洲集团" }] }, {}, {});
  const stream = new ReadableStream({
    start(controller) {
      const encoder = new TextEncoder();
      controller.enqueue(encoder.encode('data: {"choices":[{"delta":{"content":"<ORG_001>"}}]}\n\n'));
      controller.close();
    }
  });
  const response = new Response(stream, { headers: { "content-type": "text/event-stream" } });

  const transformed = await transformer.transformResponseOut(response, {});

  await assert.rejects(() => transformed.text(), /tuomin fail-closed/);
});

test("fails closed when a masked request reports blocked labels", async () => {
  const transformer = new TuominTransformer({ baseUrl, failOnBlocked: true });

  await assert.rejects(
    () => transformer.transformRequestIn({ messages: [{ role: "user", content: "绝密策略" }] }, {}, {}),
    /blocked labels: LEGAL_STRATEGY/
  );
});


test("writes an outbound snapshot only after masking when enabled", async () => {
  const snapshotDir = fs.mkdtempSync(path.join(os.tmpdir(), "tuomin-out-snapshot-"));
  try {
    const transformer = new TuominTransformer({ baseUrl, outboundSnapshotDir: snapshotDir });

    await transformer.transformRequestIn({ messages: [{ role: "user", content: "\u8bf7\u5206\u6790\u7eff\u6d32\u96c6\u56e2\u3002" }] }, {}, {});

    const files = fs.readdirSync(snapshotDir);
    assert.equal(files.length, 1);
    assert.match(files[0], /^outbound-to-provider-.*\.json$/);

    const snapshot = JSON.parse(fs.readFileSync(path.join(snapshotDir, files[0]), "utf8"));
    const serialized = JSON.stringify(snapshot.payload);
    assert.equal(snapshot.direction, "outbound_to_provider");
    assert.match(serialized, /<ORG_001>/);
    assert.doesNotMatch(serialized, /\u7eff\u6d32\u96c6\u56e2/);
  } finally {
    fs.rmSync(snapshotDir, { recursive: true, force: true });
  }
});

test("fails closed when the mask response is missing the masked field", async () => {
  await new Promise((resolve) => server.close(resolve));
  await startTuominStub((req, res) => {
    if (req.url === "/session/open") {
      writeJson(res, 200, { session_id: "sess_test" });
      return;
    }
    if (req.url === "/session/sess_test/mask") {
      writeJson(res, 200, { blocked_labels: [] });
      return;
    }
    writeJson(res, 404, { detail: "not found" });
  });
  const transformer = new TuominTransformer({ baseUrl, requestTimeoutMs: 1000 });

  await assert.rejects(
    () => transformer.transformRequestIn({ messages: [{ role: "user", content: "绿洲集团" }] }, {}, {}),
    (error) => {
      assert.equal(error.code, "TUOMIN_FAIL_CLOSED");
      assert.match(error.message, /missing masked text/);
      return true;
    }
  );
});

test("fails closed when refill returns an unknown status", async () => {
  await new Promise((resolve) => server.close(resolve));
  await startTuominStub((req, res, payload) => {
    if (req.url === "/session/open") {
      writeJson(res, 200, { session_id: "sess_test" });
      return;
    }
    if (req.url === "/session/sess_test/mask") {
      writeJson(res, 200, { masked: payload.text.replaceAll("绿洲集团", "<ORG_001>"), blocked_labels: [] });
      return;
    }
    if (req.url === "/session/sess_test/refill") {
      writeJson(res, 200, { status: "degraded", text: payload.text, unknown_placeholders: [], restored_count: 0 });
      return;
    }
    writeJson(res, 404, { detail: "not found" });
  });
  const transformer = new TuominTransformer({ baseUrl });
  await transformer.transformRequestIn({ messages: [{ role: "user", content: "绿洲集团" }] }, {}, {});
  const response = new Response(
    JSON.stringify({ choices: [{ message: { role: "assistant", content: "涉及<ORG_001>。" } }] }),
    { status: 200, headers: { "content-type": "application/json" } }
  );

  await assert.rejects(
    () => transformer.transformResponseOut(response, {}),
    (error) => {
      assert.equal(error.code, "TUOMIN_FAIL_CLOSED");
      assert.match(error.message, /unexpected status: degraded/);
      return true;
    }
  );
});

test("fails closed when a refill ok response is missing the text field", async () => {
  await new Promise((resolve) => server.close(resolve));
  await startTuominStub((req, res, payload) => {
    if (req.url === "/session/open") {
      writeJson(res, 200, { session_id: "sess_test" });
      return;
    }
    if (req.url === "/session/sess_test/mask") {
      writeJson(res, 200, { masked: payload.text.replaceAll("绿洲集团", "<ORG_001>"), blocked_labels: [] });
      return;
    }
    if (req.url === "/session/sess_test/refill") {
      writeJson(res, 200, { status: "ok", unknown_placeholders: [], restored_count: 0 });
      return;
    }
    writeJson(res, 404, { detail: "not found" });
  });
  const transformer = new TuominTransformer({ baseUrl });
  await transformer.transformRequestIn({ messages: [{ role: "user", content: "绿洲集团" }] }, {}, {});
  const response = new Response(
    JSON.stringify({ choices: [{ message: { role: "assistant", content: "涉及<ORG_001>。" } }] }),
    { status: 200, headers: { "content-type": "application/json" } }
  );

  await assert.rejects(
    () => transformer.transformResponseOut(response, {}),
    (error) => {
      assert.equal(error.code, "TUOMIN_FAIL_CLOSED");
      assert.match(error.message, /missing text/);
      return true;
    }
  );
});

test("masks tool_use input string leaves while preserving structure", async () => {
  const transformer = new TuominTransformer({ baseUrl });
  const request = {
    messages: [
      {
        role: "assistant",
        content: [
          { type: "text", text: "我来查询绿洲集团" },
          {
            type: "tool_use",
            id: "toolu_1",
            name: "lookup_contract",
            input: { company: "绿洲集团", phone: "13800138000", count: 3, filters: { city: "绿洲集团" } }
          }
        ]
      }
    ]
  };

  const result = await transformer.transformRequestIn(request, {}, {});

  const toolUse = result.messages[0].content[1];
  assert.equal(result.messages[0].content[0].text, "我来查询<ORG_001>");
  assert.equal(toolUse.input.company, "<ORG_001>");
  assert.equal(toolUse.input.phone, "<PHONE_001>");
  assert.equal(toolUse.input.count, 3);
  assert.equal(toolUse.input.filters.city, "<ORG_001>");
  assert.equal(toolUse.name, "lookup_contract");
  assert.equal(toolUse.id, "toolu_1");
});

test("leaves a top-level system prompt untouched by default", async () => {
  const transformer = new TuominTransformer({ baseUrl });
  const request = {
    system: "系统提示：绿洲集团",
    messages: [{ role: "user", content: "分析绿洲集团" }]
  };

  const result = await transformer.transformRequestIn(request, {}, {});

  assert.equal(result.system, "系统提示：绿洲集团");
  assert.equal(result.messages[0].content, "分析<ORG_001>");
});

test("masks top-level system string and blocks when includeSystem is enabled", async () => {
  const transformer = new TuominTransformer({ baseUrl, includeSystem: true });

  const stringForm = await transformer.transformRequestIn(
    { system: "系统提示：绿洲集团", messages: [{ role: "user", content: "你好" }] },
    {},
    {}
  );
  assert.equal(stringForm.system, "系统提示：<ORG_001>");

  const blockForm = await transformer.transformRequestIn(
    { system: [{ type: "text", text: "系统提示：绿洲集团" }], messages: [{ role: "user", content: "你好" }] },
    {},
    {}
  );
  assert.equal(blockForm.system[0].text, "系统提示：<ORG_001>");
});

test("masks tool definition descriptions but keeps structural fields verbatim", async () => {
  const transformer = new TuominTransformer({ baseUrl });
  const request = {
    messages: [{ role: "user", content: "查一下" }],
    tools: [
      {
        name: "lookup_contract",
        description: "查询绿洲集团的合同",
        input_schema: {
          type: "object",
          properties: {
            company: { type: "string", description: "公司名，例如绿洲集团" }
          },
          required: ["company"]
        }
      }
    ]
  };

  const result = await transformer.transformRequestIn(request, {}, {});

  const tool = result.tools[0];
  assert.equal(tool.name, "lookup_contract");
  assert.equal(tool.description, "查询<ORG_001>的合同");
  assert.equal(tool.input_schema.type, "object");
  assert.equal(tool.input_schema.properties.company.type, "string");
  assert.equal(tool.input_schema.properties.company.description, "公司名，例如<ORG_001>");
  assert.deepEqual(tool.input_schema.required, ["company"]);
});

test("masks tool call arguments given as an object", async () => {
  const transformer = new TuominTransformer({ baseUrl });
  const request = {
    messages: [
      {
        role: "assistant",
        content: null,
        tool_calls: [
          {
            id: "call_1",
            type: "function",
            function: { name: "lookup_contract", arguments: { company: "绿洲集团", count: 2 } }
          }
        ]
      }
    ]
  };

  const result = await transformer.transformRequestIn(request, {}, {});

  assert.deepEqual(result.messages[0].tool_calls[0].function.arguments, { company: "<ORG_001>", count: 2 });
  assert.equal(result.messages[0].tool_calls[0].function.name, "lookup_contract");
});

test("refills CRLF-delimited SSE streams", async () => {
  const transformer = new TuominTransformer({ baseUrl });
  await transformer.transformRequestIn({ messages: [{ role: "user", content: "绿洲集团" }] }, {}, {});
  const response = sseJsonResponse([
    'data: {"choices":[{"delta":{"content":"结论涉及<ORG_001>"}}]}\r\n\r\n',
    "data: [DONE]\r\n\r\n"
  ]);

  const transformed = await transformer.transformResponseOut(response, {});
  const text = await transformed.text();

  assert.match(text, /绿洲集团/);
  assert.doesNotMatch(text, /<ORG_001>/);
  assert.match(text, /data: \[DONE\]/);
});

test("runs placeholder integrity checks on CRLF-delimited streams", async () => {
  const transformer = new TuominTransformer({ baseUrl });
  await transformer.transformRequestIn({ messages: [{ role: "user", content: "绿洲集团" }] }, {}, {});
  const response = sseJsonResponse([
    'data: {"choices":[{"delta":{"content":"涉及<ORG-001>泄露"}}]}\r\n\r\n',
    "data: [DONE]\r\n\r\n"
  ]);

  const transformed = await transformer.transformResponseOut(response, {});

  await assert.rejects(() => transformed.text(), /altered placeholders: <ORG-001>/);
});

test("blocks an altered placeholder split across chunks", async () => {
  const transformer = new TuominTransformer({ baseUrl });
  await transformer.transformRequestIn({ messages: [{ role: "user", content: "绿洲集团" }] }, {}, {});
  const response = sseJsonResponse([
    'data: {"choices":[{"delta":{"content":"涉及<ORG-00"}}]}\n\n',
    'data: {"choices":[{"delta":{"content":"1>。"}}]}\n\n',
    "data: [DONE]\n\n"
  ]);

  const transformed = await transformer.transformResponseOut(response, {});

  await assert.rejects(() => transformed.text(), /altered placeholders: <ORG-001>/);
});

test("blocks a bare placeholder split across chunks", async () => {
  const transformer = new TuominTransformer({ baseUrl });
  await transformer.transformRequestIn({ messages: [{ role: "user", content: "绿洲集团" }] }, {}, {});
  const response = sseJsonResponse([
    'data: {"choices":[{"delta":{"content":"涉及ORG_00"}}]}\n\n',
    'data: {"choices":[{"delta":{"content":"1。"}}]}\n\n',
    "data: [DONE]\n\n"
  ]);

  const transformed = await transformer.transformResponseOut(response, {});

  await assert.rejects(() => transformed.text(), /altered placeholders: ORG_001/);
});

test("handles SSE data lines without a space after the colon", async () => {
  const transformer = new TuominTransformer({ baseUrl });
  await transformer.transformRequestIn({ messages: [{ role: "user", content: "绿洲集团" }] }, {}, {});
  const response = sseJsonResponse([
    'data:{"choices":[{"delta":{"content":"<ORG_001>"}}]}\n\n',
    "data:[DONE]\n\n"
  ]);

  const transformed = await transformer.transformResponseOut(response, {});
  const text = await transformed.text();

  assert.match(text, /绿洲集团/);
  assert.doesNotMatch(text, /<ORG_001>/);
  assert.match(text, /data: \[DONE\]/);
});

test("joins multi-line SSE data before parsing", async () => {
  const transformer = new TuominTransformer({ baseUrl });
  await transformer.transformRequestIn({ messages: [{ role: "user", content: "绿洲集团" }] }, {}, {});
  const response = sseJsonResponse([
    'data: {"choices":[{"delta":\ndata: {"content":"<ORG_001>"}}]}\n\n',
    "data: [DONE]\n\n"
  ]);

  const transformed = await transformer.transformResponseOut(response, {});
  const text = await transformed.text();

  assert.match(text, /绿洲集团/);
  assert.doesNotMatch(text, /<ORG_001>/);
});

test("does not stall or grow without bound on an over-long unclosed angle run", async () => {
  const transformer = new TuominTransformer({ baseUrl });
  await transformer.transformRequestIn({ messages: [{ role: "user", content: "绿洲集团" }] }, {}, {});
  const run = "A".repeat(1000);
  const response = sseJsonResponse([
    `data: {"choices":[{"delta":{"content":"<${run}"}}]}\n\n`,
    `data: {"choices":[{"delta":{"content":"${run}"}}]}\n\n`,
    `data: {"choices":[{"delta":{"content":"${run}"}}]}\n\n`,
    `data: {"choices":[{"delta":{"content":"${run}"}}]}\n\n`,
    `data: {"choices":[{"delta":{"content":"${run}"}}]}\n\n`,
    "data: [DONE]\n\n"
  ]);

  const transformed = await transformer.transformResponseOut(response, {});
  const text = await transformed.text();

  const pieces = [...text.matchAll(/"content":"([^"]*)"/g)].map((match) => match[1]);
  assert.equal(pieces.join(""), `<${"A".repeat(5000)}`);
  assert.match(text, /data: \[DONE\]/);
});

test("refills a well-formed placeholder split across three chunks", async () => {
  const transformer = new TuominTransformer({ baseUrl });
  await transformer.transformRequestIn({ messages: [{ role: "user", content: "绿洲集团" }] }, {}, {});
  const response = sseJsonResponse([
    'data: {"choices":[{"delta":{"content":"<OR"}}]}\n\n',
    'data: {"choices":[{"delta":{"content":"G_00"}}]}\n\n',
    'data: {"choices":[{"delta":{"content":"1>的项目"}}]}\n\n',
    "data: [DONE]\n\n"
  ]);

  const transformed = await transformer.transformResponseOut(response, {});
  const text = await transformed.text();

  assert.match(text, /绿洲集团的项目/);
  assert.doesNotMatch(text, /<ORG_001>/);
});

function sseJsonResponse(events) {
  const stream = new ReadableStream({
    start(controller) {
      const encoder = new TextEncoder();
      for (const event of events) {
        controller.enqueue(encoder.encode(event));
      }
      controller.close();
    }
  });
  return new Response(stream, { headers: { "content-type": "text/event-stream" } });
}

function payloadSafeMask(req) {
  return req.url.includes("mask") ? "<ORG_001>" : "";
}

test("reopens an evaporated session and retries mask once (TTL/gateway restart)", async () => {
  await new Promise((resolve) => server.close(resolve));
  let openCount = 0;
  await startTuominStub((req, res, payload) => {
    if (req.url === "/session/open") {
      openCount += 1;
      writeJson(res, 200, { session_id: `sess_${openCount}` });
      return;
    }
    if (req.url === "/session/sess_1/mask") {
      writeJson(res, 404, { detail: "session 不存在或已过期" });
      return;
    }
    if (req.url === "/session/sess_2/mask") {
      writeJson(res, 200, { masked: payload.text.replaceAll("绿洲集团", "<ORG_001>"), blocked_labels: [] });
      return;
    }
    writeJson(res, 404, { detail: "not found" });
  });
  const transformer = new TuominTransformer({ baseUrl, requestTimeoutMs: 1000 });

  const result = await transformer.transformRequestIn(
    { messages: [{ role: "user", content: "绿洲集团" }] }, {}, {}
  );

  assert.equal(result.messages[0].content, "<ORG_001>");
  assert.equal(openCount, 2); // initial open + exactly one reopen
  assert.equal(requests.filter((entry) => entry.url === "/session/sess_2/mask").length, 1);
});

test("stays fail-closed when the reopened session also 404s", async () => {
  await new Promise((resolve) => server.close(resolve));
  let openCount = 0;
  await startTuominStub((req, res) => {
    if (req.url === "/session/open") {
      openCount += 1;
      writeJson(res, 200, { session_id: `sess_${openCount}` });
      return;
    }
    writeJson(res, 404, { detail: "not found" }); // every mask call 404s
  });
  const transformer = new TuominTransformer({ baseUrl, requestTimeoutMs: 1000 });

  await assert.rejects(
    () => transformer.transformRequestIn({ messages: [{ role: "user", content: "绿洲集团" }] }, {}, {}),
    (error) => {
      assert.equal(error.code, "TUOMIN_FAIL_CLOSED");
      return true;
    }
  );
  assert.equal(openCount, 2); // one initial open + exactly one reopen, no stampede
});

test("refill retries once on session 404 and survives for placeholder-free text", async () => {
  await new Promise((resolve) => server.close(resolve));
  let openCount = 0;
  await startTuominStub((req, res, payload) => {
    if (req.url === "/session/open") {
      openCount += 1;
      writeJson(res, 200, { session_id: `sess_${openCount}` });
      return;
    }
    if (req.url === "/session/sess_1/mask") {
      writeJson(res, 200, { masked: payload.text, blocked_labels: [] });
      return;
    }
    if (req.url === "/session/sess_1/refill") {
      writeJson(res, 404, { detail: "session 不存在或已过期" });
      return;
    }
    if (req.url === "/session/sess_2/refill") {
      writeJson(res, 200, {
        status: "ok", text: payload.text,
        unknown_placeholders: [], altered_placeholders: [], missing_placeholders: [],
        restored_count: 0
      });
      return;
    }
    writeJson(res, 404, { detail: "not found" });
  });
  const transformer = new TuominTransformer({ baseUrl });
  await transformer.transformRequestIn({ messages: [{ role: "user", content: "你好" }] }, {}, {});
  const response = new Response(
    JSON.stringify({ choices: [{ message: { role: "assistant", content: "普通回答，无占位符。" } }] }),
    { status: 200, headers: { "content-type": "application/json" } }
  );

  const transformed = await transformer.transformResponseOut(response, {});
  const payload = await transformed.json();

  assert.equal(payload.choices[0].message.content, "普通回答，无占位符。");
  assert.equal(openCount, 2);
});

test("dead-session placeholders stay fail-closed after refill reopen", async () => {
  await new Promise((resolve) => server.close(resolve));
  let openCount = 0;
  await startTuominStub((req, res, payload) => {
    if (req.url === "/session/open") {
      openCount += 1;
      writeJson(res, 200, { session_id: `sess_${openCount}` });
      return;
    }
    if (req.url === "/session/sess_1/mask") {
      writeJson(res, 200, { masked: payload.text.replaceAll("绿洲集团", "<ORG_001>"), blocked_labels: [] });
      return;
    }
    if (req.url === "/session/sess_1/refill") {
      writeJson(res, 404, { detail: "session 不存在或已过期" });
      return;
    }
    if (req.url === "/session/sess_2/refill") {
      // The fresh session does not know the dead session's placeholders.
      writeJson(res, 200, {
        status: "ok", text: payload.text,
        unknown_placeholders: ["<ORG_001>"], altered_placeholders: [], missing_placeholders: [],
        restored_count: 0
      });
      return;
    }
    writeJson(res, 404, { detail: "not found" });
  });
  const transformer = new TuominTransformer({ baseUrl });
  await transformer.transformRequestIn({ messages: [{ role: "user", content: "绿洲集团" }] }, {}, {});
  const response = new Response(
    JSON.stringify({ choices: [{ message: { role: "assistant", content: "涉及<ORG_001>。" } }] }),
    { status: 200, headers: { "content-type": "application/json" } }
  );

  await assert.rejects(
    () => transformer.transformResponseOut(response, {}),
    /unknown placeholders: <ORG_001>/
  );
});
