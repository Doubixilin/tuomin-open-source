"""Redaction profiles (脱敏档位).

A profile is the per-application policy that turns raw detections into a
redaction decision: which labels to redact, at what risk threshold, and what to
do otherwise (pass through / block the whole request). Each consuming app
declares one profile, so the same neutral engine serves contract review, agent
clusters and the knowledge base with different security/utility trade-offs.

Applied AFTER fusion and BEFORE placeholder replacement: see ``apply_profile``.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace

from tuomin_gateway.roles import ROLE_SETS
from tuomin_gateway.schemas import DetectionSpan


# low < medium < high < critical. "unknown" is treated as high so that a span
# of uncertain risk is redacted by default (safe over leaky).
_RISK_RANK = {"low": 1, "medium": 2, "high": 3, "critical": 4, "unknown": 3}

# Per-span decisions.
REDACT = "redact"   # replace with a reversible placeholder
GENERALIZE = "generalize"  # coarsen the value irreversibly (amount→range etc.); see generalizers.py
PASS = "pass"       # leave in clear text (e.g. amounts a reviewer must analyse)
BLOCK = "block"     # this label must never reach the cloud — flag the request
WARN = "warn"       # guard-layer action: raise an alert but do NOT block (pillars 5 & 6)

_SEMANTIC_POLICY_LABELS = frozenset(
    {
        "INTERNAL_OPINION",
        "LEGAL_STRATEGY",
        "RISK_JUDGMENT",
        "NEGOTIATION_FLOOR",
        "NEGOTIATION_STRATEGY",
        "NEGOTIATION_POSITION",
        "INTERNAL_PROCESS",
    }
)

_LEGAL_ANALYSIS_LABELS = frozenset({"COURT", "ARBITRATION", "STATUTE"})


def _risk_rank(level: str) -> int:
    return _RISK_RANK.get(level, _RISK_RANK["unknown"])


@dataclass(frozen=True)
class Profile:
    """Per-application redaction policy.

    labels:        allow-list of labels eligible for redaction; None = all.
    deny_labels:   labels that always pass through in clear text.
    min_risk:      only redact spans at/above this risk level (low/medium/high/critical).
    action:        per-label override -> "redact" | "pass" | "block" (wins over everything).
    scope:         "document" (placeholders restart per call) or "session" (stable across
                   calls). Migration-window field: frozen to the two legacy modes and NOT
                   read by the /api/v1 capability API (which pins "document" or binds a
                   persistent "namespace" mapping to the capability instead).
    use_ner:       whether the NER detector should run for this profile.
    ner_required:  whether an unavailable NER runtime must fail closed.
    refill_strict: True = block refill on any placeholder anomaly; False = lenient (session subset).
    """

    name: str
    labels: frozenset[str] | None = None
    deny_labels: frozenset[str] = field(default_factory=frozenset)
    min_risk: str = "low"
    action: dict[str, str] = field(default_factory=dict)
    # label -> generalizer name (see generalizers.GENERALIZERS). A label listed
    # here is coarsened irreversibly instead of placeholder-redacted (L3).
    generalize: dict[str, str] = field(default_factory=dict)
    # name of a role set in roles.ROLE_SETS; "" = no transaction-role assignment.
    # When set, ORG/SUPPLIER entities introduced by a role cue get a role-typed
    # placeholder (<PARTYA_001> …) instead of <ORG_001> (角色化占位).
    roles: str = ""
    scope: str = "document"
    use_ner: bool = False
    ner_required: bool = False
    refill_strict: bool = True
    # --- guard / inspection layer (pillars 5 & 6) -------------------------
    # All default to "scan and warn, never hard-block" so adding the guard
    # layer cannot break an existing app: alerts are emitted at/above
    # alert_min_severity; the guard only fail-closes when block_min_severity is
    # set (per-app opt-in). The masking pipeline's own fail-closed behaviour
    # (blocked labels, refill_strict) is unchanged and independent of these.
    scan_injection: bool = True            # inspect input for prompt-injection signatures
    scan_input_secrets: bool = True        # inspect input (incl. tool args) for secrets
    check_tool_results: bool = True        # treat tool_result/retrieved content as untrusted
    scan_output_secrets: bool = True       # inspect model response for leaked secrets
    scan_output_pii: bool = True           # inspect model response for re-leaked PII
    block_on_hallucinated_placeholder: bool = False  # guard-level (separate from refill_strict)
    alert_min_severity: str = "warn"       # info | warn | critical — emit alerts at/above this
    block_min_severity: str | None = None  # None = guard never hard-blocks; "critical" to opt in

    def __post_init__(self) -> None:
        # Fail fast at construction: a typo'd role set name would otherwise be
        # silently ignored by assign_roles (roles.ROLE_SETS.get -> None), an
        # invisible utility regression. A typo'd scope would only surface much
        # later in MappingScope conversion — validate at the boundary instead.
        if self.roles and self.roles not in ROLE_SETS:
            raise ValueError(
                f"unknown role set: {self.roles!r} "
                f"(expected '' or one of {sorted(ROLE_SETS)})"
            )
        if self.scope not in {"document", "session"}:
            raise ValueError(
                f"unknown profile scope: {self.scope!r} "
                "(legacy Profile is frozen to 'document'/'session'; "
                "namespace mappings are a v1 capability-layer contract)"
            )

    def decide(self, span: DetectionSpan) -> str:
        """Return the action (redact/generalize/pass/block) for a single span."""
        if span.metadata.get("whole_private_key") and span.label == "CREDENTIAL":
            return BLOCK if self.action.get("CREDENTIAL") == BLOCK else REDACT
        override = self.action.get(span.label)
        if override is not None:
            return override
        # A label explicitly marked for generalization is coarsened regardless of
        # deny/min_risk — it is an intentional middle ground between redact and
        # pass, so it wins over those (the explicit per-label action still wins
        # over it, above).
        if span.label in self.generalize:
            return GENERALIZE
        if span.label in self.deny_labels:
            return PASS
        if self.labels is not None and span.label not in self.labels:
            return PASS
        if _risk_rank(span.risk_level) < _risk_rank(self.min_risk):
            return PASS
        return REDACT


def is_fully_reversible(profile: Profile) -> bool:
    """Return whether a profile can never choose irreversible generalization."""
    return not profile.generalize and all(
        action != GENERALIZE for action in profile.action.values()
    )


def apply_profile(
    spans: list[DetectionSpan], profile: Profile
) -> tuple[list[DetectionSpan], list[str]]:
    """Filter fused detections through a profile.

    Returns ``(kept_spans, blocked_labels)`` where kept_spans are the ones to
    process (redact OR generalize) and blocked_labels lists labels whose presence
    the profile marks as must-not-reach-cloud (the caller decides whether to
    refuse the request). BLOCK spans are ALSO redacted — a blocked label must
    never be left in clear text; the label is merely additionally flagged so the
    caller can refuse.

    A span chosen for generalization is tagged in ``metadata`` (``action`` /
    ``generalizer``) so the redactor coarsens it irreversibly instead of mapping
    it to a placeholder. The tag survives re-fusion (``resolve_overlaps`` keeps
    non-overlapping spans as-is and re-uses ``replace`` for clipped ones).
    """
    kept: list[DetectionSpan] = []
    blocked: list[str] = []
    for span in spans:
        decision = profile.decide(span)
        if decision == REDACT:
            kept.append(span)
        elif decision == GENERALIZE:
            kept.append(
                replace(
                    span,
                    metadata={
                        **span.metadata,
                        "action": GENERALIZE,
                        "generalizer": profile.generalize.get(span.label),
                    },
                )
            )
        elif decision == BLOCK:
            kept.append(span)
            blocked.append(span.label)
    return kept, sorted(set(blocked))


# --- built-in presets -------------------------------------------------------
# Names map to the consuming apps. Apps may also register custom profiles.

PRESETS: dict[str, Profile] = {
    # Knowledge base: redact every identity-type detection (current KB behaviour),
    # session-stable so a cloud agent reading many snippets stays consistent.
    "kb": Profile(name="kb", scope="session", use_ner=False, refill_strict=False),
    # Contract review: a reviewer LLM must still SEE amounts and dates to analyse
    # payment terms, so those pass through; identity entities are redacted.
    # Document-scoped with strict refill (single round-trip QA).
    "contract_review": Profile(
        name="contract_review",
        deny_labels=frozenset({"AMOUNT", "DATE"}) | _LEGAL_ANALYSIS_LABELS,
        scope="document",
        use_ner=True,
        ner_required=True,
        refill_strict=True,
    ),
    # Agent cluster: many tool-call round-trips, so session-stable placeholders
    # and lenient refill (the final answer is a subset of all masked evidence).
    # refill_strict MUST stay False for chat-style agents: strict refill does
    # document-wide placeholder integrity checks and fails closed
    # (SSE missing_placeholder) whenever the reply does not echo every masked
    # entity — correct for whole-document transforms, wrong for chat. For a
    # stricter chat profile use inline {"base": "strict", "refill_strict": false}
    # (keeps ner_required and the detection/blocking floor). Verified by the
    # 2026-08-22 WorkBuddy smoke; regression: test_proxy.py auto-refill stream pair.
    "agent": Profile(name="agent", scope="session", use_ner=True, refill_strict=False),
    # Maximal: redact everything detected, strict refill. A safe default for
    # apps that have not declared a profile yet.
    "strict": Profile(
        name="strict",
        scope="document",
        use_ner=True,
        ner_required=True,
        refill_strict=True,
    ),
    # File outputs must remain refill-restorable, so generalization is forbidden.
    "file_workbench": Profile(
        name="file_workbench",
        scope="document",
        use_ner=True,
        ner_required=False,
        refill_strict=True,
    ),
    # Review mode: core detections still redact, semantic policy spans are not
    # placeholder-masked but are surfaced as local review/warn decisions.
    "review": Profile(
        name="review",
        action={label: WARN for label in _SEMANTIC_POLICY_LABELS},
        scope="document",
        use_ner=True,
        ner_required=True,
        refill_strict=True,
    ),
}


# --- declared sensitivity tiers ---------------------------------------------
# An app declares "脱敏深度" as a sensitivity level; these map it onto the
# existing knobs so depth genuinely differs per app without any engine change.
#   light    – redact only clearly-sensitive formatted PII (contact/account/
#              credential/org-code/url); names, orgs, amounts, dates all pass;
#              NER off. Max utility, min redaction.
#   standard – redact identity + formatted PII but PASS amounts/dates so a
#              reviewing LLM still sees figures/deadlines; NER on.
#   strict   – redact everything detected incl. amounts/dates; NER on (default).
#   ultra    – strict + credentials are hard-blocked (must never reach cloud).
_FORMATTED_SENSITIVE = frozenset({"CONTACT", "BANK_ACCOUNT", "CREDENTIAL", "ORG_CODE", "SYSTEM_URL"})

SENSITIVITY_PRESETS: dict[str, Profile] = {
    "light": Profile(name="light", labels=_FORMATTED_SENSITIVE, use_ner=False),
    "standard": Profile(
        name="standard",
        deny_labels=frozenset({"AMOUNT", "DATE"}) | _LEGAL_ANALYSIS_LABELS,
        action={label: PASS for label in _SEMANTIC_POLICY_LABELS},
        use_ner=True,
    ),
    "strict": PRESETS["strict"],
    "ultra": replace(
        PRESETS["strict"], name="ultra",
        action={"CREDENTIAL": "block"}, block_min_severity="critical",
    ),
}


# --- scenario presets (场景化脱敏) ------------------------------------------
# A scenario bundles a domain-specific policy: which labels to redact / pass /
# generalize / block for ONE business situation. Unlike the generic sensitivity
# tiers, scenarios encode "what is sensitive HERE" — the per-scenario judgement
# that a generic gateway can't make. See
# ``docs/design/desensitization-layer-strategy.md`` §2. Field values are the
# user's法务 decision; more scenarios (投前/HR/风险数据) are added per stage.
SCENARIO_PRESETS: dict[str, Profile] = {
    # 合同审查：交易主体身份脱敏（ORG/SUPPLIER 后续在 block 3 带交易角色）；
    # AMOUNT/DATE 明文通过供付款条款分析；ADDRESS 泛化到省级（具体位置是强
    # 再识别信号，审查少用精确街道）；法律策略/谈判底线不得上云（block）。
    "contract": Profile(
        name="contract",
        deny_labels=frozenset({"AMOUNT", "DATE"}) | _LEGAL_ANALYSIS_LABELS,
        generalize={"ADDRESS": "region"},
        action={"LEGAL_STRATEGY": "block", "NEGOTIATION_POSITION": "block"},
        roles="contract",
        scope="document",
        use_ner=True,
        ner_required=True,
        refill_strict=True,
    ),
    # 投前投资项目：未公开项目，防反推优先。主体保持纯身份占位，不从零散语境
    # 推断投资角色（推断错误会破坏跨文档 namespace 的身份稳定性）；项目名/
    # 地址/时间(推进信号)/编号一律占位脱敏；投资体量泛化到 10亿档（保留量级供
    # 逻辑/政策判断，降"体量+行业+区域"三联可归属）；行业类型/投资结构属明文
    # （非敏感标签，不检测即通过）。法律策略/谈判底线不得上云。
    "pre_investment": Profile(
        name="pre_investment",
        generalize={"AMOUNT": "amount_band_10yi"},
        action={"LEGAL_STRATEGY": "block", "NEGOTIATION_POSITION": "block"},
        roles="",
        scope="document",
        use_ner=True,
        ner_required=True,
        refill_strict=True,
    ),
    # 诉讼案件（建设工程施工合同纠纷/仲裁）：身份强脱、分析数据强保真。设计见
    # docs/design/litigation-scenario.md（v0.2 收敛——放弃角色化/关系层，身份保真
    # 优先）。AMOUNT/DATE 明文通过供精确法律计算（逾期天数/违约金）；ADDRESS 泛化到
    # 省（当事人地址非分析核心；法院名内嵌的地址由 COURT 词典整段保护、不在此被泛
    # 化）；COURT/ARBITRATION/STATUTE 保留原名（LLM 需识别具体法院/法规判审判倾向）
    # ——靠保护词典命中这些标签 + 此处 pass；当事人身份照常占位脱敏；**不引入交易
    # 角色**（角色是推断的、判错会耦合进身份占位符并伤回填，见设计 §7）。
    "litigation": Profile(
        name="litigation",
        deny_labels=frozenset({"AMOUNT", "DATE"}) | _LEGAL_ANALYSIS_LABELS,
        generalize={"ADDRESS": "region"},
        action={"COURT": "pass", "ARBITRATION": "pass", "STATUTE": "pass"},
        scope="document",
        use_ner=True,
        ner_required=True,
        refill_strict=True,
    ),
}


def get_profile(name: str) -> Profile:
    """Look up a built-in preset by name; raises KeyError if unknown."""
    return PRESETS[name]


def profile_for_sensitivity(name: str) -> Profile:
    """Map a declared sensitivity tier to a profile; raises KeyError if unknown."""
    return SENSITIVITY_PRESETS[name]


def profile_for_scenario(name: str) -> Profile:
    """Map a declared business scenario to a profile; raises KeyError if unknown."""
    return SCENARIO_PRESETS[name]


def resolve_named_profile(name: str) -> Profile:
    """Resolve a preset name, a sensitivity tier, or a scenario (in that
    precedence on name clash)."""
    if name in PRESETS:
        return PRESETS[name]
    if name in SENSITIVITY_PRESETS:
        return SENSITIVITY_PRESETS[name]
    if name in SCENARIO_PRESETS:
        return SCENARIO_PRESETS[name]
    raise KeyError(name)


def with_overrides(base: Profile, **overrides) -> Profile:
    """Derive a profile from a preset with field overrides (e.g. use_ner)."""
    return replace(base, **overrides)
