from tuomin_gateway.policy import (
    LEGACY_MAPPING_SCOPE_MODES,
    MAPPING_SCOPE_MODES,
    DetectionTier,
    POLICY_SCHEMA_VERSION,
    MappingScope,
    PolicyDimensions,
    dimensions_from_profile,
    policy_compatibility_metadata,
    profile_from_dimensions,
)
from tuomin_gateway.profiles import get_profile, profile_for_scenario


def test_profile_round_trips_through_five_policy_dimensions():
    profile = profile_for_scenario("contract")

    dimensions = dimensions_from_profile(profile)
    restored = profile_from_dimensions(dimensions)

    assert restored == profile
    assert dimensions.detection.ner_required is True
    assert dimensions.mapping.mode == "document"
    assert dimensions.refill.strict is True


def test_policy_compatibility_metadata_is_explicit_and_safe():
    metadata = policy_compatibility_metadata(get_profile("agent"))

    assert metadata["schema_version"] == POLICY_SCHEMA_VERSION
    assert metadata["compatibility"] == "legacy-profile-supported"
    assert metadata["legacy_profile"] == "agent"
    assert set(metadata["dimensions"]) == {
        "schema_version", "name", "detection", "scenario", "mapping", "refill", "guard"
    }


def test_mapping_scope_rejects_unknown_mode():
    try:
        MappingScope("global")
    except ValueError as exc:
        assert "document" in str(exc) and "namespace" in str(exc)
    else:  # pragma: no cover - defensive
        raise AssertionError("unknown mapping scope was accepted")


def test_mapping_scope_covers_namespace_and_legacy_modes():
    assert MAPPING_SCOPE_MODES == frozenset({"document", "session", "namespace"})
    assert LEGACY_MAPPING_SCOPE_MODES == frozenset({"document", "session"})
    assert MappingScope("namespace").to_safe_dict() == {"mode": "namespace"}


def test_legacy_profile_cannot_express_namespace_mapping_scope():
    dimensions = dimensions_from_profile(get_profile("strict"))
    namespace_dimensions = PolicyDimensions(
        **{**dimensions.__dict__, "mapping": MappingScope("namespace")}
    )
    try:
        profile_from_dimensions(namespace_dimensions)
    except ValueError as exc:
        assert "namespace" in str(exc)
    else:  # pragma: no cover - defensive
        raise AssertionError("namespace mapping scope silently collapsed to legacy")


def test_required_ner_cannot_be_disabled_in_dimension_model():
    try:
        DetectionTier(use_ner=False, ner_required=True)
    except ValueError as exc:
        assert "must also be enabled" in str(exc)
    else:  # pragma: no cover - defensive
        raise AssertionError("incoherent NER policy was accepted")
