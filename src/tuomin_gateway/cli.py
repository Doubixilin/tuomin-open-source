from __future__ import annotations

import argparse
import json
import os
import socket
from pathlib import Path

from tuomin_gateway.audit import build_audit_event
from tuomin_gateway.benchmark import load_benchmark, risk_reduction_report, run_benchmark, run_ner_comparison
from tuomin_gateway.detectors.dictionary import DictionaryDetector
from tuomin_gateway.detectors.rules import RuleDetector
from tuomin_gateway.dictionary_quality import lint_dictionary_entries
from tuomin_gateway.evaluation import evaluate_samples
from tuomin_gateway.fusion import fuse_detections
from tuomin_gateway.mapping import load_mapping, save_mapping
from tuomin_gateway.placeholders import reserved_placeholder_conflict
from tuomin_gateway.redactor import redact_text
from tuomin_gateway.refill import refill_text


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="tuomin-gateway")
    subparsers = parser.add_subparsers(dest="command", required=True)

    redact_parser = subparsers.add_parser("redact")
    _add_text_args(redact_parser)
    redact_parser.add_argument("--dictionary", type=Path)
    redact_parser.add_argument("--mapping-dir", type=Path, default=Path("local_private/mappings"))
    redact_parser.add_argument("--task-id", default=None)

    refill_parser = subparsers.add_parser("refill")
    _add_text_args(refill_parser)
    refill_parser.add_argument("--mapping", type=Path, required=True)

    eval_parser = subparsers.add_parser("eval")
    eval_parser.add_argument("--samples", type=Path, required=True)
    eval_parser.add_argument("--dictionary", type=Path, required=True)
    eval_parser.add_argument("--ner", action="store_true", help="include the local NER detector (optional dep)")

    benchmark_parser = subparsers.add_parser("benchmark")
    benchmark_parser.add_argument("--samples", type=Path, required=True)
    benchmark_parser.add_argument("--dictionary", type=Path, required=True)
    benchmark_parser.add_argument("--ner", action="store_true", help="include the local NER detector (optional dep)")
    benchmark_parser.add_argument("--risk-reduction", action="store_true", help="include L0/L1 relative risk reduction metrics")
    benchmark_parser.add_argument("--compare-ner", action="store_true", help="compare L1a rules+dictionary with L1b local NER; real NER loads only with --ner")

    dictionary_lint_parser = subparsers.add_parser("dictionary-lint")
    dictionary_lint_parser.add_argument("--dictionary", type=Path, required=True)

    redact_file_parser = subparsers.add_parser("redact-file", help="脱敏文件（Word/Excel/PPT 保留排版，PDF/文本输出 .md）")
    redact_file_parser.add_argument("files", nargs="+", type=Path)
    redact_file_parser.add_argument("--app", default="workbench", help="使用哪个应用的词典与档位（默认 workbench）")
    redact_file_parser.add_argument("--out-dir", type=Path, default=None, help="结果目录（默认写在原文件旁）")
    redact_file_parser.add_argument("--gateway", default=None, help="正在运行的 Tuomin 地址；默认自动发现，找不到则在本进程内处理")
    redact_file_parser.add_argument("--json", action="store_true", help="以 JSON 输出每个文件的结果")

    serve_parser = subparsers.add_parser("serve")
    serve_parser.add_argument("--host", default="127.0.0.1")
    serve_parser.add_argument("--port", type=int, default=None)

    args = parser.parse_args(argv)
    if args.command == "redact":
        return _redact(args)
    if args.command == "redact-file":
        return _redact_file(args)
    if args.command == "refill":
        return _refill(args)
    if args.command == "eval":
        return _eval(args)
    if args.command == "benchmark":
        return _benchmark(args)
    if args.command == "dictionary-lint":
        return _dictionary_lint(args)
    if args.command == "serve":
        return _serve(args)
    return 2


def _add_text_args(parser: argparse.ArgumentParser) -> None:
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--text")
    source.add_argument("--file", type=Path)


def _read_text(args: argparse.Namespace) -> str:
    if args.text is not None:
        return args.text
    return args.file.read_text(encoding="utf-8")


def _redact(args: argparse.Namespace) -> int:
    try:
        text = _read_text(args)
    except OSError:
        return _print_error("input_read_error", "input file could not be read")
    if reserved_placeholder_conflict(text):
        return _print_error(
            "reserved_placeholder_conflict",
            "input contains reserved placeholder syntax",
        )
    detectors = [RuleDetector()]
    if args.dictionary:
        try:
            detectors.append(DictionaryDetector.from_json(args.dictionary))
        except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError):
            return _print_error("dictionary_read_error", "dictionary file could not be read")
    from tuomin_gateway.detectors.rules import UnsafeCredentialInput

    try:
        detections = fuse_detections([span for detector in detectors for span in detector.detect(text)], text)
    except UnsafeCredentialInput:
        return _print_error("invalid_private_key_block", "Private key block is incomplete or mismatched")
    result = redact_text(text, detections, task_id=args.task_id)
    mapping_path = args.mapping_dir / result.mapping_id
    save_mapping(result.mapping, mapping_path)
    audit = build_audit_event(result.task_id, detections, result.mapping)
    payload = result.to_safe_dict() | {
        "mapping_path": str(mapping_path),
        "audit": audit.to_safe_dict(),
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def _refill(args: argparse.Namespace) -> int:
    try:
        text = _read_text(args)
    except OSError:
        return _print_error("input_read_error", "input file could not be read")
    try:
        mapping = load_mapping(args.mapping)
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError):
        return _print_error("mapping_read_error", "mapping file could not be read")
    result = refill_text(text, mapping)
    print(json.dumps(result.to_safe_dict(), ensure_ascii=False, indent=2))
    return 0 if result.status == "ok" else 1


def _eval(args: argparse.Namespace) -> int:
    try:
        report = evaluate_samples(args.samples, args.dictionary, use_ner=args.ner)
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError):
        return _print_error("eval_read_error", "evaluation inputs could not be read")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


def _benchmark(args: argparse.Namespace) -> int:
    try:
        if args.compare_ner:
            report = run_ner_comparison(args.samples, args.dictionary, use_ner=args.ner)
        else:
            report = run_benchmark(args.samples, args.dictionary, use_ner=args.ner)
            if args.risk_reduction:
                report = {
                    "status": "ok",
                    "source": "synthetic",
                    "benchmark": report,
                    "risk_reduction": risk_reduction_report(load_benchmark(args.samples), report),
                }
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError):
        return _print_error("benchmark_read_error", "benchmark inputs could not be read")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


def _dictionary_lint(args: argparse.Namespace) -> int:
    try:
        entries = json.loads(args.dictionary.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return _print_error(
            "dictionary_read_error", "dictionary file could not be read"
        )
    report = lint_dictionary_entries(entries)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "ok" else 1


def _pick_port(preferred: int, host: str = "127.0.0.1") -> int:
    """Return the preferred port if free, else the next free one (OS-assigned)."""
    for candidate in range(preferred, preferred + 20):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            try:
                sock.bind((host, candidate))
                return candidate
            except OSError:
                continue
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((host, 0))
        return sock.getsockname()[1]


def _serve(args: argparse.Namespace) -> int:
    try:
        import uvicorn

        from tuomin_gateway.service.app import create_app
    except ImportError:
        return _print_error("serve_unavailable", "serve 需要可选依赖：pip install tuomin-gateway[serve]")
    preferred = args.port if args.port is not None else int(os.environ.get("TUOMIN_PORT", "8765"))
    port = _pick_port(preferred, args.host)
    if port != preferred:
        print(f"[tuomin] port {preferred} busy, using {port}", flush=True)
    from tuomin_gateway.runtime import configure_admin_token, report_admin_token_location

    token_path = configure_admin_token(Path(os.environ.get("TUOMIN_DATA_DIR", "local_private/runtime")))
    app = create_app()
    print(f"[tuomin] serving http://{args.host}:{port}  (docs at /docs, WebUI at /ui)", flush=True)
    report_admin_token_location(token_path)
    _warm_ner_async()
    uvicorn.run(app, host=args.host, port=port, log_level="info")
    return 0


def _warm_ner_async():
    from tuomin_gateway.runtime import warm_ner_async

    return warm_ner_async()


def _print_error(error_type: str, message: str) -> int:
    print(
        json.dumps(
            {
                "status": "error",
                "error_type": error_type,
                "message": message,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 2




def _desktop_data_dir() -> Path:
    from tuomin_gateway.agent_setup import default_data_dir

    return default_data_dir()


def _running_gateway(explicit: str | None) -> tuple[str | None, str | None]:
    """(base_url, admin_token) of a running desktop instance, if any."""
    data_dir = _desktop_data_dir()
    url = explicit or os.environ.get("TUOMIN_GATEWAY_URL")
    if not url:
        try:
            url = (data_dir / "instance.url").read_text(encoding="utf-8").strip().removesuffix("/ui/").removesuffix("/")
        except OSError:
            url = None
    token = os.environ.get("TUOMIN_ADMIN_TOKEN")
    if not token:
        try:
            token = (data_dir / "admin-token").read_text(encoding="utf-8").strip()
        except OSError:
            token = None
    if url:
        import urllib.request

        try:
            with urllib.request.urlopen(url.rstrip("/") + "/healthz", timeout=1) as response:
                if response.status != 200:
                    url = None
        except OSError:
            url = None
    return url, token


def _in_process_post():
    """Serve the document API inside this process (servers, scripts, CI)."""
    import secrets

    from fastapi.testclient import TestClient

    from tuomin_gateway.service.app import create_app

    data_dir = _desktop_data_dir()
    if not os.environ.get("TUOMIN_CONFIG") and (data_dir / "apps.json").exists():
        os.environ["TUOMIN_CONFIG"] = str(data_dir / "apps.json")
    os.environ.setdefault("TUOMIN_DATA_DIR", str(data_dir))
    os.environ.setdefault("TUOMIN_MAPPING_DIR", str(data_dir / "mappings"))
    token = secrets.token_urlsafe(32)
    client = TestClient(create_app(admin_token=token), base_url="http://127.0.0.1")

    def post(route: str, body: bytes) -> tuple[int, dict]:
        response = client.post(route, content=body, headers={"x-tuomin-admin-token": token, "X-Requested-With": "tuomin-cli"})
        try:
            return response.status_code, response.json()
        except ValueError:
            return response.status_code, {}
    return post


def _redact_file(args: argparse.Namespace) -> int:
    from tuomin_gateway.file_redact import http_post, redact_file

    url, token = _running_gateway(args.gateway)
    if url and token:
        post, mode = http_post(url, token), f"connected:{url}"
    else:
        post, mode = _in_process_post(), "in-process"
    results = [redact_file(path, post=post, app_id=args.app or None, out_dir=args.out_dir) for path in args.files]
    if args.json:
        print(json.dumps({"mode": mode, "results": results}, ensure_ascii=False, indent=2))
    else:
        for item in results:
            if item["status"] == "ok":
                print(f"✓ {item['source']} → {item['output']}（任务 {item['task_id']}）")
            else:
                print(f"✗ {item['source']}：{item['message']}")
    return 0 if all(item["status"] == "ok" for item in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
