"""Bare secret-format rules (2026.09.11): redact paths must mask credential
tokens even without an ``api_key=`` style cue. All values are synthetic
docs-example shapes, never real credentials."""

from tuomin_gateway.detectors.rules import RuleDetector


def _values(text: str) -> list[str]:
    return [text[span.start : span.end] for span in RuleDetector().detect(text)]


def test_bare_aws_access_key_masked_even_glued_to_cjk():
    text = "备份密钥AKIAIOSFODNN7EXAMPLE请处理。"
    assert "AKIAIOSFODNN7EXAMPLE" in _values(text)


def test_bare_github_token_masked():
    text = "token ghp_0123456789abcdefghijklmnopqrstuvwxyzABCD 已泄露"
    assert any(v.startswith("ghp_") for v in _values(text))


def test_bare_slack_token_masked():
    # Construct a deterministic fake; never store a credential-shaped literal.
    alphabet = "".join(chr(n) for n in range(97, 113))
    text = "xoxb-" + str(1234567890) + "-" + alphabet + " in chat"
    assert any(v.startswith("xoxb-") for v in _values(text))


def test_bare_stripe_live_key_masked():
    digits = "".join(str(n) for n in range(10))
    alphabet = "".join(chr(n) for n in range(97, 113))
    text = "key " + "sk_live_" + digits + alphabet
    assert any("sk_live_" in v for v in _values(text))


def test_bare_openai_style_key_masked():
    text = "sk-proj-0123456789abcdefghijklmnopqrstuv"
    assert any(v.startswith("sk-") for v in _values(text))


def test_bare_google_api_key_masked():
    # Real Google keys are AIza + exactly 35 chars; trailing CJK must match.
    text = "密钥AIzaSy0123456789abcdefghijklmnopqrstuvw。"
    assert any(v.startswith("AIza") for v in _values(text))


def test_private_key_block_header_masked():
    text = "-----BEGIN RSA PRIVATE KEY-----\nMIIB\n-----END RSA PRIVATE KEY-----"
    assert any("BEGIN RSA PRIVATE KEY" in v for v in _values(text))


def test_bare_jwt_masked():
    text = (
        "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
        "eyJzdWIiOiIxMjM0NTY3ODkwIn0."
        "SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
    )
    assert any(v.startswith("eyJ") for v in _values(text))


def test_near_misses_not_masked():
    text = (
        "AKIA12345（仅15位主体不足）"  # AKIA + 6 chars, too short
        " ghx_0123456789abcdefghijklmnopqrstuvwxyzABCD"  # invalid gh prefix
        " sk-short"  # too short after sk-
        " eyJhbGciOiJIUzI1NiJ9.missing"  # not three JWT segments
    )
    assert _values(text) == []


def test_secret_inside_longer_alphanumeric_run_not_matched():
    # Order-id-like run embedding a key shape must not false-positive.
    assert _values("订单号ORD-AKIAIOSFODNN7EXAMPLE99") == []
