"""Offline before/after proxy masking benchmark with real CPU NER and synthetic fixtures.

Run in separate processes with --source pointing at a frozen baseline or the
candidate source. Output contains hashes/counts/timings, never fixture text.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import statistics
import sys
import time

ROOT = Path(__file__).resolve().parents[1]


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()


def tool_payload(distinct):
    tools = []
    for i in range(40):
        tag = f"任务{i}：" if distinct else ""
        tools.append({"type": "function", "function": {
            "name": f"synthetic_{i}",
            "description": tag + "Read a local file and provide a concise answer. Preserve its structure and check supplied details. 合成案例：张三在北辰测试制造有限公司负责资料整理。",
            "parameters": {"type": "object", "properties": {
                "path": {"type": "string", "description": tag + "Absolute path to the local file to inspect."},
                "query": {"type": "string", "description": tag + "Text describing the information requested by the user."},
                "format": {"type": "string", "description": tag + "Preferred format for the result, either text or a list."},
                "limit": {"type": "integer", "description": tag + "Maximum number of matching entries to return."},
            }}}})
    return {"tools": tools, "messages": [{"role": "user", "content": "张三在北辰测试制造有限公司工作，联系电话13900001111。"}]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=ROOT / "src")
    parser.add_argument("--mode", choices=("baseline", "optimized"), required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--endpoints-only", action="store_true")
    parser.add_argument("--frozen", type=Path, help="Verify an extracted executable with real bundled NER and a loopback upstream")
    args = parser.parse_args()
    if args.frozen:
        if args.mode != "optimized":
            parser.error("--frozen requires --mode optimized")
        report = verify_frozen(args.frozen.resolve(), args.output.parent.resolve())
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(report, ensure_ascii=False), flush=True)
        return
    sys.path.insert(0, str(args.source.resolve()))
    os.environ.update(TUOMIN_NER_MODEL_DIR=str(ROOT / "packaging/ner-model"), TUOMIN_NER_DEVICE="cpu",
                      HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1")
    from tuomin_gateway.session import SessionRedactor, build_detectors
    from tuomin_gateway.service.proxy import mask_request, refill_json
    from tuomin_gateway.service.registry import _profile_from_spec
    from tuomin_gateway.detectors.ner import get_ner_detector
    import torch
    if args.mode == "optimized":
        from tuomin_gateway.ner_cache import NerCache
        from tuomin_gateway.service.proxy import optimized_mask_request
    profile = _profile_from_spec({"base": "strict", "refill_strict": False})
    report = {"mode": args.mode, "source": str(args.source.resolve()), "network": False,
              "synthetic_only": True, "torch": torch.__version__, "threads": torch.get_num_threads(), "cases": []}
    started = time.perf_counter()
    get_ner_detector().detect("预热示例：张三在北辰测试制造有限公司工作。")
    report["cold_load_seconds"] = round(time.perf_counter() - started, 4)
    if args.endpoints_only:
        report["endpoints"] = verify_endpoints()
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(report["endpoints"]), flush=True)
        return
    samples = [json.loads(line) for line in (ROOT / "tests/fixtures/redaction_benchmark.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    long_text = ("合成案例：张三在北辰测试制造有限公司工作，联系电话13900001111。需要按原格式整理资料并保留全部正文。\n" * 130)
    cases = [("repeated_tools", tool_payload(False)), ("distinct_tools", tool_payload(True)),
             ("long_history", {"messages": [{"role": "user", "content": long_text}, {"role": "assistant", "content": "张三已完成资料整理。"}]})]

    def run(payload, cache):
        redactor = SessionRedactor(build_detectors([]), profile, identity_mode="surface")
        metrics = {}
        started = time.perf_counter()
        if args.mode == "optimized":
            out = optimized_mask_request(deepcopy(payload), redactor, cache=cache, scope="benchmark", metrics=metrics)
        else:
            out = mask_request(deepcopy(payload), redactor)
        elapsed = time.perf_counter() - started
        answer = {"content": " | ".join(redactor.mapping), "reasoning_content": " | ".join(redactor.mapping),
                  "tool_calls": [{"function": {"arguments": json.dumps({"result": " | ".join(redactor.mapping)}, ensure_ascii=False)}}]}
        auto = refill_json(deepcopy(answer), redactor, {"restored": 0, "unknown": set()})
        demo = refill_json(deepcopy(answer), redactor, {"restored": 0, "unknown": set()}, skip_keys=frozenset({"reasoning_content", "thinking"}))
        result = {"masked": out, "mapping": redactor.mapping,
                  "blocked": sorted(redactor.blocked_labels), "auto_refill": auto, "demo_refill": demo,
                  "trace": [vars(t) for t in redactor.trace_entries()],
                  "unknown_blocked": redactor.refill("<ORG_999999>", allow_missing=True)["status"]}
        return elapsed, digest(result), len(redactor.mapping), metrics

    for name, payload in cases:
        cache = NerCache() if args.mode == "optimized" else None
        rows = [run(payload, cache) for _ in range(args.repeats)]
        report["cases"].append({"name": name, "seconds": [round(r[0], 4) for r in rows],
                                "median_seconds": round(statistics.median(r[0] for r in rows), 4),
                                "result_hashes": [r[1] for r in rows], "mapping_count": rows[0][2],
                                "metrics": [r[3] for r in rows]})
        print(json.dumps(report["cases"][-1]), flush=True)
    # Differential corpus: run each original field independently, then in one
    # request to catch batching, mapping-order and known-value propagation drift.
    corpus_cache = NerCache() if args.mode == "optimized" else None
    corpus = []
    for sample in samples:
        try:
            _, hash_value, count, _ = run({"messages": [{"role": "user", "content": sample["text"]}]}, corpus_cache)
            corpus.append({"id": sample["sample_id"], "hash": hash_value, "mapping_count": count})
        except Exception as exc:
            corpus.append({"id": sample["sample_id"], "error": type(exc).__name__})
    combined = {"messages": [{"role": "user", "content": s["text"]} for s in samples]}
    try:
        _, combined_hash, _, _ = run(combined, NerCache() if args.mode == "optimized" else None)
        report["combined_corpus_hash"] = combined_hash
    except Exception as exc:
        report["combined_corpus_error"] = type(exc).__name__
    report["corpus"] = corpus
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"completed": args.mode, "corpus_count": len(corpus)}), flush=True)


def verify_endpoints():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from tuomin_gateway.service.proxy import register_proxy, ForwardResult
    from tuomin_gateway.service.registry import AppRegistry
    app = FastAPI()
    source = "张三在北辰测试制造有限公司工作，联系电话13900001111。"
    captured = []
    async def forward(url, headers, body, *, stream):
        payload = json.loads(body)
        text = payload["messages"][-1]["content"]
        captured.append(text)
        async def chunks():
            for offset in range(0, len(text), 7):
                value = text[offset:offset + 7]
                yield ("data: " + json.dumps({"choices": [{"delta": {"content": value, "reasoning_content": value}}]}, ensure_ascii=False) + "\n\n").encode()
            yield b"data: [DONE]\n\n"
        return ForwardResult(200, {"content-type": "text/event-stream"}, aiter=chunks())
    registry = AppRegistry({"benchmark": {"profile": {"base": "strict", "refill_strict": False},
                          "proxy_upstreams": {"openai": "https://synthetic.invalid/chat"},
                          "allow_auto_refill": True}})
    register_proxy(app, registry, forwarder=forward)
    rows = []
    with TestClient(app) as client:
        for mode, segment in [("masked", ""), ("demo", "demo/"), ("auto", "auto/")]:
            payload = tool_payload(False)
            payload.update(model="synthetic", stream=True)
            started = time.perf_counter()
            response = client.post(f"/apps/benchmark/{segment}v1/chat/completions", json=payload)
            seconds = time.perf_counter() - started
            content = reasoning = ""
            for line in response.text.splitlines():
                if line.startswith("data: ") and line != "data: [DONE]":
                    item = json.loads(line[6:])
                    for choice in item.get("choices", []):
                        delta = choice.get("delta", {})
                        content += delta.get("content", "")
                        reasoning += delta.get("reasoning_content", "")
            outbound_safe = all(v not in captured[-1] for v in ("张三", "北辰测试制造有限公司", "13900001111"))
            expected_content = captured[-1] if mode == "masked" else source
            expected_reasoning = source if mode == "auto" else captured[-1]
            ok = response.status_code == 200 and "data: [DONE]" in response.text and outbound_safe and content == expected_content and reasoning == expected_reasoning
            rows.append({"mode": mode, "status": response.status_code, "seconds": round(seconds, 4),
                         "outbound_safe": outbound_safe, "content_exact": content == expected_content,
                         "reasoning_exact": reasoning == expected_reasoning, "ok": ok})
            if not ok:
                print(json.dumps({"failed": rows[-1]}), flush=True)
                raise RuntimeError(f"endpoint equivalence failed: {mode}")
    return rows


def verify_frozen(binary, output_dir):
    """Exercise the delivered binary, isolated from user settings and networks."""
    import subprocess
    import tempfile
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    import httpx
    from acceptance_shell import _stop

    output_dir.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="frozen-ner-", dir=output_dir))
    data, home = work / "data", work / "home"
    data.mkdir()
    for path in (home / "AppData/Roaming", home / "AppData/Local"):
        path.mkdir(parents=True, exist_ok=True)
    captured = []

    class Upstream(BaseHTTPRequestHandler):
        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers["content-length"])))
            captured.append(payload)
            text = payload["messages"][-1]["content"]
            self.send_response(200)
            self.send_header("content-type", "text/event-stream")
            self.end_headers()
            for offset in range(0, len(text), 7):
                value = text[offset:offset + 7]
                chunk = {"choices": [{"delta": {"content": value, "reasoning_content": value}}]}
                self.wfile.write(("data: " + json.dumps(chunk, ensure_ascii=False) + "\n\n").encode())
            self.wfile.write(b"data: [DONE]\n\n")

        def log_message(self, *args):
            pass

    upstream = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
    threading.Thread(target=upstream.serve_forever, daemon=True).start()
    registry = data / "apps.json"
    registry.write_text(json.dumps({"apps": {"benchmark": {
        "profile": {"base": "strict", "refill_strict": False}, "allow_auto_refill": True,
        "upstream_model": "synthetic",
    }}}), encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if not k.startswith("TUOMIN_")}
    env.update(TUOMIN_CONFIG=str(registry), TUOMIN_DATA_DIR=str(data),
               TUOMIN_KEYCHAIN_SERVICE="com.tuomin.benchmark." + work.name,
               TUOMIN_OPEN_BROWSER="0", TUOMIN_NO_BROWSER="1", TUOMIN_NER_DEVICE="cpu",
               TUOMIN_UPSTREAM_ALLOWLIST="127.0.0.1",
               TUOMIN_UPSTREAM_OPENAI=f"http://127.0.0.1:{upstream.server_port}/v1/chat/completions",
               HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1", PYTHONNOUSERSITE="1",
               PYTHONUTF8="1", PYTHONIOENCODING="utf-8",
               HOME=str(home), USERPROFILE=str(home), APPDATA=str(home / "AppData/Roaming"),
               LOCALAPPDATA=str(home / "AppData/Local"))
    report = {"binary": str(binary), "synthetic_only": True, "network": "loopback_only", "endpoints": []}
    proc = None
    with (work / "service.log").open("w", encoding="utf-8") as log:
        try:
            proc = subprocess.Popen([str(binary), "--headless", "--shutdown-on-stdin-close"],
                                    stdin=subprocess.PIPE, stdout=log, stderr=subprocess.STDOUT,
                                    env=env, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            with httpx.Client(trust_env=False, timeout=180) as client:
                deadline = time.monotonic() + 240
                while time.monotonic() < deadline:
                    if proc.poll() is not None:
                        raise RuntimeError("frozen service exited during startup")
                    try:
                        url = (data / "instance.url").read_text(encoding="utf-8").strip().removesuffix("/ui/").rstrip("/")
                        if client.get(url + "/healthz", timeout=2).status_code == 200:
                            break
                    except (OSError, httpx.HTTPError):
                        pass
                    time.sleep(0.25)
                else:
                    raise RuntimeError("frozen service startup timed out")
                token = next(line.strip() for line in (data / "admin-token").read_text(encoding="utf-8").splitlines()
                             if line.strip() and not line.startswith("#"))
                headers = {"x-tuomin-admin-token": token, "X-Requested-With": "tuomin-ui"}
                payload = tool_payload(False)
                payload.update(model="synthetic", stream=True)
                source = payload["messages"][-1]["content"]
                for mode, segment in [("masked", ""), ("demo", "demo/"), ("auto", "auto/")]:
                    started = time.perf_counter()
                    response = client.post(f"{url}/apps/benchmark/{segment}v1/chat/completions", json=payload, headers=headers)
                    content = reasoning = ""
                    for line in response.text.splitlines():
                        if line.startswith("data: ") and line != "data: [DONE]":
                            for choice in json.loads(line[6:]).get("choices", []):
                                delta = choice.get("delta", {})
                                content += delta.get("content", "")
                                reasoning += delta.get("reasoning_content", "")
                    outbound = captured[-1] if len(captured) == len(report["endpoints"]) + 1 else {}
                    masked = outbound.get("messages", [{}])[-1].get("content", "")
                    safe = bool(outbound) and all(value not in json.dumps(outbound, ensure_ascii=False)
                                                  for value in ("张三", "北辰测试制造有限公司", "13900001111"))
                    row = {"mode": mode, "status": response.status_code, "seconds": round(time.perf_counter() - started, 4),
                           "outbound_safe": safe, "content_exact": content == (masked if mode == "masked" else source),
                           "reasoning_exact": reasoning == (source if mode == "auto" else masked),
                           "server_timing_present": "ner_infer;dur=" in response.headers.get("server-timing", "")}
                    report["endpoints"].append(row)
                    if response.status_code != 200 or "data: [DONE]" not in response.text or not all(row[k] for k in
                            ("outbound_safe", "content_exact", "reasoning_exact", "server_timing_present")):
                        raise RuntimeError(f"frozen endpoint equivalence failed: {mode}")
                audit = client.get(url + "/admin/audit/proxy-performance", headers=headers)
                audit.raise_for_status()
                rows = [row for row in audit.json()["events"] if row.get("stage") == "response_headers"]
                fields = ("status", "text_fields", "unique_texts", "cache_hits", "cache_misses", "mask_ms", "ner_ready_ms", "ner_infer_ms")
                report["timings"] = [{k: row.get(k) for k in fields} for row in rows]
                report["cache_verified"] = (len(rows) == 3 and rows[0].get("text_fields") == 201
                    and rows[0].get("cache_misses") == 6
                    and all(row.get("cache_hits") == 6 and row.get("cache_misses") == 0 for row in rows[1:]))
                if not report["cache_verified"]:
                    raise RuntimeError("frozen cache metrics differ from expected workload")
        finally:
            if proc is not None:
                report["clean_exit"] = _stop(proc) and proc.returncode == 0
            upstream.shutdown()
            upstream.server_close()
    if not report.get("clean_exit"):
        raise RuntimeError("frozen service did not exit cleanly")
    return report


if __name__ == "__main__":
    main()
