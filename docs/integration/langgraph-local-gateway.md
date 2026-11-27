# LangGraph Local Gateway Contract

This document describes a synthetic-only LangGraph integration shape for the
local Tuomin Gateway at `127.0.0.1`. It is a contract and pseudocode reference;
it does not install LangGraph or any model dependency.

## Recommended State

Use a state shape like `examples/langgraph-state-example.json`:

```json
{
  "task_id": "task_2b3c4d5e6f7a",
  "raw_text": {
    "local_only": true,
    "value": "Synthetic Team Alpha contact 13900001111 for the local gateway demo."
  },
  "redacted_text": "Synthetic Team Alpha contact <CONTACT_001> for the local gateway demo.",
  "risk_summary": {"total": 1, "by_label": {"CONTACT": 1}},
  "llm_text": "The hosted model only saw <CONTACT_001> and returned a synthetic summary.",
  "refill_status": "pending",
  "review_reason": null
}
```

Recommended fields:

- `raw_text.local_only`: original input stays in process-local state and is
  never sent to a hosted LLM node.
- `redacted_text`: only this field is sent to the LLM node.
- `task_id`: local mapping lookup key for refill, minted by the gateway and
  taken from the `/redact` response (client-chosen ids are rejected).
- `risk_summary`: safe counts and labels for routing.
- `refill_status`: `pending`, `ok`, `blocked`, or `error`.
- `review_reason`: reason for the `human_review` branch.

Do not upload mapping. Do not upload original text. Do not upload full audit
details or mapping files to graph stores that are shared with hosted services.

Register the synthetic workflow locally before using the pseudocode, for
example `{"apps":{"langgraph_synthetic_demo":{"profile":"kb"}}}`. The
registry owns the minimum detector policy; graph state must not override it.

## Node Responsibilities

`redact_node`

- Calls `POST http://127.0.0.1:8765/redact`.
- Sends `raw_text.value` only to the local gateway.
- Stores `task_id`, `redacted_text`, and `risk_summary`.
- Does not store mapping in graph state.

`llm_node`

- Sends only `redacted_text` to the hosted LLM.
- Receives model output that may still contain placeholders.
- Does not ask the hosted LLM to infer original values.

`refill_node`

- Calls `POST http://127.0.0.1:8765/refill` with `task_id` and LLM output text.
- Sets `refill_status = "ok"` when the response status is `ok`.
- Sets `refill_status = "blocked"` when placeholder integrity fails.

`human_review_node`

- Handles placeholder rewrite, unknown placeholder, missing mapping, expired
  mapping, or gateway errors.
- Shows only the minimum local evidence needed for an operator to decide the
  next step.

## Synthetic Pseudocode

```python
def redact_node(state):
    response = http.post(
        "http://127.0.0.1:8765/redact",
        json={
            "app_id": "langgraph_synthetic_demo",
            "text": state["raw_text"]["value"],
        },
    )
    data = response.json()
    # The gateway rejects a client-chosen task_id (400); the server mints it.
    return {
        **state,
        "task_id": data["task_id"],
        "redacted_text": data["redacted_text"],
        "risk_summary": data["risk_summary"],
    }


def llm_node(state):
    result = hosted_llm.invoke(state["redacted_text"])
    return {**state, "llm_text": result.text}


def refill_node(state):
    response = http.post(
        "http://127.0.0.1:8765/refill",
        json={"task_id": state["task_id"], "text": state["llm_text"]},
    )
    if response.status_code == 404:
        return {**state, "refill_status": "blocked", "review_reason": "mapping_missing_or_expired"}
    data = response.json()
    if data["status"] != "ok":
        return {**state, "refill_status": "blocked", "review_reason": ",".join(data["error_types"])}
    return {**state, "refill_status": "ok", "final_text": data["text"]}
```

## Failure Branches

Route to `human_review` when:

- A placeholder is rewritten, for example `<CONTACT-001>` instead of
  `<CONTACT_001>`.
- The model invents an unknown placeholder such as `<CONTACT_999>`.
- `/refill` returns `404`, which means the local mapping is missing or expired.
- The gateway returns HTTP `400`, `409`, or `5xx`.

The graph should fail closed for these branches and avoid sending raw values to
hosted services while resolving the issue.
