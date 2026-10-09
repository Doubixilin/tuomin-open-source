"""Auto-discover the local Tuomin gateway that actually serves a given app.

Why this exists
---------------
URL-only desktop agents (WorkBuddy and friends) and the STDIO MCP adapter
persist a gateway URL, but the Tuomin gateway listens on whatever port its
launcher picked:

- the CLI ``serve`` defaults to ``TUOMIN_PORT`` / 8765;
- the frozen macOS app picks a free port and records it in
  ``~/Library/Application Support/Tuomin/instance.url``;
- launchpad-managed deployments use their own port (for example 8775 for
  ``review-tuomin``).

A hard-coded port therefore rots silently into ``ECONNREFUSED`` → HTTP 502 —
exactly the failure this module prevents. It finds live gateways on localhost
and, crucially, verifies via the side-effect-free ``GET /profiles`` which of
them really has the target app registered, so callers never point a client at a
port that would answer ``404 unknown_app``.
"""
from __future__ import annotations

import json
import os
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

GATEWAY_URL_ENV = "TUOMIN_GATEWAY_URL"
GATEWAY_PORT_ENV = "TUOMIN_PORT"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_SCAN_RANGE = "8760-8799"
DEFAULT_PROBE_TIMEOUT = 0.4
MACOS_INSTANCE_URL = Path.home() / "Library" / "Application Support" / "Tuomin" / "instance.url"


@dataclass
class Gateway:
    """A live Tuomin gateway discovered on localhost."""

    base_url: str
    port: int | None = None
    apps: list[str] = field(default_factory=list)
    apps_known: bool = False
    version: str | None = None
    source: str = "scan"

    def serves(self, app: str) -> bool:
        """True only when we positively confirmed the app is registered."""
        return bool(self.apps_known and app in self.apps)


@dataclass
class Resolution:
    """Outcome of resolving which gateway should back a given app."""

    base_url: str | None
    gateway: Gateway | None
    gateways: list[Gateway]
    verified: bool
    reason: str


def normalize_base_url(value: str | None) -> str:
    """Return a bare ``scheme://host[:port]`` origin (no trailing slash / UI path)."""
    text = str(value or "").strip()
    if not text:
        return ""
    if "://" not in text:
        text = f"http://{text}"
    parts = urlsplit(text)
    path = parts.path.rstrip("/")
    # The packaged app stores ".../ui/"; the gateway origin is the useful part.
    if path.endswith("/ui"):
        path = path[: -len("/ui")]
    if path == "/":
        path = ""
    return urlunsplit((parts.scheme, parts.netloc, path, "", ""))


def port_of(base_url: str) -> int | None:
    try:
        port = urlsplit(base_url).port
    except ValueError:
        return None
    if port:
        return port
    return {"http": 80, "https": 443}.get(urlsplit(base_url).scheme)


def parse_scan_range(spec: str | None) -> list[int]:
    """Parse ``"8760-8799"`` / ``"8765,8775"`` / ``"8775"`` into an ordered, unique list."""
    ports: list[int] = []
    for chunk in str(spec or "").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "-" in chunk:
            start_text, end_text = chunk.split("-", 1)
            start, end = int(start_text), int(end_text)
            if start > end:
                start, end = end, start
            ports.extend(range(start, end + 1))
        else:
            ports.append(int(chunk))
    seen: set[int] = set()
    ordered: list[int] = []
    for port in ports:
        if port not in seen:
            seen.add(port)
            ordered.append(port)
    return ordered


def _http_get_json(url: str, timeout: float) -> object:
    request = urllib.request.Request(url, headers={"Accept": "application/json"}, method="GET")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read(65536).decode("utf-8", "replace"))


def _build_probe(probe, get_json):
    if probe is not None:
        return probe
    fetch = get_json or _http_get_json

    def probe_fn(url: str, *, timeout: float = DEFAULT_PROBE_TIMEOUT, source: str = "scan"):
        return probe_gateway(url, timeout=timeout, source=source, get_json=fetch)

    return probe_fn


def _scan_ports_for(scan_ports: list[int] | None, allow_scan: bool) -> list[int]:
    """Explicit list wins; ``None`` + scanning allowed means the default range."""
    if not allow_scan:
        return []
    if scan_ports is None:
        return parse_scan_range(DEFAULT_SCAN_RANGE)
    return list(scan_ports)


def probe_gateway(
    base_url: str,
    *,
    timeout: float = DEFAULT_PROBE_TIMEOUT,
    source: str = "scan",
    get_json=None,
) -> Gateway | None:
    """Return a Gateway if ``base_url`` answers like a Tuomin service, else None.

    The Tuomin signature is ``GET /healthz`` → ``{"status": "ok", "ner_available": ...}``.
    App membership is read from ``GET /profiles`` (side-effect free — it never
    triggers an upstream model call).
    """
    fetch = get_json or _http_get_json
    base = normalize_base_url(base_url)
    if not base:
        return None
    try:
        health = fetch(f"{base}/healthz", timeout)
    except Exception:
        return None
    if not isinstance(health, dict) or health.get("status") != "ok" or "ner_available" not in health:
        return None

    apps: list[str] = []
    apps_known = False
    try:
        profiles = fetch(f"{base}/profiles", timeout)
        if isinstance(profiles, dict) and isinstance(profiles.get("apps"), list):
            apps = [str(app) for app in profiles["apps"]]
            apps_known = True
    except Exception:
        apps_known = False

    return Gateway(
        base_url=base,
        port=port_of(base),
        apps=apps,
        apps_known=apps_known,
        version=health.get("version") if isinstance(health.get("version"), str) else None,
        source=source,
    )


def macos_instance_url(path: Path | None = None) -> str | None:
    """Read the packaged macOS app's recorded instance URL, if present."""
    target = path or MACOS_INSTANCE_URL
    try:
        text = target.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return normalize_base_url(text) or None


def candidate_sources(
    *,
    base_url: str | None = None,
    port: int | None = None,
    env: dict | None = None,
    instance_url: str | None = None,
    scan_ports: list[int] | None = None,
    host: str = DEFAULT_HOST,
) -> list[tuple[str, str]]:
    """Ordered ``(base_url, source)`` candidates: explicit → env → instance.url → scan."""
    environment = os.environ if env is None else env
    seen: set[str] = set()
    candidates: list[tuple[str, str]] = []

    def add(url: str | None, source: str) -> None:
        normalized = normalize_base_url(url)
        if normalized and normalized not in seen:
            seen.add(normalized)
            candidates.append((normalized, source))

    add(base_url, "explicit")
    if port:
        add(f"http://{host}:{int(port)}", "explicit-port")
    add(environment.get(GATEWAY_URL_ENV), f"env:{GATEWAY_URL_ENV}")
    env_port = environment.get(GATEWAY_PORT_ENV)
    if env_port:
        try:
            add(f"http://{host}:{int(str(env_port).strip())}", f"env:{GATEWAY_PORT_ENV}")
        except (TypeError, ValueError):
            pass
    add(instance_url if instance_url is not None else macos_instance_url(), "macos-instance")
    for candidate_port in scan_ports or ():
        add(f"http://{host}:{int(candidate_port)}", "scan")
    return candidates


def discover_gateways(
    candidates: list[tuple[str, str]],
    *,
    timeout: float = DEFAULT_PROBE_TIMEOUT,
    probe=None,
    max_workers: int = 16,
) -> list[Gateway]:
    """Probe candidates concurrently and return live gateways sorted by port."""
    probe_fn = _build_probe(probe, None)
    if not candidates:
        return []

    def work(item: tuple[str, str]) -> Gateway | None:
        url, source = item
        return probe_fn(url, timeout=timeout, source=source)

    found: list[Gateway] = []
    workers = max(1, min(max_workers, len(candidates)))
    with ThreadPoolExecutor(max_workers=workers) as executor:
        for gateway in executor.map(work, candidates):
            if gateway is not None:
                found.append(gateway)
    found.sort(key=lambda gateway: (gateway.port if gateway.port is not None else 1 << 30, gateway.base_url))
    return found


def resolve_gateway(
    app: str,
    *,
    base_url: str | None = None,
    port: int | None = None,
    env: dict | None = None,
    instance_url: str | None = None,
    scan_ports: list[int] | None = None,
    allow_scan: bool = True,
    timeout: float = DEFAULT_PROBE_TIMEOUT,
    probe=None,
    host: str = DEFAULT_HOST,
    get_json=None,
) -> Resolution:
    """Pick the gateway that serves ``app``.

    An explicit ``base_url`` / ``port`` short-circuits discovery (the operator
    said so); it is still probed only to warn when the app is not registered.
    Otherwise candidates are probed and the lowest-port gateway that positively
    serves ``app`` wins. When nothing serves it, ``base_url`` is None so the
    caller can fail loudly instead of writing a URL that will 404.
    """
    probe_fn = _build_probe(probe, get_json)

    if base_url or port:
        target = normalize_base_url(base_url) if base_url else f"http://{host}:{int(port)}"
        gateway = probe_fn(target, timeout=timeout, source="explicit")
        if gateway is not None and gateway.serves(app):
            return Resolution(target, gateway, [gateway], True, "explicit")
        if gateway is not None and not gateway.apps_known:
            return Resolution(target, gateway, [gateway], False, "explicit (app membership unverified)")
        if gateway is not None:
            return Resolution(
                target, gateway, [gateway], False,
                f"explicit, but app {app!r} is not registered (apps={gateway.apps})",
            )
        return Resolution(target, None, [], False, "explicit (gateway not reachable)")

    ports = _scan_ports_for(scan_ports, allow_scan)
    candidates = candidate_sources(
        env=env, instance_url=instance_url, scan_ports=ports, host=host,
    )
    gateways = discover_gateways(candidates, timeout=timeout, probe=probe_fn)

    matches = [gateway for gateway in gateways if gateway.serves(app)]
    if matches:
        best = matches[0]
        return Resolution(best.base_url, best, gateways, True, f"auto-detected ({best.source})")

    unverified = [gateway for gateway in gateways if not gateway.apps_known]
    if unverified:
        best = unverified[0]
        return Resolution(best.base_url, best, gateways, False, "auto-detected (app membership unverified)")

    return Resolution(None, None, gateways, False, f"no live gateway serves app {app!r}")


def resolve_gateway_url(
    app: str | None = None,
    *,
    base_url: str | None = None,
    port: int | None = None,
    env: dict | None = None,
    instance_url: str | None = None,
    scan_ports: list[int] | None = None,
    allow_scan: bool = True,
    timeout: float = DEFAULT_PROBE_TIMEOUT,
    probe=None,
    host: str = DEFAULT_HOST,
    get_json=None,
) -> Resolution:
    """Best-effort gateway URL for a client that may not know its app yet.

    Prefers the gateway serving ``app``; when the app is unknown/unconfirmed it
    falls back to the lowest-port live gateway (so an MCP adapter still starts),
    and only returns ``base_url=None`` when nothing is reachable and nothing was
    passed explicitly.
    """
    kwargs = dict(
        base_url=base_url, port=port, env=env, instance_url=instance_url,
        scan_ports=scan_ports, allow_scan=allow_scan, timeout=timeout,
        probe=probe, host=host, get_json=get_json,
    )
    if app:
        resolution = resolve_gateway(app, **kwargs)
        if resolution.base_url and (resolution.verified or base_url or port):
            return resolution
        fallback = _best_any_gateway(**kwargs)
        if fallback.base_url:
            fallback.reason = f"{fallback.reason}; app {app!r} not confirmed"
            return fallback
        return resolution
    return _best_any_gateway(**kwargs)


def _best_any_gateway(
    *,
    base_url: str | None = None,
    port: int | None = None,
    env: dict | None = None,
    instance_url: str | None = None,
    scan_ports: list[int] | None = None,
    allow_scan: bool = True,
    timeout: float = DEFAULT_PROBE_TIMEOUT,
    probe=None,
    host: str = DEFAULT_HOST,
    get_json=None,
) -> Resolution:
    probe_fn = _build_probe(probe, get_json)
    if base_url or port:
        target = normalize_base_url(base_url) if base_url else f"http://{host}:{int(port)}"
        gateway = probe_fn(target, timeout=timeout, source="explicit")
        verified = gateway is not None and gateway.apps_known
        return Resolution(target, gateway, [gateway] if gateway else [], verified, "explicit")

    ports = _scan_ports_for(scan_ports, allow_scan)
    candidates = candidate_sources(env=env, instance_url=instance_url, scan_ports=ports, host=host)
    gateways = discover_gateways(candidates, timeout=timeout, probe=probe_fn)
    if not gateways:
        return Resolution(None, None, [], False, "no live Tuomin gateway found")
    best = gateways[0]
    return Resolution(best.base_url, best, gateways, best.apps_known, f"auto-detected ({best.source})")


def describe_gateways(gateways: list[Gateway]) -> str:
    """Human-readable list for errors and diagnostics."""
    if not gateways:
        return "  (no live Tuomin gateway found)"
    lines = []
    for gateway in gateways:
        apps = ", ".join(gateway.apps) if gateway.apps_known else "(apps unknown)"
        version = f" v{gateway.version}" if gateway.version else ""
        lines.append(f"  - {gateway.base_url}{version} [port {gateway.port}] apps: {apps}")
    return "\n".join(lines)
