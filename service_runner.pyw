"""Optional foreground-independent runner. No startup registration is installed."""
from __future__ import annotations

import os
from pathlib import Path
import sys
import time


def main() -> int:
    project = Path(__file__).resolve().parent
    sys.path.insert(0, str(project / "src"))
    from tuomin_gateway.platform_security import restrict_permissions

    data_dir = Path(os.environ.get("TUOMIN_DATA_DIR") or
                    str(Path(os.environ.get("APPDATA", str(project / "local_private"))) / "Tuomin"))
    data_dir = data_dir.expanduser().resolve()
    data_dir.mkdir(parents=True, exist_ok=True)
    restrict_permissions(data_dir, is_dir=True)
    os.environ.setdefault("TUOMIN_DATA_DIR", str(data_dir))
    os.environ.setdefault("TUOMIN_MAPPING_DIR", str(data_dir / "mappings"))
    os.environ.setdefault("TUOMIN_CONFIG", str(project / "tuomin_apps.json"))
    os.environ.setdefault("TUOMIN_NER_MODEL_DIR", str(project / "packaging" / "ner-model"))
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
    log_dir = data_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    restrict_permissions(log_dir, is_dir=True)
    log_path = log_dir / f"tuomin-{time.strftime('%Y%m%d')}.log"
    fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        restrict_permissions(log_path, is_dir=False)
        with os.fdopen(fd, "a", encoding="utf-8", buffering=1) as log:
            fd = -1
            old_stdout, old_stderr = sys.stdout, sys.stderr
            sys.stdout = sys.stderr = log
            try:
                from tuomin_gateway.cli import main as cli_main
                print(f"[tuomin] starting local service (pid={os.getpid()})", flush=True)
                return cli_main(["serve", "--host", "127.0.0.1", "--port", os.environ.get("TUOMIN_PORT", "8765")])
            except Exception as exc:
                print(f"[tuomin] startup failed ({type(exc).__name__})", flush=True)
                return 1
            finally:
                sys.stdout, sys.stderr = old_stdout, old_stderr
    finally:
        if fd >= 0:
            os.close(fd)


if __name__ == "__main__":
    raise SystemExit(main())
