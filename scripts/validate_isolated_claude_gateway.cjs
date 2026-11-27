#!/usr/bin/env node
"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const http = require("node:http");
const net = require("node:net");
const os = require("node:os");
const path = require("node:path");
const { spawn } = require("node:child_process");

const TuominTransformer = require("../integrations/ccr/tuomin-transformer.cjs");

const ROOT = path.resolve(__dirname, "..");
const TRANSFORMER_PATH = path.join(ROOT, "integrations/ccr/tuomin-transformer.cjs");
const RAW = {
  PERSON: "Synthetic Person Alpha",
  ORG: "Synthetic Org Oasis",
  PROJECT: "Project Starbridge",
  CONTRACT_ID: "HT-2026-TM-0001",
  BANK_ACCOUNT: "SYNTH-BANK-778899",
  CREDENTIAL: "tm_synthetic_secret_2026_abcdef",
  SYSTEM_URL: "https://synthetic.example.invalid/system",
};
const RAW_VALUES = Object.values(RAW);
const EXPECTED_PLACEHOLDERS = [
  "<PERSON_001>",
  "<ORG_001>",
  "<PROJECT_001>",
  "<CONTRACT_ID_001>",
  "<BANK_ACCOUNT_001>",
  "<CREDENTIAL_001>",
  "<URL_001>",
];

function syntheticPrompt() {
  return [
    `Person ${RAW.PERSON} from ${RAW.ORG} works on ${RAW.PROJECT}.`,
    `Contract ${RAW.CONTRACT_ID} uses account ${RAW.BANK_ACCOUNT}.`,
    `Credential ${RAW.CREDENTIAL} protects ${RAW.SYSTEM_URL}`,
  ].join(" ");
}

function requestBody() {
  return {
    model: "fake-claude-model",
    max_tokens: 256,
    messages: [
      { role: "system", content: "System message remains local to this validation." },
      { role: "user", content: syntheticPrompt() },
    ],
  };
}

function containsRaw(value) {
  const serialized = typeof value === "string" ? value : JSON.stringify(value);
  return RAW_VALUES.some((raw) => serialized.includes(raw));
}

function seenRawLabels(value) {
  const serialized = typeof value === "string" ? value : JSON.stringify(value);
  return Object.entries(RAW).filter(([, raw]) => serialized.includes(raw)).map(([label]) => label);
}

function seenPlaceholders(value) {
  const serialized = typeof value === "string" ? value : JSON.stringify(value);
  return EXPECTED_PLACEHOLDERS.filter((placeholder) => serialized.includes(placeholder));
}

function assertSafeSummary(summary) {
  const serialized = JSON.stringify(summary);
  assert.equal(containsRaw(serialized), false, "summary must not include raw synthetic values");
  const forbidden = new RegExp(["s" + "k-", "A" + "KIA", "Bear" + "er ", "real-key", "DEEPSEEK_API_KEY"].join("|"), "i");
  assert.doesNotMatch(serialized, forbidden);
}

function redactRawValues(message) {
  let safe = String(message || "");
  for (const raw of RAW_VALUES) {
    safe = safe.split(raw).join("[redacted]");
  }
  return safe;
}

function readJson(req) {
  return new Promise((resolve, reject) => {
    let body = "";
    req.setEncoding("utf8");
    req.on("data", (chunk) => { body += chunk; });
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

async function startFakeUpstream() {
  const seen = [];
  let mode = "echo";
  const server = http.createServer(async (req, res) => {
    const payload = await readJson(req);
    seen.push({ url: req.url, payload });
    let content;
    if (mode === "unknown") {
      content = "Provider invented <PERSON_999>.";
    } else if (mode === "altered") {
      content = "Provider damaged <ORG-001>.";
    } else if (mode === "missing") {
      content = "Provider mentioned only <PERSON_001>.";
    } else {
      content = `Provider saw: ${extractUserContent(payload)}`;
    }
    writeJson(res, 200, {
      id: "fake_upstream_response",
      type: "message",
      role: "assistant",
      content: [{ type: "text", text: content }],
    });
  });
  await listen(server, 0);
  return {
    server,
    seen,
    get baseUrl() { return `http://127.0.0.1:${server.address().port}`; },
    setMode(nextMode) { mode = nextMode; },
    reset() { seen.length = 0; mode = "echo"; },
  };
}

function extractUserContent(payload) {
  for (const message of payload.messages || []) {
    if (message.role === "user") {
      return typeof message.content === "string" ? message.content : JSON.stringify(message.content);
    }
  }
  return "";
}

async function listen(server, port) {
  await new Promise((resolve) => server.listen(port, "127.0.0.1", resolve));
}

function closeServer(server) {
  return new Promise((resolve) => server.close(resolve));
}

function freePort() {
  return new Promise((resolve, reject) => {
    const server = net.createServer();
    server.on("error", reject);
    server.listen(0, "127.0.0.1", () => {
      const port = server.address().port;
      server.close(() => resolve(port));
    });
  });
}

function writeSyntheticConfig(tmpRoot, fakeUpstreamUrl) {
  const dictionaryPath = path.join(tmpRoot, "synthetic-dictionary.json");
  const entries = Object.entries(RAW).map(([label, canonical]) => ({
    entry_id: `synthetic_${label.toLowerCase()}`,
    label,
    canonical_value: canonical,
    aliases: [],
    risk_level: label === "CREDENTIAL" ? "critical" : "high",
    status: "active",
    version: "isolated-validation",
  }));
  fs.writeFileSync(dictionaryPath, JSON.stringify(entries, null, 2), "utf8");

  const configPath = path.join(tmpRoot, "tuomin-apps.json");
  fs.writeFileSync(configPath, JSON.stringify({
    apps: {
      isolated_claude_validation: {
        profile: { base: "strict", name: "isolated_claude_validation", use_ner: false, refill_strict: true },
        dictionary: dictionaryPath,
      },
    },
  }, null, 2), "utf8");

  const claudeConfigDir = path.join(tmpRoot, "claude-config");
  fs.mkdirSync(claudeConfigDir, { recursive: true });
  const ccrConfigPath = path.join(tmpRoot, "ccr-config.synthetic.json");
  fs.writeFileSync(ccrConfigPath, JSON.stringify({
    transformers: [
      {
        name: "tuomin",
        path: TRANSFORMER_PATH,
        options: {
          baseUrl: "FILLED_AT_RUNTIME",
          appId: "isolated_claude_validation",
          failOnBlocked: true,
          failOnUnknownPlaceholders: true,
          requestTimeoutMs: 5000,
        },
      },
    ],
    Providers: [
      {
        name: "fake-upstream",
        api_base_url: `${fakeUpstreamUrl}/v1/messages`,
        api_key: "fake-key",
        transformer: { use: ["tuomin"] },
      },
    ],
    Router: { default: "fake-upstream,fake-claude-model" },
  }, null, 2), "utf8");

  return { dictionaryPath, configPath, claudeConfigDir, ccrConfigPath };
}

async function startGateway(tmpRoot, configPath, port) {
  const localPython = path.join(ROOT, ".venv", process.platform === "win32" ? "Scripts/python.exe" : "bin/python");
  const python = process.env.PYTHON || (fs.existsSync(localPython) ? localPython : "python");
  const env = {
    ...process.env,
    PYTHONPATH: process.env.PYTHONPATH ? `${path.join(ROOT, "src")}${path.delimiter}${process.env.PYTHONPATH}` : path.join(ROOT, "src"),
    TUOMIN_CONFIG: configPath,
    TUOMIN_DATA_DIR: tmpRoot,
    TUOMIN_WARM_NER: "0",
    HF_HUB_OFFLINE: "1",
    TRANSFORMERS_OFFLINE: "1",
    TUOMIN_MAPPING_DIR: path.join(tmpRoot, "mappings"),
    TUOMIN_ALLOW_PLAINTEXT_STORE: "1",
    TUOMIN_ADMIN_TOKEN: "synthetic-local-token",
  };
  const child = spawn(python, ["-m", "tuomin_gateway.cli", "serve", "--host", "127.0.0.1", "--port", String(port)], {
    cwd: ROOT,
    env,
    stdio: ["ignore", "pipe", "pipe"],
  });
  let output = "";
  child.stdout.on("data", (chunk) => { output += chunk.toString(); });
  child.stderr.on("data", (chunk) => { output += chunk.toString(); });
  child.on("error", (error) => { output += `spawn error: ${error.message}\n`; });
  try {
    await waitForHealth(`http://127.0.0.1:${port}/healthz`, () => output);
  } catch (error) {
    child.kill();
    throw error;
  }
  return { child, output: () => output };
}

async function waitForHealth(url, output, timeoutMs = 20000) {
  const started = Date.now();
  let lastError;
  while (Date.now() - started < timeoutMs) {
    try {
      const response = await fetch(url);
      if (response.ok) {
        return;
      }
      lastError = new Error(`health returned ${response.status}`);
    } catch (error) {
      lastError = error;
    }
    await new Promise((resolve) => setTimeout(resolve, 250));
  }
  throw new Error(`gateway did not become healthy: ${lastError?.message || "timeout"}\n${output()}`);
}

async function postJson(url, payload) {
  const response = await fetch(url, {
    method: "POST",
    headers: { "content-type": "application/json", "x-api-key": "fake-key" },
    body: JSON.stringify(payload),
  });
  const text = await response.text();
  return { response, text, payload: text ? JSON.parse(text) : {} };
}

async function runValidation() {
  const tmpRoot = fs.mkdtempSync(path.join(os.tmpdir(), "tuomin-isolated-claude-"));
  const snapshotDir = path.join(tmpRoot, "outbound-snapshots");
  const fakeUpstream = await startFakeUpstream();
  const paths = writeSyntheticConfig(tmpRoot, fakeUpstream.baseUrl);
  const gatewayPort = await freePort();
  const unavailablePort = await freePort();
  let gateway;
  try {
    gateway = await startGateway(tmpRoot, paths.configPath, gatewayPort);
    const gatewayUrl = `http://127.0.0.1:${gatewayPort}`;
    const transformerOptions = {
      baseUrl: gatewayUrl,
      appId: "isolated_claude_validation",
      requestTimeoutMs: 5000,
      outboundSnapshotDir: snapshotDir,
      failOnBlocked: true,
      failOnUnknownPlaceholders: true,
    };

    const direct = await postJson(`${fakeUpstream.baseUrl}/v1/messages`, requestBody());
    assert.equal(direct.response.status, 200, direct.text);
    const directSeen = fakeUpstream.seen.at(-1).payload;
    assert.equal(containsRaw(directSeen), true);

    fakeUpstream.reset();
    const transformer = new TuominTransformer(transformerOptions);
    const masked = await transformer.transformRequestIn(requestBody());
    assert.equal(containsRaw(masked), false);
    assert.deepEqual(seenPlaceholders(masked).sort(), EXPECTED_PLACEHOLDERS.slice().sort());
    const upstreamResponse = await postJson(`${fakeUpstream.baseUrl}/v1/messages`, masked);
    assert.equal(upstreamResponse.response.status, 200, upstreamResponse.text);
    const upstreamSeen = fakeUpstream.seen.at(-1).payload;
    assert.equal(containsRaw(upstreamSeen), false);
    assert.deepEqual(seenPlaceholders(upstreamSeen).sort(), EXPECTED_PLACEHOLDERS.slice().sort());
    const refilled = await transformer.transformResponseOut(new Response(upstreamResponse.text, {
      status: upstreamResponse.response.status,
      headers: { "content-type": "application/json" },
    }));
    const refilledPayload = await refilled.json();
    assert.equal(containsRaw(refilledPayload), true);
    assert.equal(seenPlaceholders(refilledPayload).length, 0);

    const auditResponse = await fetch(`${gatewayUrl}/redact`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ app_id: "isolated_claude_validation", text: syntheticPrompt(), use_ner: false }),
    });
    const auditPayload = await auditResponse.json();
    assert.equal(auditResponse.status, 200, JSON.stringify(auditPayload));
    assert.equal(containsRaw(auditPayload), false);

    const snapshotFiles = fs.existsSync(snapshotDir) ? fs.readdirSync(snapshotDir).filter((name) => name.endsWith(".json")) : [];
    assert.ok(snapshotFiles.length >= 1);
    const snapshotPayloads = snapshotFiles.map((name) => JSON.parse(fs.readFileSync(path.join(snapshotDir, name), "utf8")));
    assert.equal(containsRaw(snapshotPayloads), false);

    const failureResults = {};
    for (const mode of ["unknown", "altered", "missing"]) {
      fakeUpstream.reset();
      fakeUpstream.setMode(mode);
      const branchTransformer = new TuominTransformer({ ...transformerOptions, outboundSnapshotDir: null });
      const branchMasked = await branchTransformer.transformRequestIn(requestBody());
      const branchResponse = await postJson(`${fakeUpstream.baseUrl}/v1/messages`, branchMasked);
      try {
        await branchTransformer.transformResponseOut(new Response(branchResponse.text, {
          status: branchResponse.response.status,
          headers: { "content-type": "application/json" },
        }));
        failureResults[mode] = { blocked: false };
      } catch (error) {
        failureResults[mode] = { blocked: error.code === "TUOMIN_FAIL_CLOSED", code: error.code };
      }
      assert.equal(failureResults[mode].blocked, true, `${mode} must fail closed`);
      assert.equal(containsRaw(fakeUpstream.seen.at(-1).payload), false, `${mode} upstream payload must stay masked`);
    }

    fakeUpstream.reset();
    const unavailableTransformer = new TuominTransformer({
      baseUrl: `http://127.0.0.1:${unavailablePort}`,
      appId: "isolated_claude_validation",
      requestTimeoutMs: 200,
    });
    let unavailableBlocked = false;
    try {
      await unavailableTransformer.transformRequestIn(requestBody());
    } catch (error) {
      unavailableBlocked = error.code === "TUOMIN_FAIL_CLOSED";
    }
    assert.equal(unavailableBlocked, true);
    assert.equal(fakeUpstream.seen.length, 0);

    const summary = {
      status: "ok",
      validation: "isolated_claude_ccr_to_tuomin_gateway_fake_upstream",
      synthetic_only: true,
      live_upstream_used: false,
      downloads_or_installs: false,
      touched_global_config: false,
      read_real_materials: false,
      claude_config_dir: "temporary",
      ccr_config: "temporary_fake_provider",
      gateway: { started: true, host: "127.0.0.1", port: gatewayPort },
      fake_upstream: { host: "127.0.0.1", port: new URL(fakeUpstream.baseUrl).port },
      ab: {
        direct_without_tuomin: {
          upstream_saw_raw_labels: seenRawLabels(directSeen),
          upstream_saw_placeholders: seenPlaceholders(directSeen),
        },
        via_tuomin_ccr_transformer: {
          upstream_saw_raw_labels: seenRawLabels(upstreamSeen),
          upstream_saw_placeholders: seenPlaceholders(upstreamSeen),
          response_refilled_raw_label_count: seenRawLabels(refilledPayload).length,
        },
      },
      checks: {
        outbound_masked_before_upstream: !containsRaw(upstreamSeen),
        refill_success: containsRaw(refilledPayload) && seenPlaceholders(refilledPayload).length === 0,
        audit_safe: !containsRaw(auditPayload),
        snapshot_safe: !containsRaw(snapshotPayloads),
        unknown_placeholder_blocked: failureResults.unknown.blocked,
        altered_placeholder_blocked: failureResults.altered.blocked,
        missing_placeholder_blocked: failureResults.missing.blocked,
        gateway_unavailable_fail_closed: unavailableBlocked,
        fake_upstream_not_called_when_gateway_unavailable: fakeUpstream.seen.length === 0,
      },
      artifacts: {
        temp_root_removed_on_success: true,
        snapshot_file_count: snapshotFiles.length,
      },
    };
    assertSafeSummary(summary);
    return summary;
  } finally {
    if (gateway?.child && !gateway.child.killed) {
      gateway.child.kill();
      await new Promise((resolve) => gateway.child.once("exit", resolve));
    }
    await closeServer(fakeUpstream.server);
    fs.rmSync(tmpRoot, {
      recursive: true,
      force: true,
      maxRetries: 8,
      retryDelay: 100,
    });
  }
}

async function main() {
  const summary = await runValidation();
  console.log(JSON.stringify(summary, null, 2));
}

if (require.main === module) {
  main().catch((error) => {
    const safe = { status: "error", message: redactRawValues(error.message || error) };
    console.error(JSON.stringify(safe, null, 2));
    process.exitCode = 1;
  });
}

module.exports = { RAW, EXPECTED_PLACEHOLDERS, assertSafeSummary, containsRaw, redactRawValues, runValidation, seenPlaceholders, seenRawLabels };
