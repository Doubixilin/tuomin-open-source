"""Tests for local gateway auto-discovery (tuomin_gateway.gateway_discovery)."""
from __future__ import annotations

import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))

from tuomin_gateway.gateway_discovery import (  # noqa: E402
    Gateway,
    candidate_sources,
    describe_gateways,
    discover_gateways,
    normalize_base_url,
    parse_scan_range,
    port_of,
    probe_gateway,
    resolve_gateway,
    resolve_gateway_url,
)


def _gateway(url: str, apps: list[str] | None = None, *, known: bool = True, source: str = "scan") -> Gateway:
    base = normalize_base_url(url)
    return Gateway(base_url=base, port=port_of(base), apps=list(apps or []), apps_known=known, source=source)


def _probe_from(mapping: dict[str, Gateway]):
    def probe(url: str, *, timeout: float = 0.4, source: str = "scan"):
        return mapping.get(normalize_base_url(url))
    return probe


# --- pure helpers ---------------------------------------------------------

def test_normalize_base_url_strips_ui_and_adds_scheme():
    assert normalize_base_url("127.0.0.1:8767") == "http://127.0.0.1:8767"
    assert normalize_base_url("http://127.0.0.1:60787/ui/") == "http://127.0.0.1:60787"
    assert normalize_base_url("http://127.0.0.1:8775/") == "http://127.0.0.1:8775"
    assert normalize_base_url("") == ""


def test_parse_scan_range_handles_ranges_lists_and_dupes():
    assert parse_scan_range("8760-8763") == [8760, 8761, 8762, 8763]
    assert parse_scan_range("8765,8775,8765") == [8765, 8775]
    assert parse_scan_range("8799-8797") == [8797, 8798, 8799]
    assert parse_scan_range("") == []


def test_candidate_sources_priority_and_dedupe():
    candidates = candidate_sources(
        base_url="http://127.0.0.1:8775",
        env={"TUOMIN_GATEWAY_URL": "http://127.0.0.1:8765", "TUOMIN_PORT": "8775"},
        instance_url="http://127.0.0.1:60787/ui/",
        scan_ports=[8765, 8775, 9000],
    )
    urls = [url for url, _ in candidates]
    assert urls == [
        "http://127.0.0.1:8775",   # explicit wins, and dedupes env/scan
        "http://127.0.0.1:8765",   # env URL
        "http://127.0.0.1:60787",  # packaged instance.url
        "http://127.0.0.1:9000",   # scan
    ]
    sources = dict(candidates)
    assert sources["http://127.0.0.1:8775"] == "explicit"


def test_discover_gateways_filters_dead_and_sorts_by_port():
    mapping = {
        "http://127.0.0.1:8775": _gateway("http://127.0.0.1:8775", ["wb_test"]),
        "http://127.0.0.1:8765": _gateway("http://127.0.0.1:8765", ["other"]),
    }
    found = discover_gateways(
        [("http://127.0.0.1:8765", "scan"), ("http://127.0.0.1:8766", "scan"),
         ("http://127.0.0.1:8775", "scan")],
        probe=_probe_from(mapping),
    )
    assert [g.port for g in found] == [8765, 8775]


# --- probing --------------------------------------------------------------

def test_probe_gateway_reads_healthz_and_profiles():
    def get_json(url: str, timeout: float):
        if url.endswith("/healthz"):
            return {"status": "ok", "ner_available": True, "version": "0.1.0"}
        if url.endswith("/profiles"):
            return {"apps": ["research_client", "wb_test"]}
        raise AssertionError(url)

    gateway = probe_gateway("127.0.0.1:8775/ui/", get_json=get_json)
    assert gateway is not None
    assert gateway.base_url == "http://127.0.0.1:8775"
    assert gateway.port == 8775
    assert gateway.version == "0.1.0"
    assert gateway.serves("wb_test")
    assert not gateway.serves("nope")


def test_probe_gateway_rejects_non_tuomin_service():
    def get_json(url: str, timeout: float):
        return {"status": "ok"}  # missing the ner_available marker

    assert probe_gateway("http://127.0.0.1:8766", get_json=get_json) is None


def test_probe_gateway_tolerates_missing_profiles():
    def get_json(url: str, timeout: float):
        if url.endswith("/healthz"):
            return {"status": "ok", "ner_available": False}
        raise RuntimeError("no /profiles here")

    gateway = probe_gateway("http://127.0.0.1:9999", get_json=get_json)
    assert gateway is not None
    assert gateway.apps_known is False


# --- resolution -----------------------------------------------------------

def test_resolve_gateway_picks_the_port_that_serves_the_app():
    mapping = {
        "http://127.0.0.1:8765": _gateway("http://127.0.0.1:8765", ["macos_contract_demo"]),
        "http://127.0.0.1:8775": _gateway("http://127.0.0.1:8775", ["research_client", "wb_test"]),
    }
    resolution = resolve_gateway(
        "wb_test", env={}, instance_url="", scan_ports=[8765, 8775], probe=_probe_from(mapping),
    )
    assert resolution.base_url == "http://127.0.0.1:8775"
    assert resolution.verified is True
    assert resolution.gateway is not None and resolution.gateway.serves("wb_test")


def test_resolve_gateway_returns_none_when_no_gateway_serves_app():
    mapping = {"http://127.0.0.1:8765": _gateway("http://127.0.0.1:8765", ["macos_contract_demo"])}
    resolution = resolve_gateway(
        "wb_test", env={}, instance_url="", scan_ports=[8765], probe=_probe_from(mapping),
    )
    assert resolution.base_url is None
    assert resolution.verified is False
    assert "no live gateway serves" in resolution.reason
    assert [g.port for g in resolution.gateways] == [8765]


def test_resolve_gateway_explicit_short_circuits_scan():
    calls: list[str] = []

    def probe(url: str, *, timeout: float = 0.4, source: str = "scan"):
        calls.append(url)
        return _gateway(url, ["wb_test"])

    resolution = resolve_gateway(
        "wb_test", base_url="http://127.0.0.1:8899", env={}, instance_url="",
        scan_ports=[8765, 8775], probe=probe,
    )
    assert resolution.base_url == "http://127.0.0.1:8899"
    assert resolution.verified is True
    assert calls == ["http://127.0.0.1:8899"]  # scan never ran


def test_resolve_gateway_explicit_warns_when_app_missing_but_still_returns():
    def probe(url: str, *, timeout: float = 0.4, source: str = "scan"):
        return _gateway(url, ["other_app"])

    resolution = resolve_gateway(
        "wb_test", port=8899, env={}, instance_url="", probe=probe,
    )
    assert resolution.base_url == "http://127.0.0.1:8899"
    assert resolution.verified is False
    assert "not registered" in resolution.reason


def test_describe_gateways_is_human_readable():
    text = describe_gateways([
        _gateway("http://127.0.0.1:8775", ["wb_test"]),
        Gateway(base_url="http://127.0.0.1:9000", port=9000, apps_known=False),
    ])
    assert "http://127.0.0.1:8775" in text
    assert "wb_test" in text
    assert "(apps unknown)" in text
    assert "no live Tuomin gateway" in describe_gateways([])


# --- resolve_gateway_url: best-effort default for MCP/agents ---------------

def test_resolve_gateway_url_prefers_verified_app_match():
    mapping = {
        "http://127.0.0.1:8765": _gateway("http://127.0.0.1:8765", ["other"]),
        "http://127.0.0.1:8775": _gateway("http://127.0.0.1:8775", ["wb_test"]),
    }
    resolution = resolve_gateway_url(
        "wb_test", env={}, instance_url="", scan_ports=[8765, 8775], probe=_probe_from(mapping),
    )
    assert resolution.base_url == "http://127.0.0.1:8775"
    assert resolution.verified is True


def test_resolve_gateway_url_falls_back_to_any_live_gateway():
    mapping = {"http://127.0.0.1:8765": _gateway("http://127.0.0.1:8765", ["macos_demo"])}
    resolution = resolve_gateway_url(
        "wb_test", env={}, instance_url="", scan_ports=[8765], probe=_probe_from(mapping),
    )
    assert resolution.base_url == "http://127.0.0.1:8765"
    assert "not confirmed" in resolution.reason


def test_resolve_gateway_url_without_app_picks_lowest_port():
    mapping = {
        "http://127.0.0.1:8775": _gateway("http://127.0.0.1:8775", ["wb_test"]),
        "http://127.0.0.1:8765": _gateway("http://127.0.0.1:8765", ["other"]),
    }
    resolution = resolve_gateway_url(
        env={}, instance_url="", scan_ports=[8775, 8765], probe=_probe_from(mapping),
    )
    assert resolution.base_url == "http://127.0.0.1:8765"


def test_resolve_gateway_url_none_when_nothing_live():
    resolution = resolve_gateway_url(
        "wb_test", env={}, instance_url="", scan_ports=[8765], probe=lambda *a, **k: None,
    )
    assert resolution.base_url is None
    assert resolution.verified is False
