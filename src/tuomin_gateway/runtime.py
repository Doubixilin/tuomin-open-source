"""Shared safe credentials and optional background NER startup."""
from __future__ import annotations

import os
from pathlib import Path
import secrets
import threading
import time

from tuomin_gateway.platform_security import restrict_permissions


def configure_admin_token(data_dir: Path) -> Path | None:
    """Keep explicit configuration; otherwise reuse a private, exclusive token file."""
    if os.environ.get("TUOMIN_ADMIN_TOKEN"):
        return None
    data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    restrict_permissions(data_dir, is_dir=True)
    token_path = data_dir / "admin-token"
    try:
        fd = os.open(token_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        restrict_permissions(token_path, is_dir=False)
        # A concurrent first launch may still be completing its exclusive write.
        for attempt in range(20):
            lines = token_path.read_text(encoding="utf-8").splitlines()
            token = next((line.strip() for line in lines if line.strip() and not line.startswith("#")), "")
            if token:
                os.environ["TUOMIN_ADMIN_TOKEN"] = token
                return token_path
            time.sleep(0.05)
        raise RuntimeError("admin credential file is empty; restore a valid credential before starting")
    try:
        restrict_permissions(token_path, is_dir=False)
        token = secrets.token_urlsafe(32)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            fd = -1
            handle.write(token + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.environ["TUOMIN_ADMIN_TOKEN"] = token
    except BaseException:
        if fd >= 0:
            os.close(fd)
        token_path.unlink(missing_ok=True)
        raise
    return token_path


def report_admin_token_location(path: Path | None) -> None:
    if path is None:
        print("[tuomin] using configured admin credential", flush=True)
    else:
        print(f"[tuomin] admin credential file: {path.resolve()}", flush=True)


def warm_ner_async() -> threading.Thread | None:
    """Use the same detector/load lock as requests; never log dependency exception text."""
    if os.environ.get("TUOMIN_WARM_NER", "1").strip().lower() in {"0", "false", "no"}:
        return None

    def load() -> None:
        started = time.monotonic()
        try:
            from tuomin_gateway.detectors.ner import get_ner_detector

            get_ner_detector().detect("预热：联系人示例就职于示例有限公司。")
            print(f"[tuomin] NER warmup done ({time.monotonic() - started:.3f}s)", flush=True)
        except Exception as exc:
            print(f"[tuomin] NER warmup unavailable ({type(exc).__name__}); see readiness", flush=True)

    worker = threading.Thread(target=load, name="tuomin-ner-warmup", daemon=True)
    worker.start()
    return worker
