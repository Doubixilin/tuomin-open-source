const { test } = require("node:test");
const assert = require("node:assert/strict");

const { assertSafeSummary, containsRaw, redactRawValues, runValidation } = require("../../scripts/validate_isolated_claude_gateway.cjs");

test("isolated Claude/CCR validation returns a safe passing summary", async () => {
  const summary = await runValidation();

  assert.equal(summary.status, "ok");
  assert.equal(summary.synthetic_only, true);
  assert.equal(summary.live_upstream_used, false);
  assert.equal(summary.touched_global_config, false);
  assert.equal(summary.ab.direct_without_tuomin.upstream_saw_raw_labels.length, 7);
  assert.deepEqual(summary.ab.via_tuomin_ccr_transformer.upstream_saw_raw_labels, []);
  assert.equal(summary.checks.outbound_masked_before_upstream, true);
  assert.equal(summary.checks.refill_success, true);
  assert.equal(summary.checks.audit_safe, true);
  assert.equal(summary.checks.snapshot_safe, true);
  assert.equal(summary.checks.unknown_placeholder_blocked, true);
  assert.equal(summary.checks.altered_placeholder_blocked, true);
  assert.equal(summary.checks.missing_placeholder_blocked, true);
  assert.equal(summary.checks.gateway_unavailable_fail_closed, true);
  assert.equal(summary.checks.fake_upstream_not_called_when_gateway_unavailable, true);
  assert.equal(containsRaw(summary), false);
  assertSafeSummary(summary);
});
test("isolated validation error redaction removes all synthetic raw values", () => {
  const unsafe = "Synthetic Person Alpha Project Starbridge HT-2026-TM-0001 SYNTH-BANK-778899 tm_synthetic_secret_2026_abcdef https://synthetic.example.invalid/system";
  const safe = redactRawValues(unsafe);

  assert.equal(containsRaw(safe), false);
  assert.match(safe, /\[redacted\]/);
});