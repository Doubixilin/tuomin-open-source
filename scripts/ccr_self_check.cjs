#!/usr/bin/env node
"use strict";

const assert = require("node:assert/strict");
const http = require("node:http");
const TuominTransformer = require("../integrations/ccr/tuomin-transformer.cjs");

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

async function startFakeTuomin() {
  const seen = { open: 0, mask: 0, refill: 0 };
  const server = http.createServer(async (req, res) => {
    const payload = await readJson(req);
    if (req.method === "POST" && req.url === "/session/open") {
      seen.open += 1;
      writeJson(res, 200, { session_id: "sess_self_check", profile: payload.profile || "agent" });
      return;
    }
    if (req.method === "POST" && req.url === "/session/sess_self_check/mask") {
      seen.mask += 1;
      writeJson(res, 200, {
        masked: payload.text
          .replaceAll("虚构星橙集团", "<ORG_001>")
          .replaceAll("13800138000", "<PHONE_001>"),
        blocked_labels: [],
      });
      return;
    }
    if (req.method === "POST" && req.url === "/session/sess_self_check/refill") {
      seen.refill += 1;
      writeJson(res, 200, {
        text: payload.text
          .replaceAll("<ORG_001>", "虚构星橙集团")
          .replaceAll("<PHONE_001>", "13800138000"),
        status: "ok",
        unknown_placeholders: [],
        restored_count: payload.text.includes("<") ? 1 : 0,
      });
      return;
    }
    writeJson(res, 404, { detail: "not found" });
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  const { port } = server.address();
  return { server, seen, baseUrl: `http://127.0.0.1:${port}` };
}

async function main() {
  const fake = await startFakeTuomin();
  try {
    const transformer = new TuominTransformer({ baseUrl: fake.baseUrl, requestTimeoutMs: 1000 });

    const outbound = await transformer.transformRequestIn({
      model: "self-check",
      messages: [
        { role: "system", content: "系统提示：虚构星橙集团默认不脱敏" },
        { role: "user", content: "请分析虚构星橙集团，电话 13800138000。" },
      ],
    });
    assert.equal(outbound.messages[0].content, "系统提示：虚构星橙集团默认不脱敏");
    assert.equal(outbound.messages[1].content, "请分析<ORG_001>，电话 <PHONE_001>。");

    const jsonResponse = await transformer.transformResponseOut(new Response(
      JSON.stringify({ choices: [{ message: { content: "结论涉及<ORG_001>。" } }] }),
      { headers: { "content-type": "application/json" } }
    ));
    assert.equal((await jsonResponse.json()).choices[0].message.content, "结论涉及虚构星橙集团。");

    const stream = new ReadableStream({
      start(controller) {
        const encoder = new TextEncoder();
        controller.enqueue(encoder.encode('data: {"choices":[{"delta":{"content":"<ORG_"}}]}\n\n'));
        controller.enqueue(encoder.encode('data: {"choices":[{"delta":{"content":"001> OK"}}]}\n\n'));
        controller.enqueue(encoder.encode("data: [DONE]\n\n"));
        controller.close();
      },
    });
    const streamed = await transformer.transformResponseOut(
      new Response(stream, { headers: { "content-type": "text/event-stream" } })
    );
    const streamedText = await streamed.text();
    const content = streamedText.split("\n")
      .filter((line) => line.startsWith("data: {"))
      .map((line) => JSON.parse(line.slice(6)).choices[0].delta.content || "")
      .join("");
    assert.equal(content, "虚构星橙集团 OK");
    assert.doesNotMatch(streamedText, /<ORG_/);

    console.log(JSON.stringify({
      status: "ok",
      checked: ["outbound_mask", "json_refill", "stream_refill_split_placeholder"],
      fake_tuomin_calls: fake.seen,
    }, null, 2));
  } finally {
    await new Promise((resolve) => fake.server.close(resolve));
  }
}

main().catch((error) => {
  console.error(JSON.stringify({ status: "error", message: error.message }, null, 2));
  process.exitCode = 1;
});
