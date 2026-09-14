"""Disposable online backup helper. CLI is limited to approved private scratch."""

import argparse
from contextlib import closing
import json
import os
import re
import shutil
import sqlite3
import stat
import time
from pathlib import Path

SCRATCH = Path("/root/gecko-dashboard-release-20260914")
SOURCE = Path("/root/gecko-alpha/scout.db")


def private_directory(path):
    if path.is_symlink():
        raise ValueError("scratch symlink refused")
    path.mkdir(mode=0o700, exist_ok=True)
    info = path.stat()
    if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise ValueError("scratch requires owned mode0700 directory")


def backup(source, destination, *, seconds=120, minimum_free=4 * 1024**3):
    source, destination = Path(source), Path(destination)
    if destination.is_symlink() or source.resolve() == destination.resolve():
        raise ValueError("unsafe backup path")
    fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(fd)
    start = time.monotonic()
    deadline = start + seconds
    progress = {"remaining": None, "pages": None}

    def guard(status, remaining, pages):
        if time.monotonic() >= deadline:
            raise TimeoutError("backup deadline exceeded; partial file is ineligible")
        if shutil.disk_usage(destination.parent).free < minimum_free:
            raise OSError("backup free-space floor reached")
        progress.update(remaining=remaining, pages=pages)

    # On failure retain the partial file, never label it valid or reuse it.
    with closing(
        sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True, timeout=1)
    ) as src:
        src.execute("PRAGMA query_only=ON")
        with closing(sqlite3.connect(destination)) as dst:
            src.backup(dst, pages=256, sleep=0.05, progress=guard)
            check_deadline = time.monotonic() + 30
            dst.set_progress_handler(
                lambda: int(time.monotonic() >= check_deadline), 1000
            )
            checks = dst.execute("PRAGMA quick_check").fetchall()
            if checks != [("ok",)]:
                raise ValueError("backup quick_check failed")
    return {
        "quick_check": "ok",
        "elapsed_seconds": time.monotonic() - start,
        "destination_bytes": destination.stat().st_size,
        **progress,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate", required=True)
    args = parser.parse_args()
    if not re.fullmatch(r"[0-9a-f]{40}", args.candidate):
        raise ValueError("exact candidate SHA required")
    os.umask(0o077)
    private_directory(SCRATCH)
    directory = SCRATCH / args.candidate
    private_directory(directory)
    if SOURCE.is_symlink() or directory.resolve().parent != SCRATCH.resolve():
        raise ValueError("unexpected source/scratch link")
    if (
        shutil.disk_usage(directory).free < 12 * 1024**3
        or os.statvfs(directory).f_favail < 10000
    ):
        raise OSError("insufficient backup space/inodes")
    print(json.dumps(backup(SOURCE, directory / "baseline.sqlite"), sort_keys=True))


if __name__ == "__main__":
    main()
