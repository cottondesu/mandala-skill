from __future__ import annotations

from pathlib import Path
import stat


def require_real_directory_path(root: Path, path: Path, *, allow_missing: bool = False) -> None:
    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"package path escapes repository root: {path}") from exc
    current = root
    for part in relative.parts:
        current = current / part
        try:
            mode = current.lstat().st_mode
        except FileNotFoundError:
            if allow_missing:
                break
            raise ValueError(f"missing package directory: {current}") from None
        if stat.S_ISLNK(mode):
            raise ValueError(f"symlink in package path: {current}")
        if not stat.S_ISDIR(mode):
            raise ValueError(f"non-directory in package path: {current}")
