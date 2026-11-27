#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PORT="${CCR_SMOKE_PORT:-3457}"
TMP_HOME="$(mktemp -d)"
TMP_HOME_WIN="$TMP_HOME"
if command -v cygpath >/dev/null 2>&1; then
  TMP_HOME_WIN="$(cygpath -w "$TMP_HOME")"
fi
LOG_FILE="$TMP_HOME/ccr-smoke.log"
CCR_PID=""

cleanup() {
  if [[ -n "$CCR_PID" ]] && kill -0 "$CCR_PID" 2>/dev/null; then
    kill "$CCR_PID" 2>/dev/null || true
    wait "$CCR_PID" 2>/dev/null || true
  fi
  rm -rf "$TMP_HOME"
}
trap cleanup EXIT

mkdir -p "$TMP_HOME/.claude-code-router"
cat > "$TMP_HOME/.claude-code-router/config.json" <<JSON
{
  "PORT": ${PORT},
  "Providers": [],
  "Router": {},
  "transformers": [
    {
      "name": "tuomin",
      "path": "${ROOT_DIR}/integrations/ccr/tuomin-transformer.cjs",
      "options": {
        "baseUrl": "http://127.0.0.1:8765",
        "appId": "agent",
        "failOnBlocked": true,
        "failOnUnknownPlaceholders": true
      }
    }
  ]
}
JSON

HOME="$TMP_HOME" USERPROFILE="$TMP_HOME_WIN" npx -y @musistudio/claude-code-router@2.0.0 start >"$LOG_FILE" 2>&1 &
CCR_PID="$!"

for _ in $(seq 1 30); do
  if grep -q "Loaded JSON config" "$LOG_FILE" 2>/dev/null; then
    echo "{\"status\":\"ok\",\"checked\":\"ccr_start_with_temp_home\",\"port\":${PORT}}"
    exit 0
  fi
  if ! kill -0 "$CCR_PID" 2>/dev/null; then
    cat "$LOG_FILE" >&2
    exit 1
  fi
  sleep 1
done

cat "$LOG_FILE" >&2
echo "{\"status\":\"error\",\"message\":\"CCR did not report config load before timeout\"}" >&2
exit 1
