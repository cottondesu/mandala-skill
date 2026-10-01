#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path
import os
import shutil
import tempfile

if __package__:
    from .package_safety import require_real_directory_path
else:
    from package_safety import require_real_directory_path


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "src" / "mandala"


def source_files() -> list[Path]:
    if not SOURCE.is_dir() or SOURCE.is_symlink():
        raise ValueError("canonical source must be a real directory")
    files = []
    for path in sorted(SOURCE.rglob("*")):
        if path.is_symlink():
            raise ValueError(f"source symlink: {path}")
        if path.is_file():
            files.append(path.relative_to(SOURCE))
        elif not path.is_dir():
            raise ValueError(f"non-regular source entry: {path}")
    if not files:
        raise ValueError("canonical source is empty")
    return files


def main() -> None:
    files = source_files()
    target = ROOT / "dist" / "mandala"
    require_real_directory_path(ROOT, target, allow_missing=True)
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".mandala-build-", dir=target.parent) as workspace:
        temporary = Path(workspace) / "new"
        temporary.mkdir()
        for relative in files:
            destination = temporary / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes((SOURCE / relative).read_bytes())
            destination.chmod(0o644)
        backup = Path(workspace) / "old"
        if target.exists():
            os.replace(target, backup)
        try:
            os.replace(temporary, target)
        except OSError:
            if backup.exists():
                os.replace(backup, target)
            raise
        if backup.exists():
            shutil.rmtree(backup)


if __name__ == "__main__":
    main()
