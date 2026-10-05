"""Optional, bounded source snapshots for Git working trees."""

import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

from .store import atomic_json


def git_metadata(cwd):
    def git(*arguments):
        result = subprocess.run(["git", "-C", str(cwd), *arguments], capture_output=True,
                                text=True, timeout=10, check=False)
        return result.stdout.strip() if result.returncode == 0 else None
    try:
        revision = git("rev-parse", "HEAD")
        return {"commit": revision, "dirty": bool(git("status", "--porcelain"))} if revision else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def freeze(cwd, root, max_bytes=256 * 1024**2):
    cwd = Path(cwd).resolve()
    result = subprocess.run(["git", "-C", str(cwd), "ls-files", "--cached", "--others",
                             "--exclude-standard", "-z", "--", "."], capture_output=True,
                            timeout=30, check=False)
    if result.returncode:
        raise ValueError("--snapshot needs a Git working tree. You can also use --cwd with an existing frozen source directory.")
    names = sorted(set(os.fsdecode(name) for name in result.stdout.split(b"\0") if name))
    if len(names) > 20000:
        raise ValueError("Source snapshot exceeds 20,000 files; keep datasets and environments outside the source tree")
    total = sum((cwd / name).lstat().st_size for name in names if (cwd / name).is_file())
    if total > max_bytes:
        raise ValueError("Source snapshot exceeds 256 MiB; mount datasets/checkpoints separately")
    snapshots = root / "snapshots"
    snapshots.mkdir(parents=True, exist_ok=True, mode=0o700)
    destination = Path(tempfile.mkdtemp(prefix="source-", dir=snapshots))
    files = []
    copied_bytes = 0
    try:
        for name in names:
            source = cwd / name
            if not source.exists() and not source.is_symlink():
                continue  # A tracked file deleted in this working tree.
            if Path(name).is_absolute() or ".." in Path(name).parts:
                raise ValueError("Snapshot contains a path outside its working directory")
            target = destination / name
            target.parent.mkdir(parents=True, exist_ok=True)
            if source.is_symlink():
                if not source.resolve().is_relative_to(cwd):
                    raise ValueError(f"Snapshot symlink points outside the source tree: {name}. Use a separate mount.")
                resolved = source.resolve().relative_to(cwd)
                link = os.path.relpath(destination / resolved, target.parent)
                target.symlink_to(link)
                files.append({"path": name, "symlink": link})
                continue
            if not source.is_file():
                continue
            digest = hashlib.sha256()
            size = 0
            with source.open("rb") as reader, target.open("wb") as writer:
                while chunk := reader.read(1024 * 1024):
                    digest.update(chunk)
                    writer.write(chunk)
                    size += len(chunk)
                    copied_bytes += len(chunk)
                    if copied_bytes > max_bytes:
                        raise ValueError("Source grew beyond the 256 MiB snapshot limit while copying")
            target.chmod(source.stat().st_mode & 0o777)
            files.append({"path": name, "size": size, "sha256": digest.hexdigest()})
        for entry in files:
            if "symlink" in entry and not (destination / entry["path"]).exists():
                raise ValueError(f"Snapshot symlink target was not captured: {entry['path']}. Mount inputs separately.")
        atomic_json(destination / ".gpuq-source.json", {"origin": str(cwd),
                    "git": git_metadata(cwd), "files": files})
        return destination
    except BaseException:
        shutil.rmtree(destination)
        raise
