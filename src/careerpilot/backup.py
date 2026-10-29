"""Stopped-service SQLite backups and non-overwriting restore for local operations."""

import argparse
import json
import shutil
import sqlite3
from pathlib import Path

FILES = (
    "business.sqlite3",
    "security.sqlite3",
    "memory.sqlite3",
    "checkpoints.sqlite3",
    "simulation.sqlite3",
    "conversations.sqlite3",
    "agent-checkpoints.sqlite3",
)


def backup(source: Path, target: Path, *, confirmed_stopped: bool) -> None:
    if not confirmed_stopped or target.exists() or not (source / "business.sqlite3").is_file():
        raise ValueError("Stop service, use a fresh backup directory and an existing data root")
    target.mkdir(mode=0o700, parents=True)
    copied = []
    for name in FILES:
        path = source / name
        if not path.exists():
            continue
        if path.is_symlink():
            raise ValueError("Backup does not follow symlinks")
        original = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
        destination = sqlite3.connect(target / name)
        try:
            if original.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ValueError("Storage integrity check failed")
            original.backup(destination)
        finally:
            original.close()
            destination.close()
        copied.append(name)
    jobs = source / "jobs"
    if jobs.exists():
        if jobs.is_symlink() or any(path.is_symlink() for path in jobs.iterdir()):
            raise ValueError("Backup does not follow symlinks")
        shutil.copytree(jobs, target / "jobs")
    (target / "manifest.json").write_text(
        json.dumps({"format": 1, "business_schema": 3, "files": copied}, indent=2)
    )


def restore(source: Path, target: Path) -> None:
    manifest = json.loads((source / "manifest.json").read_text())
    if target.exists() or manifest.get("format") != 1 or manifest.get("business_schema") != 3:
        raise ValueError("Restore requires a fresh target and supported backup manifest")
    target.mkdir(mode=0o700, parents=True)
    for name in manifest["files"]:
        if name not in FILES or (source / name).is_symlink():
            raise ValueError("Unsafe backup entry")
        shutil.copyfile(source / name, target / name)
    if (source / "jobs").exists():
        if (source / "jobs").is_symlink() or any(
            path.is_symlink() for path in (source / "jobs").iterdir()
        ):
            raise ValueError("Unsafe job definitions")
        shutil.copytree(source / "jobs", target / "jobs")


def main() -> None:
    parser = argparse.ArgumentParser(description="Stopped-service local backup/restore")
    parser.add_argument("operation", choices=("backup", "restore"))
    parser.add_argument("source", type=Path)
    parser.add_argument("target", type=Path)
    parser.add_argument("--confirm-stopped", action="store_true")
    args = parser.parse_args()
    if args.operation == "backup":
        backup(args.source, args.target, confirmed_stopped=args.confirm_stopped)
    else:
        restore(args.source, args.target)
    print("Storage operation completed; validate readiness before service cutover.")
