from __future__ import annotations

from pathlib import Path


class PathGuardError(ValueError):
    pass


def resolve_under(root: str | Path, candidate: str | Path) -> Path:
    """Resolve ``candidate`` against ``root`` and ensure it stays inside."""
    root_path = Path(root).expanduser().resolve()
    candidate_path = Path(candidate)
    resolved = (
        candidate_path.expanduser().resolve()
        if candidate_path.is_absolute()
        else (root_path / candidate_path).resolve()
    )
    try:
        resolved.relative_to(root_path)
    except ValueError as exc:
        raise PathGuardError(f"path escapes allowed root: {candidate}") from exc
    return resolved


def ensure_under(root: str | Path, candidate: str | Path) -> Path:
    """Resolve an already-absolute ``candidate`` and ensure it stays inside root."""
    root_path = Path(root).expanduser().resolve()
    resolved = Path(candidate).expanduser().resolve()
    try:
        resolved.relative_to(root_path)
    except ValueError as exc:
        raise PathGuardError(f"path escapes allowed root: {candidate}") from exc
    return resolved
