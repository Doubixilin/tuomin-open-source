from __future__ import annotations

import socket

import pytest


@pytest.fixture(autouse=True)
def explicit_plaintext_mapping_store_for_synthetic_tests(monkeypatch):
    """Unit/integration fixtures use temporary synthetic mappings only.

    macOS production defaults remain Keychain-backed; tests must opt in to the
    plaintext development cipher so they never touch a developer's Keychain.
    """

    monkeypatch.setenv("TUOMIN_ALLOW_PLAINTEXT_STORE", "1")


@pytest.fixture(autouse=True)
def no_real_dns_in_tests(monkeypatch):
    """Tests never make real DNS requests.

    The proxy SSRF guard resolves upstream hostnames via getaddrinfo; by
    default every hostname resolves to a documentation public IP. A test that
    needs a specific answer (private IP, NXDOMAIN, ...) monkeypatches
    ``socket.getaddrinfo`` again — the last patch wins.
    """

    def fake_getaddrinfo(host, port=0, *args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port or 0))]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
