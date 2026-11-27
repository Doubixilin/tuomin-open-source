#!/usr/bin/env node
"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const http = require("node:http");
const os = require("node:os");
const path = require("node:path");
const { spawn, spawnSync } = require("node:child_process");

const ROOT = path.resolve(__dirname, "..");

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

async function listen(server) {
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  return `http://127.0.0.1:${server.address().port}`;
}

async function startFakeTuomin() {
  const calls = { open: 0, mask: 0, refill: 0 };
  const server = http.createServer(async (req, res) => {
    const payload = await readJson(req);
    if (req.url === "/session/open") {
      calls.open += 1;
      writeJson(res, 200, { session_id: "sess_ccr_e2e", profile: "agent" });
      return;
    }
    if (req.url === "/session/sess_ccr_e2e/mask") {
      calls.mask += 1;
      writeJson(res, 200, {
        masked: payload.text.replaceAll("虚构星橙集团", "<ORG_001>"),
        blocked_labels: [],
      });
      return;
    }
    if (req.url === "/session/sess_ccr_e2e/refill") {
      calls.refill += 1;
      writeJson(res, 200, {
        text: payload.text.replaceAll("<ORG_001>", "虚构星橙集团"),
        status: "ok",
        unknown_placeholders: [],
        restored_count: payload.text.includes("<ORG_001>") ? 1 : 0,
      });
      return;
    }
    writeJson(res, 404, { detail: "not found" });
  });
  return { server, calls, baseUrl: await listen(server) };
}

async function startFakeUpstream() {
  const seen = [];
  const server = http.createServer(async (req, res) => {
    const payload = await readJson(req);
    seen.push({ url: req.url, payload });
    writeJson(res, 200, {
      id: "chatcmpl_fake",
      object: "chat.completion",
      model: payload.model || "fake-model",
      choices: [
        {
          index: 0,
          message: { role: "assistant", content: "上游只看到<ORG_001>。" },
          finish_reason: "stop",
        },
      ],
      usage: { prompt_tokens: 1, completion_tokens: 1, total_tokens: 2 },
    });
  });
  return { server, seen, baseUrl: await listen(server) };
}

function waitForOutput(child, matcher, timeoutMs = 15000) {
  return new Promise((resolve, reject) => {
    let output = "";
    const timer = setTimeout(() => reject(new Error(`timeout waiting for ${matcher}; output:\n${output}`)), timeoutMs);
    const onData = (chunk) => {
      output += chunk.toString();
      if (output.includes(matcher)) {
        clearTimeout(timer);
        resolve(output);
      }
    };
    child.stdout.on("data", onData);
    child.stderr.on("data", onData);
    child.on("exit", (code) => {
      clearTimeout(timer);
      if (code === 0 && output.includes("server is running")) {
        resolve(output);
        return;
      }
      reject(new Error(`ccr exited early with code ${code}; output:\n${output}`));
    });
  });
}

async function fetchWithRetry(url, options, timeoutMs = 15000) {
  const started = Date.now();
  let lastError;
  while (Date.now() - started < timeoutMs) {
    try {
      return await fetch(url, options);
    } catch (error) {
      lastError = error;
      await new Promise((resolve) => setTimeout(resolve, 300));
    }
  }
  throw lastError;
}

async function closeServer(server) {
  await new Promise((resolve) => server.close(resolve));
}

async function main() {
  const tmpHome = fs.mkdtempSync(path.join(os.tmpdir(), "tuomin-ccr-e2e-"));
  const tuomin = await startFakeTuomin();
  const upstream = await startFakeUpstream();
  let ccr;
  let ccrOutput = "";
  const ccrBin = process.platform === "win32" ? "npx.cmd" : "npx";
  const ccrArgs = ["-y", "@musistudio/claude-code-router@2.0.0"];
  const ccrEnv = { ...process.env, HOME: tmpHome, USERPROFILE: tmpHome };
  const ccrProcess = process.platform === "win32" ? (process.env.ComSpec || "cmd.exe") : ccrBin;
  const ccrStartArgs = process.platform === "win32"
    ? ["/d", "/s", "/c", ccrBin, ...ccrArgs, "start"]
    : [...ccrArgs, "start"];
  const ccrStopArgs = process.platform === "win32"
    ? ["/d", "/s", "/c", ccrBin, ...ccrArgs, "stop"]
    : [...ccrArgs, "stop"];
  try {
    fs.mkdirSync(path.join(tmpHome, ".claude-code-router"), { recursive: true });
    fs.writeFileSync(
      path.join(tmpHome, ".claude-code-router", "config.json"),
      JSON.stringify({
        PORT: 3458,
        Providers: [
          {
            name: "fake",
            api_base_url: `${upstream.baseUrl}/v1/messages`,
            api_key: "fake-key",
            models: ["fake-model"],
            transformer: { use: ["tuomin", "anthropic"] },
          },
        ],
        Router: { default: "fake,fake-model" },
        transformers: [
          {
            name: "tuomin",
            path: path.join(ROOT, "integrations/ccr/tuomin-transformer.cjs"),
            options: {
              baseUrl: tuomin.baseUrl,
              appId: "agent",
              failOnBlocked: true,
              failOnUnknownPlaceholders: true,
              requestTimeoutMs: 1000,
            },
          },
        ],
      }, null, 2)
    );

    ccr = spawn(ccrProcess, ccrStartArgs, {
      env: ccrEnv,
      stdio: ["ignore", "pipe", "pipe"],
    });
    ccr.stdout.on("data", (chunk) => {
      ccrOutput += chunk.toString();
    });
    ccr.stderr.on("data", (chunk) => {
      ccrOutput += chunk.toString();
    });
    ccr.on("error", (error) => {
      ccrOutput += `ccr spawn error: ${error.message}\n`;
    });
    await waitForOutput(ccr, "Loaded JSON config");

    let response;
    try {
      response = await fetchWithRetry("http://127.0.0.1:3458/v1/messages", {
        method: "POST",
        headers: { "content-type": "application/json", "x-api-key": "fake-key" },
        body: JSON.stringify({
          model: "fake-model",
          max_tokens: 128,
          messages: [{ role: "user", content: "请分析虚构星橙集团。" }],
        }),
      });
    } catch (error) {
      const cause = error.cause ? `; cause=${error.cause.code || error.cause.message}` : "";
      throw new Error(`fetch to CCR failed: ${error.message}${cause}; ccrOutput=${ccrOutput}`);
    }
    const text = await response.text();
    assert.equal(response.status, 200, text);
    const payload = JSON.parse(text);
    assert.equal(upstream.seen.length, 1);
    assert.doesNotMatch(JSON.stringify(upstream.seen[0].payload), /虚构星橙集团/);
    assert.match(JSON.stringify(upstream.seen[0].payload), /<ORG_001>/);
    assert.match(JSON.stringify(payload), /虚构星橙集团/);
    assert.doesNotMatch(JSON.stringify(payload), /<ORG_001>/);

    console.log(JSON.stringify({
      status: "ok",
      checked: "ccr_v1_messages_to_fake_upstream",
      tuomin_calls: tuomin.calls,
      upstream_requests: upstream.seen.length,
    }, null, 2));
  } finally {
    if (ccr && !ccr.killed) {
      ccr.kill();
    }
    spawnSync(ccrProcess, ccrStopArgs, {
      env: ccrEnv,
      stdio: "ignore",
    });
    await closeServer(tuomin.server);
    await closeServer(upstream.server);
    fs.rmSync(tmpHome, { recursive: true, force: true });
  }
}

main().catch((error) => {
  console.error(JSON.stringify({ status: "error", message: error.message }, null, 2));
  process.exitCode = 1;
});
