#!/usr/bin/env node
"use strict";

const fs = require("node:fs");
const net = require("node:net");
const os = require("node:os");
const path = require("node:path");
const { spawn, spawnSync } = require("node:child_process");

const ROOT = path.resolve(__dirname, "..");
const PORT = Number(process.env.CCR_SMOKE_PORT || 3457);
const CCR_BIN = process.platform === "win32" ? "npx.cmd" : "npx";
const CCR_ARGS = ["-y", "@musistudio/claude-code-router@2.0.0"];

function waitForOutput(child, matcher, timeoutMs = 30000) {
  return new Promise((resolve, reject) => {
    let output = "";
    const timer = setTimeout(() => reject(new Error("timeout waiting for " + matcher + "; output:\n" + output)), timeoutMs);
    const onData = (chunk) => {
      output += chunk.toString();
      if (output.includes(matcher) || output.includes("server is running")) {
        clearTimeout(timer);
        resolve(output);
      }
    };
    child.stdout.on("data", onData);
    child.stderr.on("data", onData);
    child.on("error", (error) => {
      clearTimeout(timer);
      reject(error);
    });
    child.on("exit", (code) => {
      if (output.includes(matcher) || (code === 0 && output.includes("server is running"))) {
        clearTimeout(timer);
        resolve(output);
        return;
      }
      clearTimeout(timer);
      reject(new Error("ccr exited early with code " + code + "; output:\n" + output));
    });
  });
}

function waitForPort(port, timeoutMs = 15000) {
  const started = Date.now();
  return new Promise((resolve, reject) => {
    const tryConnect = () => {
      const socket = net.createConnection({ host: "127.0.0.1", port });
      socket.once("connect", () => {
        socket.destroy();
        resolve();
      });
      socket.once("error", (error) => {
        socket.destroy();
        if (Date.now() - started >= timeoutMs) {
          reject(error);
          return;
        }
        setTimeout(tryConnect, 300);
      });
    };
    tryConnect();
  });
}

async function main() {
  const tmpHome = fs.mkdtempSync(path.join(os.tmpdir(), "tuomin-ccr-smoke-"));
  const ccrEnv = { ...process.env, HOME: tmpHome, USERPROFILE: tmpHome };
  const ccrProcess = process.platform === "win32" ? (process.env.ComSpec || "cmd.exe") : CCR_BIN;
  const startArgs = process.platform === "win32"
    ? ["/d", "/s", "/c", CCR_BIN, ...CCR_ARGS, "start"]
    : [...CCR_ARGS, "start"];
  const stopArgs = process.platform === "win32"
    ? ["/d", "/s", "/c", CCR_BIN, ...CCR_ARGS, "stop"]
    : [...CCR_ARGS, "stop"];
  let ccr;
  try {
    fs.mkdirSync(path.join(tmpHome, ".claude-code-router"), { recursive: true });
    fs.writeFileSync(path.join(tmpHome, ".claude-code-router", "config.json"), JSON.stringify({
      PORT,
      Providers: [],
      Router: {},
      transformers: [
        {
          name: "tuomin",
          path: path.join(ROOT, "integrations/ccr/tuomin-transformer.cjs"),
          options: {
            baseUrl: "http://127.0.0.1:8765",
            appId: "agent",
            failOnBlocked: true,
            failOnUnknownPlaceholders: true,
          },
        },
      ],
    }, null, 2));

    ccr = spawn(ccrProcess, startArgs, { env: ccrEnv, stdio: ["ignore", "pipe", "pipe"] });
    await waitForOutput(ccr, "Loaded JSON config");
    await waitForPort(PORT);
    console.log(JSON.stringify({ status: "ok", checked: "ccr_start_with_temp_home", port: PORT }));
  } finally {
    if (ccr && !ccr.killed) {
      ccr.kill();
    }
    spawnSync(ccrProcess, stopArgs, { env: ccrEnv, stdio: "ignore" });
    fs.rmSync(tmpHome, { recursive: true, force: true });
  }
}

main().catch((error) => {
  console.error(JSON.stringify({ status: "error", message: error.message }, null, 2));
  process.exitCode = 1;
});
