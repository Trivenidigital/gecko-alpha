"""Stale-reader watchdog: find non-service processes holding scout.db open.

Root cause of the 2026-06-18 WAL-bloat incident was 2 orphaned interactive
reader processes pinning the WAL for 65+ days. A pinned WAL makes
``wal_checkpoint(TRUNCATE)`` return ``busy`` and silently ineffective — so this
watchdog is what makes that busy actionable. Linux ``/proc`` only; degrades to
``[]`` on non-Linux or an unreadable ``/proc``.

Fold 3 (gate-1 review): legitimacy is decided by an explicit expected-service
allowlist, NOT a blanket ``".service" in cgroup`` — otherwise a rogue
long-lived systemd/cron job pinning the WAL would be silently excluded.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass


@dataclass
class DbHolder:
    pid: int
    cmdline: str
    cgroup: str
    age_seconds: float
    is_expected_service: bool


def classify_is_expected_service(cgroup: str, expected_units: list[str]) -> bool:
    """True only if the holder's cgroup names one of the allowlisted units."""
    return any(unit in cgroup for unit in expected_units)


def proc_available(proc_root: str = "/proc") -> bool:
    """Whether the /proc scan can run. Lets the caller distinguish a genuine
    'zero holders' result from a blind watchdog (no readable /proc), which
    otherwise both look like an empty scan."""
    return os.path.isdir(proc_root)


def _read_text(path: str) -> str:
    try:
        with open(path, "r") as fh:
            return fh.read()
    except OSError:
        return ""


def _read_btime(proc_root: str) -> float:
    for line in _read_text(os.path.join(proc_root, "stat")).splitlines():
        if line.startswith("btime "):
            try:
                return float(line.split()[1])
            except (IndexError, ValueError):
                return 0.0
    return 0.0


def _start_epoch(proc_root: str, pid: int, btime: float, clk_tck: int) -> float:
    raw = _read_text(os.path.join(proc_root, str(pid), "stat"))
    if not raw or ")" not in raw:
        return 0.0
    # comm may contain spaces/parens; split after the final ')'. Fields then
    # start at field 3 (state); starttime is field 22 -> index 19.
    after = raw.rsplit(")", 1)[1].split()
    if len(after) <= 19:
        return 0.0
    try:
        return btime + (int(after[19]) / clk_tck)
    except (ValueError, ZeroDivisionError):
        return 0.0


def _cmdline(proc_root: str, pid: int) -> str:
    return (
        _read_text(os.path.join(proc_root, str(pid), "cmdline"))
        .replace("\x00", " ")
        .strip()
    )


def _holds_any(proc_root: str, pid: int, targets: set[str]) -> bool:
    fd_dir = os.path.join(proc_root, str(pid), "fd")
    try:
        names = os.listdir(fd_dir)
    except OSError:
        return False
    for n in names:
        try:
            tgt = os.readlink(os.path.join(fd_dir, n))
        except OSError:
            continue
        if tgt in targets or os.path.realpath(tgt) in targets:
            return True
    return False


def scan_db_holders(
    db_paths: list[str],
    *,
    proc_root: str = "/proc",
    own_pid: int | None = None,
    now: float | None = None,
    clk_tck: int | None = None,
    expected_units: list[str] | None = None,
) -> list[DbHolder]:
    """Return processes (excluding ``own_pid``) holding any of ``db_paths`` /
    ``-wal`` / ``-shm`` open. No-op (``[]``) when ``/proc`` is unavailable."""
    now = time.time() if now is None else now
    expected_units = expected_units or []
    if clk_tck is None:
        try:
            clk_tck = int(os.sysconf("SC_CLK_TCK")) or 100
        except (ValueError, OSError, AttributeError):
            clk_tck = 100
    own_pid = os.getpid() if own_pid is None else own_pid

    targets: set[str] = set()
    for p in db_paths:
        rp = os.path.realpath(p)
        for suffix in ("", "-wal", "-shm"):
            targets.add(p + suffix)
            targets.add(rp + suffix)

    try:
        entries = os.listdir(proc_root)
    except OSError:
        return []  # non-Linux / no /proc -> no-op

    btime = _read_btime(proc_root)
    holders: list[DbHolder] = []
    for entry in entries:
        if not entry.isdigit():
            continue
        pid = int(entry)
        if pid == own_pid or not _holds_any(proc_root, pid, targets):
            continue
        start = _start_epoch(proc_root, pid, btime, clk_tck)
        age = max(0.0, now - start) if start else 0.0
        cgroup = _read_text(os.path.join(proc_root, str(pid), "cgroup")).strip()
        holders.append(
            DbHolder(
                pid=pid,
                cmdline=_cmdline(proc_root, pid),
                cgroup=cgroup,
                age_seconds=age,
                is_expected_service=classify_is_expected_service(
                    cgroup, expected_units
                ),
            )
        )
    return holders


def find_stale_readers(
    holders: list[DbHolder], *, max_age_hours: float, own_pid: int
) -> list[DbHolder]:
    cutoff = max_age_hours * 3600.0
    return [
        h
        for h in holders
        if (not h.is_expected_service) and h.pid != own_pid and h.age_seconds > cutoff
    ]
