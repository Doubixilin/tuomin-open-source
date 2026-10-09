"""WorkBuddy custom-model entry logic (pure, no network).

WorkBuddy's user-level ``models.json`` is a plain list of custom-model entries.
Tuomin's project endpoints encode the refill mode in the URL path, so one app
maps to three entries:

- ``masked``: ``…/apps/{app}/v1/chat/completions``      (answer keeps placeholders)
- ``demo``:   ``…/apps/{app}/demo/v1/chat/completions`` (reasoning masked, body refilled)
- ``auto``:   ``…/apps/{app}/auto/v1/chat/completions`` (seamless refill)

Because WorkBuddy dedupes custom models by name, the display name may be
arbitrary — the gateway pins the real upstream model via the registry's
``upstream_model``.

This module is deliberately dependency-free (stdlib only) so it can be reused
by the CLI, the admin API, the WebUI, and tests. Gateway discovery lives in
``gateway_discovery``; orchestration in ``workbuddy_onboarding``.
"""
from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

MODES = {
    "masked": ("{base}/apps/{app}/v1/chat/completions", "{name}·脱敏·全mask"),
    "demo": ("{base}/apps/{app}/demo/v1/chat/completions", "{name}·脱敏·思维链mask"),
    "auto": ("{base}/apps/{app}/auto/v1/chat/completions", "{name}·脱敏·无感回填"),
}

# WorkBuddy models.json written by the GUI uses real booleans, but the schema
# also accepts the string form; keep emitting the GUI-compatible string form.
PLACEHOLDER_API_KEY = "EXAMPLE_ONLY_NO_SECRET"


def build_entries(app: str, base_url: str, api_key: str, modes: list[str]) -> list[dict]:
    entries = []
    for mode in modes:
        url_t, name_t = MODES[mode]
        entries.append(
            {
                "id": name_t.format(name=app),
                "name": name_t.format(name=app),
                "vendor": "Custom",
                "url": url_t.format(base=base_url.rstrip("/"), app=app),
                "apiKey": api_key,
                "supportsToolCall": "True",
                "supportsImages": "False",
                "supportsReasoning": "True",
                "useCustomProtocol": "False",
                "reasoning": "{'defaultEffort': 'high'}",
            }
        )
    return entries


def mode_from_url(url: object, app: str) -> str | None:
    """Return 'masked' | 'demo' | 'auto' when ``url`` is this app's project endpoint."""
    if not isinstance(url, str) or not app:
        return None
    path = urlsplit(url).path
    prefix = f"/apps/{app}/"
    if not path.startswith(prefix):
        return None
    remainder = path[len(prefix):]
    for mode in ("demo", "auto"):
        if remainder.startswith(f"{mode}/v1/"):
            return mode
    if remainder.startswith("v1/"):
        return "masked"
    return None


def retarget_entries(existing: list, app: str, base_url: str) -> list[dict]:
    """Repoint this app's existing entries at ``base_url``; return the changed ones.

    Identity (id/name/apiKey/…) is preserved — only the origin changes — so a
    gateway port move is healed without clobbering GUI tweaks.
    """
    base = urlsplit(base_url)
    changed: list[dict] = []
    for entry in existing:
        if not isinstance(entry, dict) or mode_from_url(entry.get("url"), app) is None:
            continue
        parts = urlsplit(entry["url"])
        new_url = urlunsplit((base.scheme, base.netloc, parts.path, parts.query, parts.fragment))
        if new_url != entry["url"]:
            entry["url"] = new_url
            changed.append(entry)
    return changed


def merge_entries(existing: list, new_entries: list[dict], *, app: str | None = None) -> list:
    """Idempotent merge.

    With ``app`` set, an existing entry already pointing at the same app+mode is
    updated in place (kept as the same WorkBuddy model) instead of appending a
    duplicate. Without ``app``, the original by-id upsert behavior is preserved.
    """
    result = [dict(entry) for entry in existing if isinstance(entry, dict)]
    by_id = {entry.get("id"): entry for entry in result}
    by_mode: dict[str, dict] = {}
    if app:
        for entry in result:
            mode = mode_from_url(entry.get("url"), app)
            if mode and mode not in by_mode:
                by_mode[mode] = entry

    for entry in new_entries:
        target = None
        if app:
            target = by_mode.get(mode_from_url(entry.get("url"), app))
        if target is not None:
            # Same logical model: keep its identity, refresh the URL, and only
            # adopt other fields when a real (non-placeholder) key is supplied.
            target["url"] = entry["url"]
            new_key = entry.get("apiKey")
            if new_key and new_key != PLACEHOLDER_API_KEY:
                target["apiKey"] = new_key
            continue
        if entry["id"] in by_id:
            current = by_id[entry["id"]]
            new_key = entry.get("apiKey")
            if (not new_key or new_key == PLACEHOLDER_API_KEY) and current.get("apiKey"):
                entry = {**entry, "apiKey": current["apiKey"]}
            current.update(entry)
        else:
            result.append(entry)
            by_id[entry["id"]] = entry
            if app:
                mode = mode_from_url(entry.get("url"), app)
                if mode:
                    by_mode[mode] = entry
    return result


def load_models(path: Path | str) -> list:
    """Load WorkBuddy's user-level models.json (tolerates the ``{"models": [...]}`` form)."""
    target = Path(path)
    if not target.exists():
        return []
    data = json.loads(target.read_text(encoding="utf-8"))
    if isinstance(data, list):
        return data
    models = data.get("models") if isinstance(data, dict) else None
    return models if isinstance(models, list) else []


def entries_for_app(existing: list, app: str) -> list[dict]:
    """Existing entries that point at this app's project endpoints."""
    return [e for e in existing if isinstance(e, dict) and mode_from_url(e.get("url"), app)]
