"""Redact files on disk through the local document API (CLI, Finder, drag & drop).

Original-format types (docx/xlsx/pptx and convertible doc/xls) keep their
layout; text-like types (pdf/txt/md/csv/json/yaml) produce a redacted
Markdown file. Results are written next to the source as ``名称（脱敏）.ext``
(or into ``out_dir``), never overwriting an existing file, and only when the
egress gate allowed the result. The task id is returned so the trusted UI can
refill later.
"""
from __future__ import annotations

import base64
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Callable

ORIGINAL_FORMATS = {".docx": "docx", ".doc": "docx", ".xlsx": "office", ".xls": "office", ".pptx": "office"}
PARSE_FORMATS = {".pdf", ".txt", ".md", ".markdown", ".csv", ".json", ".yaml", ".yml", ".xlsm"}
SUPPORTED = set(ORIGINAL_FORMATS) | PARSE_FORMATS
MAX_FILE_BYTES = 200 * 1024 * 1024
SUFFIX = "（脱敏）"

Post = Callable[[str, bytes], tuple[int, dict]]


def supported(path: str | Path) -> bool:
    return Path(path).suffix.lower() in SUPPORTED


def output_path(source: Path, extension: str, out_dir: Path | None = None) -> Path:
    folder = out_dir or source.parent
    stem = source.stem
    candidate = folder / f"{stem}{SUFFIX}{extension}"
    number = 2
    while candidate.exists():
        candidate = folder / f"{stem}（脱敏 {number}）{extension}"
        number += 1
    return candidate


def _message(status: int, data: dict) -> str:
    error = data.get("error") if isinstance(data, dict) else None
    if isinstance(error, dict) and error.get("message"):
        return str(error["message"])
    if isinstance(data, dict) and isinstance(data.get("detail"), str):
        return data["detail"]
    return f"服务返回 {status}"


def _egress_reasons(data: dict) -> str:
    egress = data.get("egress") or {}
    reasons = [r.get("message") or r.get("code") for r in egress.get("reasons", []) if isinstance(r, dict)]
    return "；".join(str(r) for r in reasons if r) or "出站闸门未通过"


def redact_file(path: str | Path, *, post: Post, app_id: str | None = "workbench", out_dir: str | Path | None = None) -> dict:
    source = Path(path).expanduser()
    result = {"source": str(source), "output": None, "task_id": None, "status": "error", "message": "", "label_counts": {}, "warnings": []}
    extension = source.suffix.lower()
    if extension not in SUPPORTED:
        result["message"] = "不支持的文件类型（支持 Word、Excel、PPT、PDF、文本等）"
        return result
    try:
        if not source.is_file():
            result["message"] = "文件不存在"
            return result
        if source.stat().st_size > MAX_FILE_BYTES:
            result["message"] = "文件超过 200 MB，请拆分后再试"
            return result
        data_bytes = source.read_bytes()
    except OSError as exc:
        result["message"] = f"无法读取文件：{exc.strerror or exc}"
        return result
    query = {"filename": source.name}
    if app_id:
        query["app_id"] = app_id
    kind = ORIGINAL_FORMATS.get(extension)
    route = (f"/api/v1/documents/redact-{kind}" if kind else "/api/v1/documents/parse") + "?" + urllib.parse.urlencode(query)
    status, data = post(route, data_bytes)
    if status != 200 or not isinstance(data, dict):
        result["message"] = _message(status, data if isinstance(data, dict) else {})
        if isinstance(data, dict) and data.get("egress"):
            result["status"] = "blocked"
        return result
    result["task_id"] = data.get("task_id")
    result["label_counts"] = data.get("label_counts") or {}
    result["warnings"] = [w.get("code") for w in data.get("warnings", []) if isinstance(w, dict)]
    if not (data.get("egress") or {}).get("egress_allowed"):
        result["status"] = "blocked"
        result["message"] = _egress_reasons(data)
        return result
    if kind:
        encoded = data.get("redacted_docx_b64") if kind == "docx" else data.get("redacted_office_b64")
        file_format = data.get("file_format")
        if not isinstance(encoded, str) or not isinstance(file_format, str):
            result["message"] = "脱敏结果格式不匹配"
            return result
        payload, target_extension = base64.b64decode(encoded), "." + file_format
    else:
        markdown = data.get("redacted_markdown")
        if not isinstance(markdown, str):
            result["message"] = "脱敏结果为空"
            return result
        payload, target_extension = markdown.encode("utf-8"), ".md"
    target = output_path(source, target_extension, Path(out_dir).expanduser() if out_dir else None)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as stream:
            from tuomin_gateway.platform_security import restrict_permissions
            restrict_permissions(target, is_dir=False)
            stream.write(payload)
    except OSError as exc:
        result["message"] = f"无法写入结果：{exc.strerror or exc}"
        return result
    result.update(status="ok", output=str(target), message="")
    return result


def http_post(base_url: str, admin_token: str, *, timeout: float = 1800) -> Post:
    """POST raw bytes to the local gateway with the admin credential."""
    origin = base_url.rstrip("/")

    def post(route: str, body: bytes) -> tuple[int, dict]:
        request = urllib.request.Request(origin + route, data=body, method="POST", headers={
            "x-tuomin-admin-token": admin_token, "Content-Type": "application/octet-stream", "X-Requested-With": "tuomin-file",
        })
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.status, json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as exc:
            try:
                return exc.code, json.loads(exc.read() or b"{}")
            except ValueError:
                return exc.code, {}
        except (urllib.error.URLError, OSError) as exc:
            return 0, {"error": {"message": f"无法连接 Tuomin：{getattr(exc, 'reason', exc)}"}}
    return post


def summarize(results: list[dict]) -> str:
    ok = [r for r in results if r["status"] == "ok"]
    failed = [r for r in results if r["status"] != "ok"]
    if not results:
        return "没有可脱敏的文件"
    if not failed:
        return f"已脱敏 {len(ok)} 个文件，结果保存在原文件旁"
    first = failed[0]
    reason = f"{Path(first['source']).name}：{first['message']}"
    return (f"已脱敏 {len(ok)} 个，{len(failed)} 个未完成（{reason}）" if ok else f"未能脱敏（{reason}）")
