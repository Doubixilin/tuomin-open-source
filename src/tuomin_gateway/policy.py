"""Five-dimensional policy model with legacy ``Profile`` compatibility.

``Profile`` remains accepted during the migration window.  This module makes
the independent policy dimensions explicit without forcing every existing
caller to migrate in one release.
"""
from __future__ import annotations

from dataclasses import dataclass

from tuomin_gateway.profiles import Profile


POLICY_SCHEMA_VERSION = "policy-dimensions-v1"
PROFILE_COMPATIBILITY = "legacy-profile-supported"

# Every mapping scope the gateway can mint grants under. ``namespace`` is the
# v1 capability-layer persistent scope; ``session`` is legacy-only (the old
# ``/session/*`` API) and is kept for read compatibility during migration.
MAPPING_SCOPE_MODES = frozenset({"document", "session", "namespace"})

# The scopes a legacy ``Profile`` can express. ``namespace`` mappings are bound
# to capabilities at the v1 API layer, never to a profile.
LEGACY_MAPPING_SCOPE_MODES = frozenset({"document", "session"})


@dataclass(frozen=True)
class DetectionTier:
    use_ner: bool
    ner_required: bool
    rules_required: bool = True
    declared_dictionary_required: bool = True

    def __post_init__(self) -> None:
        if self.ner_required and not self.use_ner:
            raise ValueError("required NER must also be enabled")

    def to_safe_dict(self) -> dict[str, object]:
        return {
            "use_ner": self.use_ner,
            "ner_required": self.ner_required,
            "rules_required": self.rules_required,
            "declared_dictionary_required": self.declared_dictionary_required,
        }


@dataclass(frozen=True)
class ScenarioPolicy:
    labels: frozenset[str] | None
    deny_labels: frozenset[str]
    min_risk: str
    action: dict[str, str]
    generalize: dict[str, str]
    roles: str

    def to_safe_dict(self) -> dict[str, object]:
        return {
            "labels": sorted(self.labels) if self.labels is not None else None,
            "deny_labels": sorted(self.deny_labels),
            "min_risk": self.min_risk,
            "action": dict(sorted(self.action.items())),
            "generalize": dict(sorted(self.generalize.items())),
            "roles": self.roles,
        }


@dataclass(frozen=True)
class MappingScope:
    mode: str

    def __post_init__(self) -> None:
        if self.mode not in MAPPING_SCOPE_MODES:
            raise ValueError(
                f"mapping scope must be one of {sorted(MAPPING_SCOPE_MODES)}"
            )

    def to_safe_dict(self) -> dict[str, object]:
        return {"mode": self.mode}


@dataclass(frozen=True)
class RefillPolicy:
    strict: bool

    def to_safe_dict(self) -> dict[str, object]:
        return {"strict": self.strict}


@dataclass(frozen=True)
class GuardPolicy:
    scan_injection: bool
    scan_input_secrets: bool
    check_tool_results: bool
    scan_output_secrets: bool
    scan_output_pii: bool
    block_on_hallucinated_placeholder: bool
    alert_min_severity: str
    block_min_severity: str | None

    def to_safe_dict(self) -> dict[str, object]:
        return {
            "scan_injection": self.scan_injection,
            "scan_input_secrets": self.scan_input_secrets,
            "check_tool_results": self.check_tool_results,
            "scan_output_secrets": self.scan_output_secrets,
            "scan_output_pii": self.scan_output_pii,
            "block_on_hallucinated_placeholder": self.block_on_hallucinated_placeholder,
            "alert_min_severity": self.alert_min_severity,
            "block_min_severity": self.block_min_severity,
        }


@dataclass(frozen=True)
class PolicyDimensions:
    name: str
    detection: DetectionTier
    scenario: ScenarioPolicy
    mapping: MappingScope
    refill: RefillPolicy
    guard: GuardPolicy

    def to_safe_dict(self) -> dict[str, object]:
        return {
            "schema_version": POLICY_SCHEMA_VERSION,
            "name": self.name,
            "detection": self.detection.to_safe_dict(),
            "scenario": self.scenario.to_safe_dict(),
            "mapping": self.mapping.to_safe_dict(),
            "refill": self.refill.to_safe_dict(),
            "guard": self.guard.to_safe_dict(),
        }


def dimensions_from_profile(profile: Profile) -> PolicyDimensions:
    """Convert a legacy profile to the explicit five policy dimensions."""
    return PolicyDimensions(
        name=profile.name,
        detection=DetectionTier(
            use_ner=profile.use_ner,
            ner_required=profile.ner_required,
        ),
        scenario=ScenarioPolicy(
            labels=profile.labels,
            deny_labels=profile.deny_labels,
            min_risk=profile.min_risk,
            action=dict(profile.action),
            generalize=dict(profile.generalize),
            roles=profile.roles,
        ),
        mapping=MappingScope(profile.scope),
        refill=RefillPolicy(profile.refill_strict),
        guard=GuardPolicy(
            scan_injection=profile.scan_injection,
            scan_input_secrets=profile.scan_input_secrets,
            check_tool_results=profile.check_tool_results,
            scan_output_secrets=profile.scan_output_secrets,
            scan_output_pii=profile.scan_output_pii,
            block_on_hallucinated_placeholder=profile.block_on_hallucinated_placeholder,
            alert_min_severity=profile.alert_min_severity,
            block_min_severity=profile.block_min_severity,
        ),
    )


def profile_from_dimensions(policy: PolicyDimensions) -> Profile:
    """Convert explicit dimensions back to the compatibility ``Profile``."""
    if not policy.detection.rules_required:
        raise ValueError("legacy Profile runtime always requires rules")
    if not policy.detection.declared_dictionary_required:
        raise ValueError("legacy Profile runtime always requires a declared dictionary")
    if policy.mapping.mode not in LEGACY_MAPPING_SCOPE_MODES:
        # A namespace mapping is a v1 capability-layer contract; collapsing it
        # to document/session would silently weaken cross-document identity.
        raise ValueError("legacy Profile cannot express namespace mapping scope")
    return Profile(
        name=policy.name,
        labels=policy.scenario.labels,
        deny_labels=policy.scenario.deny_labels,
        min_risk=policy.scenario.min_risk,
        action=dict(policy.scenario.action),
        generalize=dict(policy.scenario.generalize),
        roles=policy.scenario.roles,
        scope=policy.mapping.mode,
        use_ner=policy.detection.use_ner,
        ner_required=policy.detection.ner_required,
        refill_strict=policy.refill.strict,
        scan_injection=policy.guard.scan_injection,
        scan_input_secrets=policy.guard.scan_input_secrets,
        check_tool_results=policy.guard.check_tool_results,
        scan_output_secrets=policy.guard.scan_output_secrets,
        scan_output_pii=policy.guard.scan_output_pii,
        block_on_hallucinated_placeholder=policy.guard.block_on_hallucinated_placeholder,
        alert_min_severity=policy.guard.alert_min_severity,
        block_min_severity=policy.guard.block_min_severity,
    )


def policy_compatibility_metadata(profile: Profile) -> dict[str, object]:
    """Safe response metadata guiding callers through the migration window."""
    return {
        "schema_version": POLICY_SCHEMA_VERSION,
        "compatibility": PROFILE_COMPATIBILITY,
        "legacy_profile": profile.name,
        "dimensions": dimensions_from_profile(profile).to_safe_dict(),
    }
