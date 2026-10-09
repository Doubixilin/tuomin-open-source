import json

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from tuomin_gateway.service.app import create_app  # noqa: E402
from tuomin_gateway.service.registry import AppRegistry, RegistryConfigurationError  # noqa: E402
from tuomin_gateway.store import MappingStore  # noqa: E402


def _dict_file(tmp_path):
    entries = [
        {"entry_id": "o1", "label": "ORG", "canonical_value": "绿洲集团",
         "aliases": [], "risk_level": "high", "status": "active", "version": "t"},
    ]
    path = tmp_path / "dict.json"
    path.write_text(json.dumps(entries, ensure_ascii=False), encoding="utf-8")
    return path


def _client(tmp_path):
    dpath = _dict_file(tmp_path)
    registry = AppRegistry({
        "kbapp": {"profile": "kb", "dictionary": str(dpath)},
        "cr": {
            "profile": {
                "base": "contract_review",
                "name": "contract_review_test",
                "use_ner": False,
            },
            "dictionary": str(dpath),
        },
        "strict_no_ner": {"profile": {"base": "strict", "name": "strict_no_ner", "use_ner": False}, "dictionary": str(dpath)},
    })
    store = MappingStore(tmp_path / "maps", ttl_seconds=3600)
    return TestClient(create_app(registry=registry, store=store, session_ttl=3600))


def test_healthz(tmp_path):
    r = _client(tmp_path).get("/healthz")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def _seed_file(tmp_path):
    entries = [
        {"entry_id": "c1", "label": "COURT", "canonical_value": "广州市中级人民法院",
         "aliases": [], "risk_level": "low", "status": "active", "version": "t"},
    ]
    path = tmp_path / "seed.json"
    path.write_text(json.dumps(entries, ensure_ascii=False), encoding="utf-8")
    return path


def test_extra_dictionaries_merge_into_app_dictionary(tmp_path):
    registry = AppRegistry({
        "app": {
            "profile": "kb",
            "dictionary": str(_dict_file(tmp_path)),
            "extra_dictionaries": [str(_seed_file(tmp_path))],
        }
    })

    values = {entry["canonical_value"] for entry in registry.resolve_dictionary("app")}

    assert {"绿洲集团", "广州市中级人民法院"} <= values


def test_extra_dictionaries_change_invalidates_cache(tmp_path):
    seed = _seed_file(tmp_path)
    registry = AppRegistry({
        "app": {
            "profile": "kb",
            "dictionary": str(_dict_file(tmp_path)),
            "extra_dictionaries": [str(seed)],
        }
    })
    first_version = registry.dictionary_version("app")

    seed.write_text(json.dumps([
        {"entry_id": "c2", "label": "ARBITRATION", "canonical_value": "广州仲裁委员会",
         "aliases": [], "risk_level": "low", "status": "active", "version": "t2"},
    ], ensure_ascii=False), encoding="utf-8")

    values = {entry["canonical_value"] for entry in registry.resolve_dictionary("app")}
    assert "广州仲裁委员会" in values
    assert registry.dictionary_version("app") != first_version


def test_extra_dictionaries_fail_closed_when_missing(tmp_path):
    registry = AppRegistry({
        "app": {
            "profile": "kb",
            "dictionary": str(_dict_file(tmp_path)),
            "extra_dictionaries": [str(tmp_path / "missing.json")],
        }
    })

    with pytest.raises(RegistryConfigurationError):
        registry.resolve_dictionary("app")


def test_extra_dictionaries_malformed_declaration_fails_closed(tmp_path):
    registry = AppRegistry({
        "app": {
            "profile": "kb",
            "dictionary": str(_dict_file(tmp_path)),
            "extra_dictionaries": "seed.json",
        }
    })

    with pytest.raises(RegistryConfigurationError):
        registry.resolve_dictionary("app")


def test_redact_then_refill_round_trip(tmp_path):
    client = _client(tmp_path)
    text = "绿洲集团签了合同，电话13800138000。"
    red = client.post("/redact", json={"app_id": "kbapp", "text": text}).json()
    assert "<ORG_001>" in red["redacted_text"]
    assert "绿洲集团" not in red["redacted_text"]
    assert "13800138000" not in red["redacted_text"]
    assert red["policy"]["schema_version"] == "policy-dimensions-v1"
    assert red["policy"]["compatibility"] == "legacy-profile-supported"

    back = client.post("/refill", json={"task_id": red["task_id"], "text": red["redacted_text"]}).json()
    assert back["status"] == "ok"
    assert back["text"] == text


def test_refill_unknown_task_is_404(tmp_path):
    client = _client(tmp_path)
    r = client.post("/refill", json={"task_id": "missing", "text": "<ORG_001>"})
    assert r.status_code == 404


def test_contract_review_keeps_amount_in_clear(tmp_path):
    client = _client(tmp_path)
    text = "绿洲集团报价人民币5,000.00元。"
    # The registry owns the test-only NER-off policy; callers cannot downgrade
    # this protection per request.
    red = client.post("/redact", json={"app_id": "cr", "text": text}).json()
    assert "<ORG_001>" in red["redacted_text"]
    assert "人民币5,000.00元" in red["redacted_text"]  # AMOUNT passes through for analysis


def test_session_placeholders_stable_across_calls(tmp_path):
    client = _client(tmp_path)
    sid = client.post("/session/open", json={"app_id": "kbapp"}).json()["session_id"]
    m1 = client.post(f"/session/{sid}/mask", json={"text": "绿洲集团 A"}).json()["masked"]
    m2 = client.post(f"/session/{sid}/mask", json={"text": "再提 绿洲集团 B"}).json()["masked"]
    assert "<ORG_001>" in m1 and "<ORG_001>" in m2  # same entity -> same placeholder

    refilled = client.post(f"/session/{sid}/refill", json={"text": "结论涉及 <ORG_001>"}).json()
    assert refilled["text"] == "结论涉及 绿洲集团"
    assert refilled["unknown_placeholders"] == []

    assert client.delete(f"/session/{sid}").json()["closed"] is True


def test_session_refill_blocks_altered_placeholder(tmp_path):
    client = _client(tmp_path)
    sid = client.post("/session/open", json={"app_id": "kbapp"}).json()["session_id"]
    client.post(f"/session/{sid}/mask", json={"text": "结论涉及绿洲集团"})

    refilled = client.post(f"/session/{sid}/refill", json={"text": "结论涉及 <ORG-001>"}).json()

    assert refilled["status"] == "blocked"
    assert "altered_placeholder" in refilled["error_types"]
    assert refilled["text"] is None


def test_session_refill_blocks_lenient_altered_variants(tmp_path):
    client = _client(tmp_path)
    sid = client.post("/session/open", json={"app_id": "kbapp"}).json()["session_id"]
    client.post(f"/session/{sid}/mask", json={"text": "结论涉及绿洲集团"})

    for variant in ["<org_001>", "< ORG_001>", "<ORG_ 001>", "<ORG001>"]:
        refilled = client.post(f"/session/{sid}/refill", json={"text": f"结论涉及 {variant}"}).json()
        assert refilled["status"] == "blocked", variant
        assert "altered_placeholder" in refilled["error_types"], variant
        assert refilled["text"] is None, variant


def test_session_refill_strict_profile_blocks_missing_placeholder(tmp_path):
    client = _client(tmp_path)
    sid = client.post("/session/open", json={"app_id": "strict_no_ner"}).json()["session_id"]
    client.post(f"/session/{sid}/mask", json={"text": "结论涉及绿洲集团，电话13900001111"})

    refilled = client.post(f"/session/{sid}/refill", json={"text": "结论只涉及 <ORG_001>"}).json()

    assert refilled["status"] == "blocked"
    assert "missing_placeholder" in refilled["error_types"]
    assert "<CONTACT_001>" in refilled["missing_placeholders"]


def test_session_unknown_returns_404(tmp_path):
    client = _client(tmp_path)
    r = client.post("/session/nope/mask", json={"text": "x"})
    assert r.status_code == 404


def test_legacy_redact_unknown_profile_is_safe_400(tmp_path):
    client = _client(tmp_path)
    r = client.post(
        "/redact",
        json={"app_id": "kbapp", "profile": "does_not_exist", "text": "绿洲集团"},
    )
    assert r.status_code == 400


def test_legacy_redact_rejects_literal_placeholder(tmp_path):
    client = _client(tmp_path)
    r = client.post("/redact", json={"app_id": "kbapp", "text": "伪造 <ORG_001> 内容"})
    assert r.status_code == 409
    assert r.headers["deprecation"] == "true"  # legacy contract headers preserved


def test_legacy_session_mask_rejects_literal_placeholder(tmp_path):
    client = _client(tmp_path)
    sid = client.post("/session/open", json={"app_id": "kbapp"}).json()["session_id"]
    r = client.post(f"/session/{sid}/mask", json={"text": "伪造 <ORG_001> 内容"})
    assert r.status_code == 409
    assert r.headers["deprecation"] == "true"
