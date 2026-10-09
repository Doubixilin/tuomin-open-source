"""Session-stable redaction over an injected detector set + profile.

``redact_text`` / a one-shot redaction restart placeholder numbering on every
call, so the same entity can get different placeholders in different snippets —
fine for one-shot display, but useless when a cloud agent reads dozens of
snippets and then writes ONE answer that must be refilled. ``SessionRedactor``
keeps a single ``PlaceholderFactory`` + ``(label, value) -> placeholder`` map
for the whole session, so a given entity always maps to the SAME placeholder.

Neutral by construction: the caller injects the detectors (rules / its own
dictionary / any extra detector) and a ``Profile``; this module knows nothing
about any specific application's entities.
"""
from __future__ import annotations

import importlib.util
import os
from dataclasses import dataclass
from pathlib import Path
import re
import threading

from tuomin_gateway.detectors.base import BaseDetector
from tuomin_gateway.detectors.base import hash_text
from tuomin_gateway.detectors.dictionary import DictionaryDetector
from tuomin_gateway.detectors.rules import PublicContextRuleDetector, RuleDetector
from tuomin_gateway.fusion import fuse_detections
from tuomin_gateway.generalizers import generalize_value
from tuomin_gateway.mapping import PlaceholderFactory
from tuomin_gateway.placeholders import PLACEHOLDER_RE, find_altered_placeholders, substitute
from tuomin_gateway.profiles import GENERALIZE, PASS, Profile, apply_profile
from tuomin_gateway.redactor import normalize_detections
from tuomin_gateway.roles import assign_roles
from tuomin_gateway.schemas import DetectionSpan
from tuomin_gateway.schemas import MappingEntry
from tuomin_gateway.schemas import RedactionTraceEntry


_PLACEHOLDER_PARTS_RE = re.compile(r"^<([A-Z][A-Z0-9_]*?)_(\d+)>$")
_AREA_QUANTITY_RE = re.compile(
    r"^\s*\d+(?:[,，]\d{3})*(?:\.\d+)?\s*[万亿]?\s*"
    r"(?:平\s*方\s*米|平\s*米|㎡|m²)?\s*$",
    re.IGNORECASE,
)
_AREA_UNIT_RE = re.compile(
    r"^\s*(?:平\s*方\s*米|平\s*米|㎡|m²)", re.IGNORECASE
)
_FLEXIBLE_DECLARED_LABELS = frozenset({"PROJECT_NAME", "DOCUMENT_NAME"})
_SESSION_MAPPING_VERSION = "session-mapping-v1"


def _is_area_quantity_mislabeled_as_amount(
    text: str, span: DetectionSpan
) -> bool:
    """Reject AMOUNT predictions that are plainly area quantities.

    The local NER model can label the numeric prefix of ``2.23万平方米`` as
    money even when the deterministic money rule correctly declines it.  The
    suppression belongs after detector collection so rules, dictionaries and
    current or future model detectors all share the same semantic guard.
    Currency-bearing values such as ``2万元/平方米`` do not match this shape.
    """

    if span.label != "AMOUNT":
        return False
    value = text[span.start : span.end]
    if _AREA_QUANTITY_RE.fullmatch(value) is None:
        return False
    return bool(
        re.search(
            r"(?:平\s*方\s*米|平\s*米|㎡|m²)\s*$", value, re.IGNORECASE
        )
        or _AREA_UNIT_RE.match(text[span.end :])
    )


def _known_value_detections(
    text: str,
    values: tuple[tuple[str, str], ...],
) -> list[DetectionSpan]:
    """Re-detect values already owned by one namespace/session.

    Exact remasking keeps an entity protected after the NER sees it once.
    Structured project/document names additionally tolerate a small PDF soft
    wrap between characters, so a declared project name cannot reappear in a
    later material block merely because extraction inserted one newline.
    """

    spans: list[DetectionSpan] = []
    seen: set[tuple[int, int, str]] = set()
    for label, value in values:
        if len(value) < 2 or len(value) > 1000:
            continue
        patterns = [re.compile(re.escape(value))]
        if (
            label in _FLEXIBLE_DECLARED_LABELS
            and value == value.strip()
            and not any(character.isspace() for character in value)
        ):
            patterns.append(
                re.compile(
                    r"[ \t\r\n]{0,2}".join(
                        re.escape(character) for character in value
                    )
                )
            )
        for pattern in patterns:
            for match in pattern.finditer(text):
                key = (match.start(), match.end(), label)
                if key in seen:
                    continue
                seen.add(key)
                surface = text[match.start() : match.end()]
                spans.append(
                    DetectionSpan(
                        start=match.start(),
                        end=match.end(),
                        label=label,
                        confidence=1.0,
                        source=(
                            "declared_value"
                            if label in _FLEXIBLE_DECLARED_LABELS
                            else "session_mapping"
                        ),
                        detector_version=(
                            "structured-values-v1"
                            if label in _FLEXIBLE_DECLARED_LABELS
                            else _SESSION_MAPPING_VERSION
                        ),
                        text_hash=hash_text(surface),
                        risk_level="high",
                        metadata={"canonical_value": value},
                    )
                )
    return spans


@dataclass(frozen=True)
class DetectorState:
    name: str
    required: bool
    active: bool
    version: str | None = None
    error_type: str | None = None

    def to_safe_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "required": self.required,
            "active": self.active,
            "status": "ok" if self.active else "unavailable",
            "version": self.version,
            "error_type": self.error_type,
        }


@dataclass(frozen=True)
class DetectorReadiness:
    states: tuple[DetectorState, ...]

    @property
    def missing_required(self) -> tuple[DetectorState, ...]:
        return tuple(state for state in self.states if state.required and not state.active)

    def to_safe_dict(self) -> dict[str, object]:
        states = sorted(self.states, key=lambda state: state.name)
        errors = [
            {
                "detector": state.name,
                "error_type": state.error_type or "Unavailable",
                "message": f"{state.name} detector unavailable",
            }
            for state in states
            if not state.active
        ]
        return {
            "requested": [state.name for state in states],
            "required": [state.name for state in states if state.required],
            "active": [state.name for state in states if state.active],
            "degraded": bool(errors),
            "errors": errors,
            "states": [state.to_safe_dict() for state in states],
        }


@dataclass(frozen=True)
class DetectionRun:
    kept: list[DetectionSpan]
    blocked_labels: list[str]
    readiness: DetectorReadiness


class RequiredDetectorUnavailable(RuntimeError):
    """A required detector failed; callers must not continue with weaker coverage."""

    def __init__(self, readiness: DetectorReadiness) -> None:
        super().__init__("required detector unavailable")
        self.readiness = readiness


def build_detectors(
    entries: list[dict] | None,
    *,
    dictionary_version: str | None = None,
) -> list[BaseDetector]:
    """The neutral detector set: always the rule layer, plus the app's dictionary
    when it has one. Shared by the service endpoints and the reverse proxy."""
    detectors: list[BaseDetector] = [RuleDetector()]
    if entries is not None:
        detector = DictionaryDetector.from_entries(entries)
        if dictionary_version is not None:
            detector.version = dictionary_version
        detectors.append(detector)
    return detectors


def run_detection(
    text: str, detectors: list[BaseDetector], profile: Profile
) -> DetectionRun:
    """Run requested detectors and return a safe, explicit readiness result.

    Rule and configured dictionary detectors are always required. NER is
    required only for profiles that declare ``ner_required``; other profiles
    may continue with an explicit degraded result.
    """
    from tuomin_gateway.detectors.rules import private_key_ranges

    private_key_ranges(text)  # framing failures are input errors, not detector failures
    spans: list[DetectionSpan] = []
    states: list[DetectorState] = []
    for detector in detectors:
        try:
            spans.extend(detector.detect(text))
            states.append(
                DetectorState(
                    name=detector.name,
                    required=True,
                    active=True,
                    version=getattr(detector, "version", None),
                )
            )
        except Exception as exc:
            states.append(
                DetectorState(
                    name=getattr(detector, "name", "unknown"),
                    required=True,
                    active=False,
                    version=getattr(detector, "version", None),
                    error_type=type(exc).__name__,
                )
            )

    public_labels = {"PUBLIC_REGION", "PUBLIC_AUTHORITY"}
    passed_labels = set(profile.deny_labels) | {
        label for label, action in profile.action.items() if action == PASS
    }
    if public_labels.issubset(passed_labels):
        public_context = PublicContextRuleDetector()
        spans.extend(public_context.detect(text))
        states.append(
            DetectorState(
                name=public_context.name,
                required=True,
                active=True,
                version=public_context.version,
            )
        )

    if profile.use_ner:
        try:
            from tuomin_gateway.detectors.ner import get_ner_detector

            ner = get_ner_detector()
            spans.extend(ner.detect(text))
            states.append(
                DetectorState(
                    name="ner",
                    required=profile.ner_required,
                    active=True,
                    version=getattr(ner, "version", None),
                )
            )
        except Exception as exc:
            states.append(
                DetectorState(
                    name="ner",
                    required=profile.ner_required,
                    active=False,
                    error_type=type(exc).__name__,
                )
            )
    elif profile.ner_required:
        states.append(
            DetectorState(
                name="ner",
                required=True,
                active=False,
                error_type="DetectorConfigurationError",
            )
        )

    readiness = DetectorReadiness(tuple(states))
    if readiness.missing_required:
        raise RequiredDetectorUnavailable(readiness)

    spans = [
        span
        for span in spans
        if not _is_area_quantity_mislabeled_as_amount(text, span)
    ]
    fused = fuse_detections(spans, text)
    kept, blocked = apply_profile(fused, profile)
    if profile.roles:
        kept = assign_roles(text, kept, profile.roles)
    return DetectionRun(kept=kept, blocked_labels=blocked, readiness=readiness)


def collect_detections(
    text: str, detectors: list[BaseDetector], profile: Profile
) -> tuple[list[DetectionSpan], list[str]]:
    """Run detectors (+ NER if the profile asks), fuse, and apply the profile.

    Compatibility wrapper returning ``(kept_spans, blocked_labels)``. Required
    detector failures raise ``RequiredDetectorUnavailable``; optional failures
    are available through ``run_detection(...).readiness``.
    """
    result = run_detection(text, detectors, profile)
    return result.kept, result.blocked_labels


def _ner_model_cached() -> bool:
    """Check the pinned model's config in the local HF cache without network.

    An explicit ``TUOMIN_NER_MODEL_DIR`` (packaged builds) is honored first:
    readiness must report the same source the runtime loader actually uses.
    """
    model_dir = os.environ.get("TUOMIN_NER_MODEL_DIR")
    if model_dir:
        return all((Path(model_dir) / name).is_file() for name in (
            "config.json", "pytorch_model.bin", "tokenizer_config.json",
            "special_tokens_map.json", "vocab.txt",
        ))
    try:
        from huggingface_hub import try_to_load_from_cache
        from tuomin_gateway.detectors.ner import MODEL_NAME, MODEL_REVISION

        cached = try_to_load_from_cache(
            MODEL_NAME, "config.json", revision=MODEL_REVISION
        )
        return isinstance(cached, str)
    except Exception:
        return False


def probe_ner_runtime(*, required: bool) -> dict[str, object]:
    """Load the configured local NER runtime and return safe readiness fields."""
    installed = importlib.util.find_spec("transformers") is not None
    if not installed:
        return {
            "requested": True,
            "required": required,
            "installed": False,
            "model_cached": False,
            "loadable": False,
            "active": False,
            "error_type": "DependencyUnavailable",
        }
    model_cached = _ner_model_cached()
    try:
        from tuomin_gateway.detectors.ner import get_ner_detector

        detector = get_ner_detector()
        from contextlib import nullcontext

        with getattr(detector, "_lock", nullcontext()):
            detector._pipeline()
        return {
            "requested": True,
            "required": required,
            "installed": True,
            "model_cached": True,
            "loadable": True,
            "active": True,
            "version": detector.version,
            "error_type": None,
        }
    except Exception as exc:
        return {
            "requested": True,
            "required": required,
            "installed": True,
            "model_cached": model_cached,
            "loadable": False,
            "active": False,
            "error_type": type(exc).__name__,
        }


class SessionRedactor:
    """Placeholder redaction kept CONSISTENT across many masking calls.

    Thread safety: the legacy ``/session/*`` endpoints (and the CCR reverse
    proxy) share ONE SessionRedactor across concurrent requests, and FastAPI
    runs sync endpoints on a thread pool. Every read/mutation of the mapping
    state is therefore guarded by ``self._lock`` — an interleaved
    check-then-act in ``_placeholder_for`` could hand the same placeholder
    number to two different entities, and the later ``mapping[placeholder]``
    write would then overwrite the earlier one, refilling the WRONG original.
    Detection (incl. NER inference) deliberately runs OUTSIDE the lock so
    concurrent callers are not serialized on the slow detector path.
    """

    def __init__(
        self,
        detectors: list[BaseDetector],
        profile: Profile,
        *,
        identity_mode: str = "canonical",
    ) -> None:
        if identity_mode not in {"canonical", "surface"}:
            raise ValueError("identity_mode must be canonical or surface")
        self._detectors = list(detectors)
        self.profile = profile
        self.identity_mode = identity_mode
        self._factory = PlaceholderFactory()
        self._key_to_ph: dict[tuple[str, str], str] = {}
        self.mapping: dict[str, str] = {}  # placeholder -> original (canonical) value
        self._placeholder_labels: dict[str, str] = {}
        self._known_values: dict[tuple[str, str], None] = {}
        self._trace: list[RedactionTraceEntry] = []
        self.blocked_labels: set[str] = set()
        self.last_readiness: DetectorReadiness | None = None
        self._lock = threading.Lock()

    def _placeholder_for(self, prefix: str, value: str, *, label: str | None = None) -> str:
        """``prefix`` is the placeholder prefix — the entity label, or a
        transaction role (PARTYA…) when role-aware (角色化占位).
        Caller must hold ``self._lock``."""
        key = (prefix, value)
        placeholder = self._key_to_ph.get(key)
        if placeholder is None:
            placeholder = self._factory.next(prefix)
            self._key_to_ph[key] = placeholder
            self.mapping[placeholder] = value
            self._placeholder_labels[placeholder] = label or prefix
            if prefix == (label or prefix):
                self._known_values[(label or prefix, value)] = None
        return placeholder

    def hydrate(self, entries: list[MappingEntry]) -> None:
        """Restore a persistent canonical mapping and resume prefix counters.

        The placeholder grammar check is single-sourced to ``PLACEHOLDER_RE``:
        a looser form (e.g. ``<ORG_01>``) would hydrate fine but then self-lock
        as "altered" on every refill, so it is rejected here (fail-closed).
        """
        with self._lock:
            for entry in entries:
                match = _PLACEHOLDER_PARTS_RE.match(entry.placeholder)
                if match is None or PLACEHOLDER_RE.fullmatch(entry.placeholder) is None:
                    raise ValueError("invalid persisted placeholder")
                prefix, number = match.group(1), int(match.group(2))
                key = (prefix, entry.original_value)
                existing = self._key_to_ph.get(key)
                if existing is not None and existing != entry.placeholder:
                    raise ValueError("duplicate persisted identity mapping")
                if entry.placeholder in self.mapping:
                    raise ValueError("duplicate persisted placeholder")
                self._key_to_ph[key] = entry.placeholder
                self.mapping[entry.placeholder] = entry.original_value
                self._placeholder_labels[entry.placeholder] = entry.label
                if prefix == entry.label:
                    self._known_values[(entry.label, entry.original_value)] = None
                self._factory.reserve(prefix, number)

    def mapping_entries(self) -> list[MappingEntry]:
        with self._lock:
            return [
                MappingEntry(
                    placeholder=placeholder,
                    label=self._placeholder_labels.get(placeholder, placeholder),
                    original_value=value,
                    text_hash=hash_text(value),
                )
                for placeholder, value in self.mapping.items()
            ]

    def trace_entries(self) -> list[RedactionTraceEntry]:
        """Return a snapshot of encrypted-inspection-only redaction trace."""
        with self._lock:
            return list(self._trace)

    def mask(self, text: str) -> str:
        """Replace detected sensitive spans with session-stable placeholders."""
        if not text:
            return text
        # Detection (incl. NER inference) runs outside the lock; only the
        # mapping mutation below needs mutual exclusion.
        with self._lock:
            known_values = tuple(self._known_values)
        run = run_detection(text, self._detectors, self.profile)
        detections = normalize_detections(
            text,
            [*run.kept, *_known_value_detections(text, known_values)],
        )
        with self._lock:
            self.last_readiness = run.readiness
            self.blocked_labels.update(run.blocked_labels)
            replacements: list[tuple[int, int, str]] = []
            for span in detections:
                original = text[span.start : span.end]
                # Generalization (L3): irreversible coarsening, never added to the
                # session mapping. Fail-closed to a placeholder if it can't coarsen.
                if span.metadata.get("action") == GENERALIZE:
                    coarse = generalize_value(span.metadata.get("generalizer"), original)
                    if coarse is not None:
                        replacements.append((span.start, span.end, coarse))
                        self._trace.append(
                            RedactionTraceEntry(
                                label=span.label,
                                original_value=original,
                                redacted_value=coarse,
                                action="generalize",
                                source=span.source,
                                detector_version=span.detector_version,
                                text_hash=span.text_hash,
                            )
                        )
                        continue
                value = (
                    span.metadata.get("canonical_value", original)
                    if self.identity_mode == "canonical"
                    else original
                )
                prefix = span.metadata.get("role") or span.label
                placeholder = self._placeholder_for(prefix, value, label=span.label)
                replacements.append((span.start, span.end, placeholder))
                self._trace.append(
                    RedactionTraceEntry(
                        label=span.label,
                        original_value=original,
                        redacted_value=placeholder,
                        action="redact",
                        source=span.source,
                        detector_version=span.detector_version,
                        text_hash=span.text_hash,
                    )
                )
        out = text
        for start, end, placeholder in sorted(replacements, key=lambda r: r[0], reverse=True):
            out = out[:start] + placeholder + out[end:]
        return out

    def mask_value(self, value: object, label: str) -> object:
        """Mask a KNOWN structured field by mapping the exact value to a
        session-stable placeholder — no detector/NER guessing. String bytes,
        including surrounding whitespace, are preserved for exact refill;
        trim is used only to decide whether the value is blank. Returns the
        value unchanged when empty/None so numbers and blanks pass through."""
        if value is None:
            return None
        text = str(value)
        if not text.strip():
            return value
        with self._lock:
            placeholder = self._placeholder_for(label, text, label=label)
            self._trace.append(
                RedactionTraceEntry(
                    label=label,
                    original_value=text,
                    redacted_value=placeholder,
                    action="redact",
                    source="declared_value",
                    detector_version="structured-values-v1",
                    text_hash=hash_text(text),
                )
            )
            return placeholder

    def unmask(self, text: str) -> str:
        """Replace placeholders with their originals (e.g. to refill a tool-call
        argument locally before querying)."""
        if not text:
            return text
        with self._lock:
            mapping = dict(self.mapping)
        out, _ = substitute(text, mapping)
        return out

    def refill(self, text: str, *, allow_missing: bool = False) -> dict:
        """Restore placeholders found in ``text`` and report integrity failures.

        Session workflows are usually lenient about omitted placeholders because
        an answer may cite only part of the masked evidence. Strict profiles keep
        document-style behavior and block when a known placeholder is missing.
        Unknown or altered placeholders are always blocked: the model either
        invented a mapping key or damaged one we cannot safely refill.
        """
        with self._lock:
            mapping = dict(self.mapping)  # snapshot; allocation may be in flight
        found = set(PLACEHOLDER_RE.findall(text))
        known = set(mapping)
        unknown = sorted(found - known)
        missing = (
            sorted(known - found)
            if self.profile.refill_strict and not allow_missing
            else []
        )
        altered = find_altered_placeholders(text)

        error_types: list[str] = []
        if unknown:
            error_types.append("unknown_placeholder")
        if missing:
            error_types.append("missing_placeholder")
        if altered:
            error_types.append("altered_placeholder")
        if error_types:
            return {
                "status": "blocked",
                "text": None,
                "error_types": error_types,
                "unknown_placeholders": unknown,
                "missing_placeholders": missing,
                "altered_placeholders": altered,
                "restored_count": 0,
            }

        restored, count = substitute(text, mapping)
        return {
            "status": "ok",
            "text": restored,
            "error_types": [],
            "unknown_placeholders": [],
            "missing_placeholders": [],
            "altered_placeholders": [],
            "restored_count": count,
        }
