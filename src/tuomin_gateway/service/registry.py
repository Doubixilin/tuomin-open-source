"""App registry: maps an ``app_id`` to its profile + dictionary.

Each consuming app (contract review, agent cluster, knowledge base) declares
its policy here, and maintains its OWN dictionary file — the service stays
neutral and never bakes in any app's entities.

Config file (JSON), located via ``TUOMIN_CONFIG`` env or ``./tuomin_apps.json``::

    {
      "apps": {
        "contract_review": {"profile": "contract_review", "dictionary": "C:/.../contract_dict.json"},
        "kb":              {"profile": "kb",              "dictionary": "C:/.../kb_entities.json"},
        "my_app":          {"profile": {"name": "custom", "use_ner": true, "deny_labels": ["AMOUNT"]}}
      }
    }

An app may additionally declare ``extra_dictionaries``: a list of read-only
shared seed files (e.g. ``data/litigation_protect_seed.json`` for the
litigation profile's COURT/ARBITRATION/STATUTE pass-through) merged after the
app's own dictionary at load time. The app's own file stays the only one the
admin API edits.

Unknown app_ids fall back to the built-in ``strict`` profile with no dictionary.
"""
from __future__ import annotations

import json
import hashlib
import os
import re
import secrets
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from tuomin_gateway.detectors.dictionary import DictionaryDetector
from tuomin_gateway.policy import MAPPING_SCOPE_MODES
from tuomin_gateway.profiles import (
    PRESETS,
    Profile,
    get_profile,
    profile_for_scenario,
    profile_for_sensitivity,
    resolve_named_profile,
)


class ProfileOverrideDenied(ValueError):
    """A request tried to replace the server-authorized app profile."""

    code = "profile_override_denied"


class RegistryConfigurationError(RuntimeError):
    """A declared protection resource is invalid or unavailable.

    The exception deliberately carries no filesystem path or parser detail so
    API responses cannot disclose local configuration information.
    """

    code = "dictionary_unavailable"

    def __init__(self, message: str = "declared dictionary unavailable", *, code: str | None = None) -> None:
        super().__init__(message)
        if code is not None:
            self.code = code


class CapabilityDenied(PermissionError):
    code = "capability_denied"


@dataclass(frozen=True)
class ResolvedDictionary:
    entries: list[dict[str, Any]] | None
    version: str | None
    configured: bool


@dataclass(frozen=True)
class _DictionaryCacheEntry:
    paths: tuple[str, ...]
    # (path, mtime_ns, size) for every merged dictionary file, in order.
    parts: tuple[tuple[str, int, int], ...]
    entries: list[dict[str, Any]]
    version: str


_STRUCTURED_VALUE_LABEL_RE = re.compile(r"[A-Z][A-Z0-9_]{0,63}")
PROXY_UPSTREAM_ENV = {
    "openai": "TUOMIN_UPSTREAM_OPENAI",
    "anthropic": "TUOMIN_UPSTREAM_ANTHROPIC",
}


def is_structured_value_label(value: object) -> bool:
    return (
        isinstance(value, str)
        and _STRUCTURED_VALUE_LABEL_RE.fullmatch(value) is not None
    )


def hash_capability_token(token: str) -> str:
    return "sha256:" + hashlib.sha256(token.encode("utf-8")).hexdigest()


def _config_path() -> Path:
    return Path(os.environ.get("TUOMIN_CONFIG", "tuomin_apps.json"))


def _profile_from_spec(spec: Any) -> Profile:
    """A profile spec is either a preset/sensitivity name (str) or an inline dict."""
    if isinstance(spec, str):
        return resolve_named_profile(spec)
    if isinstance(spec, dict):
        fields = dict(spec)
        base = resolve_named_profile(fields.pop("base")) if "base" in fields else None
        for key in ("labels", "deny_labels"):
            if key in fields and fields[key] is not None:
                fields[key] = frozenset(fields[key])
        if base is not None:
            from dataclasses import replace

            # An operator-authorized inline profile may intentionally disable
            # NER for a local/test app. Keep the two detector flags coherent;
            # request-time callers are not allowed to make this override.
            if fields.get("use_ner") is False and "ner_required" not in fields:
                fields["ner_required"] = False
            return replace(base, **fields)
        fields.setdefault("name", "custom")
        return Profile(**fields)
    raise ValueError("profile spec must be a preset name or a dict")


class AppRegistry:
    def __init__(
        self,
        apps: dict[str, dict[str, Any]] | None = None,
        *,
        config_path: str | Path | None = None,
        raw: dict[str, Any] | None = None,
    ) -> None:
        self._apps = apps if apps is not None else {}
        self._dict_cache: dict[str, _DictionaryCacheEntry] = {}
        self._dict_cache_lock = threading.RLock()
        self._config_path = Path(config_path) if config_path else None
        # Full config object, so write-back preserves top-level keys (comments etc).
        self._raw = raw if raw is not None else {"apps": self._apps}

    @classmethod
    def load(cls, path: str | Path | None = None) -> "AppRegistry":
        cfg_path = Path(path) if path else _config_path()
        if not cfg_path.exists():
            return cls({}, config_path=cfg_path, raw={"apps": {}})
        data = json.loads(cfg_path.read_text(encoding="utf-8"))
        apps = data.get("apps")
        if not isinstance(apps, dict):
            apps = {}
            data["apps"] = apps
        return cls(apps, config_path=cfg_path, raw=data)

    # --- admin write-back (pillar 4) ---------------------------------------
    def save_config(self) -> None:
        if self._config_path is None:
            raise RuntimeError("registry has no config path; cannot persist")
        self._config_path.write_text(
            json.dumps(self._raw, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def get_app(self, app_id: str) -> dict[str, Any] | None:
        entry = self._apps.get(app_id)
        return dict(entry) if isinstance(entry, dict) else None

    def upsert_app(self, app_id: str, entry: dict[str, Any]) -> None:
        self._apps[app_id] = entry
        self.save_config()

    def delete_app(self, app_id: str) -> bool:
        existed = self._apps.pop(app_id, None) is not None
        if existed:
            self.save_config()
        return existed

    def _dict_paths(self, app_id: str | None) -> list[str]:
        """Main dictionary plus operator-declared protection dictionaries.

        ``extra_dictionaries`` merges shared, read-only seed lists (e.g. the
        litigation protection seed: courts/arbitration/statutes kept visible)
        into the app's own dictionary without copying entries into it. A
        malformed declaration is a server misconfiguration: fail closed.
        """
        entry = self._apps.get(app_id or "", {})
        dict_path = entry.get("dictionary")
        if not dict_path:
            return []
        extras = entry.get("extra_dictionaries", [])
        if not isinstance(extras, list) or any(not isinstance(p, str) or not p for p in extras):
            raise RegistryConfigurationError(
                "extra dictionaries policy unavailable",
                code="extra_dictionaries_unavailable",
            )
        return [dict_path, *extras]

    def _dict_path(self, app_id: str) -> str | None:
        entry = self._apps.get(app_id or "", {})
        return entry.get("dictionary")

    def load_entries(self, app_id: str) -> list[dict[str, Any]]:
        """Read the app's dictionary entries fresh from disk (for editing)."""
        path = self._dict_path(app_id)
        if not path or not Path(path).exists():
            return []
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []

    def write_entries(self, app_id: str, entries: list[dict[str, Any]]) -> None:
        path = self._dict_path(app_id)
        if not path:
            raise RuntimeError("app has no dictionary file configured")
        Path(path).write_text(
            json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        self.invalidate_dictionary()

    def resolve_profile(self, app_id: str | None, profile_name: str | None = None) -> Profile:
        """Resolve the server-authorized protection floor for an app.

        A request may select a profile only when no ``app_id`` is supplied. If
        an app is named, its registry declaration (or strict fallback) is the
        minimum protection line and cannot be replaced by request data.
        """
        entry = self._apps.get(app_id or "", {})
        try:
            if "profile" in entry:
                configured = _profile_from_spec(entry["profile"])
            elif "scenario" in entry:
                configured = profile_for_scenario(entry["scenario"])
            elif "sensitivity" in entry:
                configured = profile_for_sensitivity(entry["sensitivity"])
            else:
                configured = get_profile("strict")
        except (KeyError, TypeError, ValueError) as exc:
            # A broken registry declaration (typo'd preset name, invalid role
            # set, unknown field) is a SERVER misconfiguration: fail closed
            # like a broken dictionary instead of leaking an unhandled 500.
            raise RegistryConfigurationError(
                "declared profile unavailable", code="profile_unavailable"
            ) from exc

        if profile_name:
            requested = _profile_from_spec(profile_name)
            if app_id:
                if requested.name != configured.name:
                    raise ProfileOverrideDenied("request profile is below the app protection floor")
                # Even a same-named built-in must not replace an inline app
                # policy: the inline policy may carry stricter block/guard
                # settings that are not visible in its name.
                return configured
            return requested
        return configured

    def resolve_dictionary_state(self, app_id: str | None) -> ResolvedDictionary:
        """Resolve a validated dictionary and its raw-byte content version.

        The cache is reused only while every merged file's resolved path,
        mtime_ns and size remain unchanged. A changed but invalid file never
        falls back to the prior successful cache entry.
        """
        dict_paths = self._dict_paths(app_id)
        if not dict_paths:
            return ResolvedDictionary(entries=None, version=None, configured=False)
        try:
            resolved_paths = tuple(str(Path(p).resolve()) for p in dict_paths)
            current_parts = tuple(
                (path, Path(path).stat().st_mtime_ns, Path(path).stat().st_size)
                for path in resolved_paths
            )
        except OSError as exc:
            raise RegistryConfigurationError("declared dictionary unavailable") from exc

        cache_key = "\n".join(resolved_paths)
        with self._dict_cache_lock:
            cached = self._dict_cache.get(cache_key)
            if cached is not None and cached.parts == current_parts:
                return ResolvedDictionary(
                    entries=cached.entries,
                    version=cached.version,
                    configured=True,
                )

            try:
                entries: list[dict[str, Any]] = []
                digest = hashlib.sha256()
                opened_parts: list[tuple[str, int, int]] = []
                for path in resolved_paths:
                    with Path(path).open("rb") as source:
                        opened_stat = os.fstat(source.fileno())
                        raw = source.read()
                    payload = json.loads(raw.decode("utf-8"))
                    if not isinstance(payload, list):
                        raise ValueError("dictionary payload must be a list")
                    DictionaryDetector.from_entries(payload)
                    entries.extend(payload)
                    digest.update(raw)
                    opened_parts.append((path, opened_stat.st_mtime_ns, opened_stat.st_size))
            except (
                OSError,
                UnicodeDecodeError,
                json.JSONDecodeError,
                TypeError,
                ValueError,
            ) as exc:
                raise RegistryConfigurationError("declared dictionary unavailable") from exc

            snapshot = _DictionaryCacheEntry(
                paths=resolved_paths,
                parts=tuple(opened_parts),
                entries=entries,
                version="sha256:" + digest.hexdigest(),
            )
            self._dict_cache[cache_key] = snapshot
            return ResolvedDictionary(
                entries=snapshot.entries,
                version=snapshot.version,
                configured=True,
            )

    def resolve_dictionary(self, app_id: str | None) -> list[dict[str, Any]] | None:
        """Load and validate a declared dictionary, failing closed on errors."""
        return self.resolve_dictionary_state(app_id).entries

    def dictionary_version(self, app_id: str | None) -> str | None:
        return self.resolve_dictionary_state(app_id).version

    def invalidate_dictionary(self, app_id: str | None = None) -> None:
        with self._dict_cache_lock:
            if app_id is None:
                self._dict_cache.clear()
                return
            dict_paths = self._dict_paths(app_id)
            if dict_paths:
                cache_key = "\n".join(str(Path(p).resolve()) for p in dict_paths)
                self._dict_cache.pop(cache_key, None)

    def known_profiles(self) -> dict[str, str]:
        return {name: p.name for name, p in PRESETS.items()}

    def known_apps(self) -> list[str]:
        return sorted(self._apps)

    def authorize(self, app_id: str, capability: str, token: str | None) -> None:
        """Require a server-declared capability and its independent token."""
        entry = self._apps.get(app_id)
        if not isinstance(entry, dict):
            raise CapabilityDenied("unknown app")
        capabilities = entry.get("capabilities", [])
        token_hashes = entry.get("capability_tokens", {})
        if capability not in capabilities or not isinstance(token_hashes, dict):
            raise CapabilityDenied("capability not granted")
        expected = token_hashes.get(capability)
        if not isinstance(token, str) or not token or not isinstance(expected, str):
            raise CapabilityDenied("capability token required")
        actual = hash_capability_token(token)
        if not secrets.compare_digest(actual, expected):
            raise CapabilityDenied("invalid capability token")

    def allowed_refill_contracts(self, app_id: str) -> frozenset[str]:
        entry = self._apps.get(app_id, {})
        values = entry.get("refill_contracts", ["none"])
        if not isinstance(values, list):
            return frozenset({"none"})
        allowed = {str(value) for value in values}
        return frozenset(allowed & {"none", "trusted_display", "exact_transform"})

    def allowed_mapping_scopes(self, app_id: str) -> frozenset[str]:
        entry = self._apps.get(app_id, {})
        values = entry.get("mapping_scopes", ["document"])
        if not isinstance(values, list):
            return frozenset({"document"})
        allowed = {str(value) for value in values}
        return frozenset(allowed & MAPPING_SCOPE_MODES)

    def auto_refill_allowed(self, app_id: str) -> bool:
        """Whether the app's ``/apps/{app_id}/auto/v1/...`` proxy endpoints may
        transparently refill responses for the local client.

        Missing means denied. A non-boolean declaration is a server
        misconfiguration: fail closed instead of guessing the intent.
        """
        entry = self._apps.get(app_id, {})
        value = entry.get("allow_auto_refill", False)
        if not isinstance(value, bool):
            raise RegistryConfigurationError(
                "auto refill policy unavailable",
                code="auto_refill_policy_unavailable",
            )
        return value

    def pinned_upstream_model(self, app_id: str) -> str | None:
        """Model pinned for the app's ``/apps/{app_id}/...`` proxy endpoints.

        When declared, the gateway rewrites the request body's ``model`` field
        to this value before forwarding: clients (e.g. WorkBuddy model entries)
        may then use arbitrary display names while the upstream always receives
        the operator-chosen model, and cannot switch models via the request.
        Missing means pass-through. A non-string declaration is a server
        misconfiguration: fail closed.
        """
        entry = self._apps.get(app_id, {})
        value = entry.get("upstream_model")
        if value is None:
            return None
        if not isinstance(value, str) or not value:
            raise RegistryConfigurationError(
                "upstream model policy unavailable",
                code="upstream_model_policy_unavailable",
            )
        return value

    def allowed_structured_value_labels(self, app_id: str) -> frozenset[str]:
        """Return the app-authorized labels for declared structured values.

        Missing means deny-all. A malformed operator declaration is a server
        configuration error, not a request denial: silently dropping one bad
        label could make the effective policy differ from the reviewed config.
        """
        entry = self._apps.get(app_id, {})
        values = entry.get("structured_value_labels", [])
        if not isinstance(values, list) or any(
            not is_structured_value_label(value) for value in values
        ):
            raise RegistryConfigurationError(
                "structured value policy unavailable",
                code="structured_value_policy_unavailable",
            )
        return frozenset(values)

    def allowed_proxy_routes(self, app_id: str) -> frozenset[str]:
        """Return operator-authorized protocol routes for transparent sessions."""
        entry = self._apps.get(app_id, {})
        values = entry.get("proxy_routes", [])
        if not isinstance(values, list) or any(
            value not in PROXY_UPSTREAM_ENV for value in values
        ):
            raise RegistryConfigurationError(
                "proxy route policy unavailable",
                code="proxy_route_policy_unavailable",
            )
        return frozenset(values)

    def resolve_proxy_upstream(self, app_id: str, route: str) -> str | None:
        """Return one persisted, operator-owned upstream for an app route.

        Provider credentials remain in request headers.  This registry value is
        only the non-secret destination URL used when the process environment
        does not override it.
        """
        if route not in PROXY_UPSTREAM_ENV:
            raise RegistryConfigurationError(
                "proxy upstream unavailable",
                code="proxy_upstream_unavailable",
            )
        entry = self._apps.get(app_id, {})
        if "proxy_upstreams" not in entry:
            return None
        values = entry["proxy_upstreams"]
        if not isinstance(values, dict) or any(
            key not in PROXY_UPSTREAM_ENV for key in values
        ):
            raise RegistryConfigurationError(
                "proxy upstream policy unavailable",
                code="proxy_upstream_policy_unavailable",
            )
        value = values.get(route)
        if value is None:
            return None
        parsed = urlparse(value if isinstance(value, str) else "")
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
        ):
            raise RegistryConfigurationError(
                "proxy upstream unavailable",
                code="proxy_upstream_unavailable",
            )
        return value

    def proxy_targets(self, app_id: str) -> dict[str, dict[str, str]]:
        """Return operator-owned named upstreams for transparent sessions.

        A target binds a stable, non-secret id to both the provider protocol
        and destination URL.  The downstream app selects only the id; it can
        never supply or override the URL carried by a proxy session.
        """
        entry = self._apps.get(app_id, {})
        raw = entry.get("proxy_targets", {})
        if not isinstance(raw, dict):
            raise RegistryConfigurationError(
                "proxy target policy unavailable",
                code="proxy_target_policy_unavailable",
            )
        normalized: dict[str, dict[str, str]] = {}
        target_id_pattern = re.compile(r"[A-Za-z0-9._:-]{1,100}")
        for target_id, spec in raw.items():
            if (
                not isinstance(target_id, str)
                or target_id_pattern.fullmatch(target_id) is None
                or not isinstance(spec, dict)
                or set(spec) != {"route", "upstream"}
            ):
                raise RegistryConfigurationError(
                    "proxy target policy unavailable",
                    code="proxy_target_policy_unavailable",
                )
            route = spec.get("route")
            upstream = spec.get("upstream")
            if route not in PROXY_UPSTREAM_ENV:
                raise RegistryConfigurationError(
                    "proxy target policy unavailable",
                    code="proxy_target_policy_unavailable",
                )
            parsed = urlparse(upstream if isinstance(upstream, str) else "")
            if (
                parsed.scheme not in {"http", "https"}
                or not parsed.hostname
                or parsed.username is not None
                or parsed.password is not None
                or parsed.fragment
            ):
                raise RegistryConfigurationError(
                    "proxy target unavailable",
                    code="proxy_target_unavailable",
                )
            normalized[target_id] = {
                "route": route,
                "upstream": upstream,
            }
        return normalized

    def resolve_proxy_target(
        self, app_id: str, *, target_id: str, route: str
    ) -> str:
        targets = self.proxy_targets(app_id)
        target = targets.get(target_id)
        if target is None or target["route"] != route:
            raise RegistryConfigurationError(
                "proxy target unavailable",
                code="proxy_target_unavailable",
            )
        return target["upstream"]
