"""Phase 2: declared sensitivity tiers map to genuinely different masking depth."""
import pytest

from tuomin_gateway.profiles import apply_profile, profile_for_sensitivity
from tuomin_gateway.schemas import DetectionSpan
from tuomin_gateway.service.registry import AppRegistry, ProfileOverrideDenied


def _span(label, risk="high", start=0, end=3):
    return DetectionSpan(start=start, end=end, label=label, confidence=0.9,
                         source="rule", detector_version="t", text_hash="x", risk_level=risk)


def _kept_labels(profile, spans):
    kept, _ = apply_profile(spans, profile)
    return {s.label for s in kept}


SPANS = [
    _span("CONTACT", start=0, end=3),
    _span("BANK_ACCOUNT", start=4, end=7),
    _span("AMOUNT", risk="medium", start=8, end=11),
    _span("DATE", risk="medium", start=12, end=15),
    _span("PERSON", start=16, end=19),
    _span("ORG", start=20, end=23),
    _span("CREDENTIAL", risk="critical", start=24, end=27),
]


def test_light_redacts_only_formatted_sensitive():
    p = profile_for_sensitivity("light")
    kept = _kept_labels(p, SPANS)
    assert kept == {"CONTACT", "BANK_ACCOUNT", "CREDENTIAL"}  # names/orgs/amounts/dates pass
    assert p.use_ner is False


def test_standard_redacts_identity_but_passes_amount_date():
    p = profile_for_sensitivity("standard")
    kept = _kept_labels(p, SPANS)
    assert "PERSON" in kept and "ORG" in kept and "CONTACT" in kept
    assert "AMOUNT" not in kept and "DATE" not in kept  # passed for the reviewer LLM
    assert p.use_ner is True


def test_strict_redacts_everything_including_amount_date():
    p = profile_for_sensitivity("strict")
    kept = _kept_labels(p, SPANS)
    assert "AMOUNT" in kept and "DATE" in kept and "PERSON" in kept


def test_ultra_hard_blocks_credential():
    p = profile_for_sensitivity("ultra")
    kept, blocked = apply_profile(SPANS, p)
    assert "CREDENTIAL" in blocked          # flagged must-not-reach-cloud
    assert "CREDENTIAL" in {s.label for s in kept}  # still redacted, never left clear
    assert p.block_min_severity == "critical"  # proxy will fail-closed


def test_depth_is_monotonic_light_to_strict():
    light = len(_kept_labels(profile_for_sensitivity("light"), SPANS))
    standard = len(_kept_labels(profile_for_sensitivity("standard"), SPANS))
    strict = len(_kept_labels(profile_for_sensitivity("strict"), SPANS))
    assert light < standard < strict


def test_registry_resolves_sensitivity_declaration():
    reg = AppRegistry({"myapp": {"sensitivity": "light"}})
    assert reg.resolve_profile("myapp").name == "light"
    # Unknown apps fall back to strict; request data cannot replace an app's
    # server-authorized minimum profile.
    assert reg.resolve_profile("nope").name == "strict"
    with pytest.raises(ProfileOverrideDenied):
        reg.resolve_profile("myapp", "standard")


def test_inline_profile_can_base_on_a_sensitivity_tier():
    # regression: inline spec base may be a sensitivity tier, not only a preset
    reg = AppRegistry({"a": {"profile": {"base": "standard", "name": "a", "use_ner": False}}})
    p = reg.resolve_profile("a")
    assert p.name == "a"
    assert p.use_ner is False
    assert "AMOUNT" in p.deny_labels  # inherited from the standard tier


def test_same_named_request_cannot_strip_inline_app_policy():
    reg = AppRegistry(
        {
            "a": {
                "profile": {
                    "base": "strict",
                    "name": "strict",
                    "action": {"CREDENTIAL": "block"},
                }
            }
        }
    )

    resolved = reg.resolve_profile("a", "strict")

    assert resolved.action["CREDENTIAL"] == "block"
