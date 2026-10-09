"""licensing 核心包单元测试：验签、状态机、持久化、签发脚本闭环。"""

from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    PublicFormat,
)

from tuomin_gateway.licensing.core import (
    GRACE_DAYS,
    LicenseFormatError,
    LicenseProductError,
    LicenseSignatureError,
    LicenseState,
    compute_state,
    decode_license_code,
    new_tasks_allowed,
)
from tests.permission_assertions import assert_owner_private
from tuomin_gateway.licensing.state import LicenseStore

REPO_ROOT = Path(__file__).resolve().parents[1]
SIGN_SCRIPT = REPO_ROOT / "scripts" / "sign_license.py"

NOW = datetime(2026, 8, 24, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture()
def keypair():
    private = Ed25519PrivateKey.generate()
    public_hex = private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw).hex()
    return private, public_hex


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def make_code(private: Ed25519PrivateKey, **overrides) -> str:
    payload = {
        "v": 1,
        "license_id": "lic_test01",
        "customer": "测试单位",
        "product": "tuomin-workbench",
        "issued_at": NOW.isoformat().replace("+00:00", "Z"),
        "expires_at": (NOW + timedelta(days=90)).isoformat().replace("+00:00", "Z"),
    }
    payload.update(overrides)
    payload_bytes = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    signature = private.sign(payload_bytes)
    return f"TM1.{_b64url(payload_bytes)}.{_b64url(signature)}"


# --- 验签 ------------------------------------------------------------------


def test_roundtrip_decode(keypair):
    private, public_hex = keypair
    payload = decode_license_code(make_code(private), public_key_hex=public_hex)
    assert payload.customer == "测试单位"
    assert payload.product == "tuomin-workbench"
    assert payload.expires_at == NOW + timedelta(days=90)


def test_tampered_payload_rejected(keypair):
    private, public_hex = keypair
    code = make_code(private)
    body = code[len("TM1.") :]
    payload_segment, signature_segment = body.split(".")
    # 篡改 payload 的一个字符（保持 base64 合法）
    flipped = ("A" if payload_segment[5] != "A" else "B") + payload_segment[1:]
    tampered_segment = payload_segment[:5] + flipped + payload_segment[6:]
    with pytest.raises(LicenseSignatureError):
        decode_license_code(f"TM1.{tampered_segment}.{signature_segment}", public_key_hex=public_hex)


def test_wrong_public_key_rejected(keypair):
    private, _ = keypair
    other_public = Ed25519PrivateKey.generate().public_key().public_bytes(Encoding.Raw, PublicFormat.Raw).hex()
    with pytest.raises(LicenseSignatureError):
        decode_license_code(make_code(private), public_key_hex=other_public)


def test_empty_public_key_rejected(keypair):
    private, _ = keypair
    with pytest.raises(LicenseSignatureError):
        decode_license_code(make_code(private), public_key_hex="")


@pytest.mark.parametrize(
    "code",
    [
        "",
        "TM2." + "x",
        "TM1.only-one-segment",
        "TM1.a.b.c",
        "TM1..sig",
        "TM1.@@@.sig",
    ],
)
def test_malformed_codes_rejected(code):
    public_hex = Ed25519PrivateKey.generate().public_key().public_bytes(Encoding.Raw, PublicFormat.Raw).hex()
    with pytest.raises(LicenseFormatError):
        decode_license_code(code, public_key_hex=public_hex)


@pytest.mark.parametrize(
    "code",
    [
        # 结构合法但签名必失败（验签先于 JSON 解析，不得先解析攻击者控制的数据）
        "TM1." + _b64url(b"not-json") + "." + _b64url(b"x" * 64),
        "TM1." + _b64url(b"[1,2]") + "." + _b64url(b"x" * 64),
    ],
)
def test_unsigned_payloads_rejected(code):
    public_hex = Ed25519PrivateKey.generate().public_key().public_bytes(Encoding.Raw, PublicFormat.Raw).hex()
    with pytest.raises(LicenseSignatureError):
        decode_license_code(code, public_key_hex=public_hex)


def test_bad_fields_rejected(keypair):
    private, public_hex = keypair
    for overrides in (
        {"v": 2},
        {"license_id": ""},
        {"customer": "  "},
        {"expires_at": NOW.isoformat().replace("+00:00", "Z")},  # expires <= issued
        {"issued_at": "not-a-time"},
        {"expires_at": "2026-01-01"},  # 无时区
    ):
        with pytest.raises(LicenseFormatError):
            decode_license_code(make_code(private, **overrides), public_key_hex=public_hex)


def test_wrong_product_rejected(keypair):
    private, public_hex = keypair
    with pytest.raises(LicenseProductError):
        decode_license_code(make_code(private, product="other-product"), public_key_hex=public_hex)


# --- 状态机 ------------------------------------------------------------------


def _payload(keypair):
    private, public_hex = keypair
    return decode_license_code(make_code(private), public_key_hex=public_hex)


def test_state_boundaries(keypair):
    payload = _payload(keypair)
    expires = payload.expires_at
    assert compute_state(payload, now=expires - timedelta(seconds=1)) is LicenseState.VALID
    assert compute_state(payload, now=expires) is LicenseState.VALID
    assert compute_state(payload, now=expires + timedelta(days=GRACE_DAYS - 1)) is LicenseState.GRACE
    assert compute_state(payload, now=expires + timedelta(days=GRACE_DAYS)) is LicenseState.GRACE
    assert compute_state(payload, now=expires + timedelta(days=GRACE_DAYS + 1)) is LicenseState.EXPIRED
    assert compute_state(None, now=NOW) is LicenseState.UNLICENSED


def test_clock_rollback_uses_max_seen(keypair):
    payload = _payload(keypair)
    expired_moment = payload.expires_at + timedelta(days=GRACE_DAYS + 10)
    # 时钟回拨到有效期内，但 max_seen 已在宽限期之后 → 仍然 EXPIRED
    assert compute_state(payload, now=NOW, max_seen=expired_moment) is LicenseState.EXPIRED


def test_new_tasks_allowed_matrix():
    assert new_tasks_allowed(LicenseState.VALID) is True
    assert new_tasks_allowed(LicenseState.GRACE) is True
    assert new_tasks_allowed(LicenseState.EXPIRED) is False
    assert new_tasks_allowed(LicenseState.UNLICENSED) is False


# --- 持久化 ------------------------------------------------------------------


def test_store_activate_and_load(keypair, tmp_path):
    private, public_hex = keypair
    store = LicenseStore(tmp_path)
    code = make_code(private)
    payload = store.activate(code, public_key_hex=public_hex)
    assert payload.customer == "测试单位"
    assert_owner_private(tmp_path / "license.json", is_dir=False)
    loaded = store.load(public_key_hex=public_hex)
    assert loaded is not None and loaded.license_id == payload.license_id


def test_store_activate_rejects_bad_signature(keypair, tmp_path):
    private, _ = keypair
    store = LicenseStore(tmp_path)
    other_public = Ed25519PrivateKey.generate().public_key().public_bytes(Encoding.Raw, PublicFormat.Raw).hex()
    with pytest.raises(LicenseSignatureError):
        store.activate(make_code(private), public_key_hex=other_public)
    assert not (tmp_path / "license.json").exists()


def test_store_load_never_raises(tmp_path):
    store = LicenseStore(tmp_path)
    assert store.load(public_key_hex="ab" * 32) is None
    (tmp_path / "license.json").write_text("corrupted{{{", encoding="utf-8")
    assert store.load(public_key_hex="ab" * 32) is None


def test_touch_clock_monotonic(tmp_path):
    store = LicenseStore(tmp_path)
    later = NOW + timedelta(days=30)
    assert store.touch_clock(later) == later
    # 回拨 now 不影响已记录的最大时间
    assert store.touch_clock(NOW) == later
    # 损坏的时钟文件按无历史处理
    (tmp_path / "license-clock.json").write_text("broken", encoding="utf-8")
    assert store.touch_clock(NOW) == NOW


# --- 签发脚本闭环 --------------------------------------------------------------


def test_sign_script_roundtrip(tmp_path, request):
    issuer_root = tmp_path / "issuer"
    if os.name == "nt":
        local_app_data = Path(
            os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")
        )
        system_temp = local_app_data / "Temp"
        system_temp.mkdir(parents=True, exist_ok=True)
        issuer_root = Path(
            tempfile.mkdtemp(prefix="tuomin-license-", dir=system_temp)
        )
        request.addfinalizer(
            lambda: shutil.rmtree(issuer_root, ignore_errors=True)
        )
    key_path = issuer_root / "key.pem"
    init = subprocess.run(
        [sys.executable, str(SIGN_SCRIPT), "--init-key", "--key-path", str(key_path)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=REPO_ROOT,
    )
    assert init.returncode == 0, init.stderr
    public_hex = init.stdout.strip()
    assert len(public_hex) == 64
    assert_owner_private(key_path, is_dir=False)

    sign = subprocess.run(
        [sys.executable, str(SIGN_SCRIPT), "--key-path", str(key_path),
         "--customer", "闭环测试单位", "--days", "45"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=REPO_ROOT,
    )
    assert sign.returncode == 0, sign.stderr
    code = sign.stdout.strip()
    payload = decode_license_code(code, public_key_hex=public_hex)
    assert payload.customer == "闭环测试单位"
    assert compute_state(payload, now=datetime.now(timezone.utc)) is LicenseState.VALID


def test_sign_script_refuses_repo_path():
    result = subprocess.run(
        [sys.executable, str(SIGN_SCRIPT), "--init-key", "--key-path", str(REPO_ROOT / "evil-key.pem")],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=REPO_ROOT,
    )
    assert result.returncode != 0
    assert not (REPO_ROOT / "evil-key.pem").exists()


def test_ledger_records_and_lists(tmp_path, request):
    issuer_root = tmp_path / "issuer"
    if os.name == "nt":
        local_app_data = Path(
            os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")
        )
        system_temp = local_app_data / "Temp"
        system_temp.mkdir(parents=True, exist_ok=True)
        issuer_root = Path(
            tempfile.mkdtemp(prefix="tuomin-ledger-", dir=system_temp)
        )
        request.addfinalizer(
            lambda: shutil.rmtree(issuer_root, ignore_errors=True)
        )
    key_path = issuer_root / "key.pem"
    subprocess.run(
        [sys.executable, str(SIGN_SCRIPT), "--init-key", "--key-path", str(key_path)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=REPO_ROOT,
        check=True,
    )
    for customer, note in (("甲单位", "张三测试"), ("乙单位", "")):
        result = subprocess.run(
            [sys.executable, str(SIGN_SCRIPT), "--key-path", str(key_path),
             "--customer", customer, "--days", "30", "--note", note],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=REPO_ROOT,
        )
        assert result.returncode == 0, result.stderr
    ledger = key_path.parent / "ledger.jsonl"
    entries = [json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert [e["customer"] for e in entries] == ["甲单位", "乙单位"]
    assert entries[0]["note"] == "张三测试" and entries[1]["note"] == ""
    assert all(e["license_id"].startswith("lic_") for e in entries)
    assert_owner_private(ledger, is_dir=False)

    listing = subprocess.run(
        [sys.executable, str(SIGN_SCRIPT), "--list", "--key-path", str(key_path)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=REPO_ROOT,
    )
    assert listing.returncode == 0
    assert "甲单位" in listing.stdout and "乙单位" in listing.stdout
    assert "有效" in listing.stdout
