# Dify Local Gateway Contract

This document describes a synthetic-only Dify HTTP Request node contract for the
local Tuomin Gateway at `127.0.0.1`. It does not require Dify plugins, model
downloads, or external detector runtimes.

## Existing Local HTTP Surface

`tuomin-gateway serve --host 127.0.0.1 --port 8765` exposes these local
surfaces:

- `GET /healthz`: service health and version.
- `GET /readiness`: required NER load and declared-dictionary readiness; use for
  traffic admission.
- `POST /redact`: one-shot redaction. Persists mapping locally under the
  configured `MappingStore`. The server always mints the `task_id`;
  client-supplied `task_id` is rejected with `400` (cross-tenant overwrite
  protection).
- `POST /refill`: local refill by `task_id`; returns `blocked` when placeholder
  integrity fails and `404` when mapping is missing or expired.
- `POST /redact_snippet`: in-memory masking for small previews; no mapping is
  persisted.
- `POST /session/open`, `POST /session/{sid}/mask`,
  `POST /session/{sid}/refill`, `POST /session/{sid}/unmask`,
  `DELETE /session/{sid}`: session-stable placeholders for agent workflows.
- `GET /profiles`: visible profiles and registered apps.
- `POST /dict/reload`: local dictionary reload; admin token protected.
- `GET/PUT/DELETE /admin/...`: local WebUI/admin app and dictionary management;
  admin token protected.
- `POST /v1/messages`, `POST /v1/chat/completions`: reverse proxy endpoints for
  Anthropic-style and OpenAI-style upstream APIs.

For Dify workflow nodes, prefer the simple `POST /redact` then `POST /refill`
contract below.

## Redact HTTP Request Node

Configure a Dify HTTP Request node:

- Method: `POST`
- URL: `http://127.0.0.1:8765/redact`
- Body type: JSON
- Body example: `examples/dify-redact-request.json`

```json
{
  "app_id": "dify_synthetic_demo",
  "text": "Synthetic Team Alpha contact 13900001111 for the local gateway demo."
}
```

Do NOT send a client-chosen `task_id` — the gateway rejects it with `400`
(a client-chosen id could overwrite another tenant's mapping or be trivially
guessable at refill time). The server mints the id; route the `task_id` from
the response into Dify variables for the refill node.

Register the synthetic app locally before running the example:

```json
{"apps": {"dify_synthetic_demo": {"profile": "kb"}}}
```

The registry owns the minimum detector policy. A workflow request must not use
`profile` or `use_ner=false` to weaken a registered app.

The response contains safe fields for workflow routing:

```json
{
  "task_id": "task_1f2a3b4c5d6e",
  "redacted_text": "Synthetic Team Alpha contact <CONTACT_001> for the local gateway demo.",
  "mapping_id": "task_1f2a3b4c5d6e.mapping.json",
  "risk_summary": {"total": 1, "by_label": {"CONTACT": 1}},
  "placeholder_count": 1,
  "blocked_labels": [],
  "profile": "kb",
  "detectors": {
    "required": ["rule"],
    "active": ["rule"],
    "degraded": false
  },
  "audit": {
    "task_id": "task_1f2a3b4c5d6e",
    "detection_count": 1,
    "placeholder_count": 1
  }
}
```

Only pass `redacted_text` to a hosted LLM node. Keep `task_id` and routing
metadata in Dify variables for the later refill node.

Do not upload mapping. Do not upload original text. Do not upload full audit
details beyond safe counts and labels. Do not use this synthetic example as
evidence of complete redaction.

## Hosted LLM Node

The hosted LLM node receives only the redacted value, for example:

```text
Summarize this synthetic record:
{{redact_node.redacted_text}}
```

The LLM response may contain placeholders such as `<CONTACT_001>`. Do not ask
the hosted model to reconstruct the original value.

## Refill HTTP Request Node

Configure a second HTTP Request node:

- Method: `POST`
- URL: `http://127.0.0.1:8765/refill`
- Body type: JSON
- Body example: `examples/dify-refill-request.json`

```json
{
  "task_id": "task_1f2a3b4c5d6e",
  "text": "The hosted model only saw <CONTACT_001> and returned a synthetic summary."
}
```

(`task_id` here is the server-minted id returned by the redact node, stored in
a Dify variable — not a value the workflow chooses.)

Successful response:

```json
{
  "status": "ok",
  "text": "The hosted model only saw 13900001111 and returned a synthetic summary.",
  "error_types": [],
  "unknown_placeholders": [],
  "missing_placeholders": [],
  "altered_placeholders": []
}
```

Blocked response example:

```json
{
  "status": "blocked",
  "text": null,
  "error_types": ["unknown_placeholder"],
  "unknown_placeholders": ["<CONTACT_999>"],
  "missing_placeholders": [],
  "altered_placeholders": []
}
```

Route `blocked` responses to human review. Route HTTP `404` from `/refill` to
human review as a missing or expired local mapping. Route other `error` shapes
to manual inspection of the local workflow configuration.

## Local-Only Boundary

The mapping stays in the local gateway store and is referenced only by `task_id`.
The Dify workflow should treat `task_id` as a local coordination key, not as a
payload to send to the hosted model.

Use only synthetic fixtures while validating the workflow.
