"""Small private JSON files. Never persist clipboard text or mapping handles."""
from __future__ import annotations
import json
import os
from pathlib import Path
import tempfile
from tuomin_gateway.platform_security import restrict_permissions

SESSION_KEYS = {'namespace_id', 'started_at', 'updated_at', 'segment_count', 'entity_count'}


def read_private(path: Path) -> dict:
    if path.is_symlink():
        raise ValueError('private state must not be a symbolic link')
    try:
        restrict_permissions(path, is_dir=False)
        value = json.loads(path.read_text(encoding='utf-8'))
    except FileNotFoundError:
        return {}
    if not isinstance(value, dict):
        raise ValueError('invalid private state')
    return value


def write_private(path: Path, value: dict) -> None:
    if path.is_symlink():
        raise ValueError('private state must not be a symbolic link')
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    restrict_permissions(path.parent, is_dir=True)
    fd, temporary = tempfile.mkstemp(prefix='.' + path.name, dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            # Apply the DACL before credentials are written, not only after
            # replacement. chmod(0600) does not establish privacy on Windows.
            restrict_permissions(temporary, is_dir=False)
            json.dump(value, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class SessionFile:
    def __init__(self, directory):
        self.path = Path(directory) / 'clipboard-session.json'

    def load(self):
        return {k: v for k, v in read_private(self.path).items() if k in SESSION_KEYS}

    def save(self, value):
        write_private(self.path, {k: v for k, v in value.items() if k in SESSION_KEYS})
