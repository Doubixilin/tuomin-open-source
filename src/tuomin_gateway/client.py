"""Small synchronous client for the local capability-protected `/api/v1`."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


_SAFE_LABEL_RE = re.compile(r"[A-Z][A-Z0-9_]{0,63}")


@dataclass
class TuominApiError(RuntimeError):
    status_code: int
    code: str
    message: str

    def __str__(self) -> str:
        return f"Tuomin API {self.status_code} {self.code}: {self.message}"


class TuominEgressDenied(RuntimeError):
    """Safe local policy error raised before a consumer sends masked output."""

    code = "egress_denied"

    def __init__(self, blocked_labels: tuple[str, ...] = ()) -> None:
        self.blocked_labels = tuple(
            sorted(
                {
                    label
                    for label in blocked_labels
                    if isinstance(label, str)
                    and _SAFE_LABEL_RE.fullmatch(label) is not None
                }
            )
        )
        suffix = (
            f"; blocked labels: {','.join(self.blocked_labels)}"
            if self.blocked_labels
            else ""
        )
        super().__init__(f"Tuomin egress denied{suffix}")


class TuominClient:
    def __init__(
        self,
        *,
        app_id: str,
        capability_tokens: dict[str, str],
        base_url: str = "http://127.0.0.1:8765",
        http_client: Any | None = None,
    ) -> None:
        self.app_id = app_id
        self.capability_tokens = dict(capability_tokens)
        self._owns_client = http_client is None
        if http_client is None:
            import httpx

            http_client = httpx.Client(base_url=base_url, timeout=30)
        self.http = http_client

    def close(self) -> None:
        if self._owns_client:
            self.http.close()

    def __enter__(self) -> "TuominClient":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    def _request(
        self,
        method: str,
        path: str,
        capability: str,
        *,
        json: dict | None = None,
        params: dict | None = None,
    ) -> dict:
        token = self.capability_tokens.get(capability)
        headers = {"x-tuomin-capability-token": token or ""}
        response = self.http.request(
            method, path, headers=headers, json=json, params=params
        )
        payload = response.json()
        if response.status_code >= 400:
            error = payload.get("error", {})
            raise TuominApiError(
                response.status_code,
                str(error.get("code", "request_failed")),
                str(error.get("message", "request failed")),
            )
        return payload

    def detect(self, text: str) -> dict:
        return self._request(
            "POST", "/api/v1/detect", "detect", json={"app_id": self.app_id, "text": text}
        )

    def redact(self, text: str) -> dict:
        return self._request(
            "POST", "/api/v1/redact", "redact", json={"app_id": self.app_id, "text": text}
        )

    def redact_batch(
        self,
        items: list[dict[str, str]],
        *,
        namespace_id: str | None = None,
    ) -> dict:
        payload: dict[str, Any] = {"app_id": self.app_id, "items": items}
        capability = "redact"
        if namespace_id is not None:
            payload["namespace_id"] = namespace_id
            capability = "namespace"
        return self._request(
            "POST",
            "/api/v1/redact/batch",
            capability,
            json=payload,
        )

    def redact_values(
        self,
        namespace_id: str,
        items: list[dict[str, str]],
    ) -> dict:
        return self._request(
            "POST",
            "/api/v1/redact/values",
            "namespace",
            json={
                "app_id": self.app_id,
                "namespace_id": namespace_id,
                "items": items,
            },
        )

    def assert_egress_allowed(self, payload: object) -> dict:
        labels: tuple[str, ...] = ()
        labels_valid = False
        if isinstance(payload, dict):
            raw_labels = payload.get("blocked_labels")
            labels_valid = isinstance(raw_labels, list) and all(
                isinstance(label, str)
                and _SAFE_LABEL_RE.fullmatch(label) is not None
                for label in raw_labels
            )
            if labels_valid:
                labels = tuple(raw_labels)
            if (
                payload.get("status") == "ok"
                and payload.get("egress_allowed") is True
                and labels_valid
                and not labels
            ):
                return payload
        raise TuominEgressDenied(labels)

    def refill(self, mapping_handle: str, text: str, *, contract: str) -> dict:
        return self._request(
            "POST",
            "/api/v1/refill",
            "trusted_refill",
            json={
                "app_id": self.app_id,
                "mapping_handle": mapping_handle,
                "text": text,
                "contract": contract,
            },
        )

    def refill_structured(
        self,
        mapping_handle: str,
        value: object,
        *,
        contract: str = "trusted_display",
    ) -> dict:
        return self._request(
            "POST",
            "/api/v1/refill/structured",
            "trusted_refill",
            json={
                "app_id": self.app_id,
                "mapping_handle": mapping_handle,
                "value": value,
                "contract": contract,
            },
        )

    def app_readiness(self) -> dict:
        return self._request(
            "GET",
            "/api/v1/readiness",
            "namespace",
            params={"app_id": self.app_id},
        )

    def create_namespace(self) -> dict:
        return self._request(
            "POST", "/api/v1/namespaces", "namespace", json={"app_id": self.app_id}
        )

    def namespace_status(self, namespace_id: str) -> dict:
        return self._request(
            "GET",
            f"/api/v1/namespaces/{namespace_id}",
            "namespace",
            params={"app_id": self.app_id},
        )

    def namespace_redact(self, namespace_id: str, text: str) -> dict:
        return self._request(
            "POST",
            f"/api/v1/namespaces/{namespace_id}/redact",
            "namespace",
            json={"app_id": self.app_id, "text": text},
        )

    def archive_namespace(self, namespace_id: str) -> dict:
        return self._request(
            "POST",
            f"/api/v1/namespaces/{namespace_id}/archive",
            "namespace",
            json={"app_id": self.app_id},
        )

    def delete_namespace(self, namespace_id: str) -> dict:
        return self._request(
            "DELETE",
            f"/api/v1/namespaces/{namespace_id}",
            "namespace",
            params={"app_id": self.app_id},
        )

    def create_inspection_snapshot(
        self,
        namespace_id: str,
        *,
        project_id: str,
        run_id: str,
        scope: str,
        terminal_status: str,
        coverage_items: list[dict[str, str]],
    ) -> dict:
        return self._request(
            "POST",
            f"/api/v1/namespaces/{namespace_id}/inspection-snapshots",
            "mapping_inspect",
            json={
                "app_id": self.app_id,
                "project_id": project_id,
                "run_id": run_id,
                "scope": scope,
                "terminal_status": terminal_status,
                "coverage_items": coverage_items,
            },
        )

    def query_inspection(
        self,
        inspection_id: str,
        *,
        category: str = "entries",
        offset: int = 0,
        limit: int = 50,
        search: str = "",
    ) -> dict:
        return self._request(
            "POST",
            f"/api/v1/inspections/{inspection_id}/query",
            "mapping_inspect",
            json={
                "app_id": self.app_id,
                "category": category,
                "offset": offset,
                "limit": limit,
                "search": search,
            },
        )

    def delete_inspection(self, inspection_id: str) -> dict:
        return self._request(
            "DELETE",
            f"/api/v1/inspections/{inspection_id}",
            "mapping_inspect",
            params={"app_id": self.app_id},
        )

    def create_proxy_session(
        self,
        *,
        project_id: str,
        run_id: str,
        thread_id: str,
        provider_route: str,
        upstream_id: str | None = None,
    ) -> dict:
        payload = {
            "app_id": self.app_id,
            "project_id": project_id,
            "run_id": run_id,
            "thread_id": thread_id,
            "provider_route": provider_route,
        }
        if upstream_id is not None:
            payload["upstream_id"] = upstream_id
        return self._request(
            "POST",
            "/api/v1/proxy-sessions",
            "proxy_session",
            json=payload,
        )

    def proxy_targets(self) -> dict:
        return self._request(
            "GET",
            "/api/v1/proxy-targets",
            "proxy_session",
            params={"app_id": self.app_id},
        )

    def proxy_session_status(
        self, proxy_session_id: str, proxy_session_token: str
    ) -> dict:
        response = self.http.request(
            "GET",
            f"/api/v1/proxy-sessions/{proxy_session_id}",
            headers={
                "x-tuomin-proxy-session-token": proxy_session_token
            },
        )
        return self._proxy_session_response(response)

    def close_proxy_session(
        self, proxy_session_id: str, proxy_session_token: str
    ) -> dict:
        response = self.http.request(
            "POST",
            f"/api/v1/proxy-sessions/{proxy_session_id}/close",
            headers={
                "x-tuomin-proxy-session-token": proxy_session_token
            },
        )
        return self._proxy_session_response(response)

    @staticmethod
    def _proxy_session_response(response: Any) -> dict:
        payload = response.json()
        if response.status_code >= 400:
            error = payload.get("error", {})
            raise TuominApiError(
                response.status_code,
                str(error.get("code", "request_failed")),
                str(error.get("message", "request failed")),
            )
        return payload
