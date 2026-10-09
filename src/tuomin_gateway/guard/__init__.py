"""Input/output guard layer (pillars 5 & 6).

Runs dependency-light scanners (prompt-injection signatures, secrets) on the
inbound request and the outbound model response, turning hits into AlertEvents.
Default posture per profile: emit alerts at/above ``alert_min_severity`` and only
hard-block when ``block_min_severity`` is set (opt-in) — so the guard never
breaks an existing app by default. Output checks also surface hallucinated /
unknown placeholders (tuomin-specific: the model invented a placeholder we can't
refill).

Honest limits: signature detection catches common patterns, not novel/adaptive
attacks, and is weaker on Chinese. Never claim complete protection.
"""
from __future__ import annotations

from collections.abc import Iterable

from tuomin_gateway.audit import build_alert_event
from tuomin_gateway.guard import injection, secrets
from tuomin_gateway.schemas import RANK_TO_SEVERITY, SEVERITY_RANK, AlertEvent


def _rank(severity: str) -> int:
    return SEVERITY_RANK.get(severity, 0)


def _action_for(severity: str, profile) -> str:
    block_min = getattr(profile, "block_min_severity", None)
    if block_min and _rank(severity) >= _rank(block_min):
        return "block"
    return "warn"


def _emit(severity: str, profile) -> bool:
    return _rank(severity) >= _rank(getattr(profile, "alert_min_severity", "warn"))


class GuardOutcome:
    def __init__(self, events: Iterable[AlertEvent]) -> None:
        self.events: list[AlertEvent] = list(events)

    @property
    def blocking(self) -> list[AlertEvent]:
        return [e for e in self.events if e.action == "block"]

    def headers(self) -> dict[str, str]:
        if not self.events:
            return {}
        max_rank = max(_rank(e.severity) for e in self.events)
        max_sev = RANK_TO_SEVERITY[max_rank]
        types = sorted({e.alert_type for e in self.events})
        return {
            "x-tuomin-alert-count": str(len(self.events)),
            "x-tuomin-alert-max-severity": max_sev,
            "x-tuomin-alert-types": ",".join(types),
        }


def _maybe(events: list, *, alert_type, severity, direction, source, detector, matched,
           pattern_id, profile, store_hashes=True):
    action = _action_for(severity, profile)
    if not _emit(severity, profile) and action != "block":
        return
    events.append(build_alert_event(
        alert_type=alert_type, severity=severity, direction=direction, action=action,
        source=source, detector=detector, detector_version="builtin",
        matched_values=matched, summary=pattern_id,
        label_counts={pattern_id: len(matched)}, store_hashes=store_hashes,
    ))


def _emit_secrets(events: list, text: str, direction: str, profile) -> None:
    for pattern_id, value in secrets.scan(text):
        # The generic entropy-gated pattern catches guessable values (passwords);
        # tag it as source=entropy and keep NO hash — even a keyed hash of a weak
        # secret is best avoided. Structured high-entropy tokens keep a keyed hash.
        is_generic = pattern_id == "generic_secret"
        _maybe(events, alert_type="secret", severity="critical", direction=direction,
               source="entropy" if is_generic else "signature",
               detector="builtin-secrets", matched=[value], pattern_id=pattern_id,
               profile=profile, store_hashes=not is_generic)


def scan_input(text: str, profile) -> GuardOutcome:
    events: list[AlertEvent] = []
    if getattr(profile, "scan_injection", True):
        for pattern_id, matched in injection.scan(text):
            _maybe(events, alert_type="injection", severity="warn", direction="input",
                   source="signature", detector="builtin-injection", matched=[matched],
                   pattern_id=pattern_id, profile=profile)
    if getattr(profile, "scan_input_secrets", True):
        _emit_secrets(events, text, "input", profile)
    return GuardOutcome(events)


def scan_output(text: str, profile, unknown_placeholders: Iterable[str] = ()) -> GuardOutcome:
    events: list[AlertEvent] = []
    if getattr(profile, "scan_output_secrets", True):
        _emit_secrets(events, text, "output", profile)
    unknown = list(unknown_placeholders)
    if unknown:
        # The model emitted a placeholder we have no mapping for — can't refill it.
        severity = "warn"
        force_block = bool(getattr(profile, "block_on_hallucinated_placeholder", False))
        action = "block" if force_block else _action_for(severity, profile)
        if _emit(severity, profile) or action == "block":
            events.append(build_alert_event(
                alert_type="hallucinated_placeholder", severity=severity, direction="output",
                action=action, source="placeholder", detector="placeholder-integrity",
                detector_version="builtin", matched_values=unknown,
                summary="unknown placeholder(s) in model response",
            ))
    return GuardOutcome(events)
