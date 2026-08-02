"""Safety-critical database resolution: absolute, existing, and actually ours.

The trap this closes
--------------------
SQLite CREATES a database for any path it is handed. ``DB_PATH`` defaults to the
RELATIVE ``scout.db``, so the identity of the database every safety mechanism
reads is a function of the process's working directory. Run a lane from ``/root``
instead of ``/root/gecko-alpha`` and sqlite happily opens a brand-new empty file
where:

    no positions · no prior signatures · no daily gross · kill switch clear

Every gate reports "all clear" **because it is reading the wrong database**. This
is not hypothetical: a zero-byte ``/root/scout.db`` sat on the production host from
2026-05-15 until 2026-08-02, created by exactly this mistake.

Why the pre-existing guards were not enough
-------------------------------------------
``kraken_pilot`` and ``solana_lane`` each checked ``db_path.exists()``. That
catches "no file" and misses the case that actually occurred: the file DOES exist,
it is simply EMPTY. ``.exists()`` returns True for a zero-byte file, so the lane
proceeds — into precisely the all-clear state described above.

Two rules, and the first is the important one
---------------------------------------------
1. **A relative DB_PATH resolves against the DEPLOYMENT ROOT, never the process
   working directory.** Today the deployment works only because the systemd units
   set ``WorkingDirectory`` and the cron line begins ``cd /root/gecko-alpha &&``.
   That makes correctness ambient — a property of how something was invoked rather
   than of what it is. Anchoring to the package location removes cwd from the
   answer entirely, and does it without requiring an operator to change ``.env``.
   An absolute ``DB_PATH`` is used exactly as given.

2. **The resolved file must prove it is a real, migrated gecko database** before
   any safety-critical path reads state from it: non-empty, a genuine SQLite file,
   carrying the migration bookkeeping and the core tables. Checked READ-ONLY via a
   ``file:...?mode=ro`` URI, because a guard that could create the database it is
   checking for would be the bug wearing a hat.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

__all__ = [
    "DEPLOYMENT_ROOT",
    "REQUIRED_TABLES",
    "UnsafeDatabase",
    "resolve_db_path",
    "assert_safe_database",
    "assert_creatable_database",
    "describe_database",
]

#: The directory containing the ``scout`` package — i.e. the deployment root.
#: ``scout/db_path.py`` -> ``scout/`` -> the root. Derived from the module's own
#: location so it is correct under an editable install, a checkout, or a copy,
#: and is never a function of how the process was launched.
DEPLOYMENT_ROOT = Path(__file__).resolve().parent.parent

#: Tables whose ABSENCE means the safety story cannot be told at all:
#: ``kill_events`` is the kill switch, ``live_trades`` is the money ledger,
#: ``paper_trades`` is the FK parent every live row hangs off. A database missing
#: any of these is not a gecko database that happens to be behind on migrations;
#: it is a different file.
REQUIRED_TABLES = ("kill_events", "live_trades", "paper_trades")

#: Bookkeeping tables that prove ``Database.initialize()`` has run. Required to be
#: present AND non-empty: an empty ``paper_migrations`` means the schema was
#: created but no migration ever recorded itself, which is the shape a
#: hand-made or truncated file has.
_MIGRATION_TABLES = ("paper_migrations", "schema_version")

#: Every SQLite database file begins with this, including one with no tables.
#: A zero-byte file has no header at all — which is why size is checked first.
_SQLITE_MAGIC = b"SQLite format 3\x00"


class UnsafeDatabase(RuntimeError):
    """The configured database must not be used for safety-critical work.

    ``reason`` is a short stable token (``relative_unresolvable``, ``missing``,
    ``empty``, ``not_a_file``, ``not_sqlite``, ``unmigrated``, ``missing_tables``,
    ``unreadable``) so callers can branch or log without parsing prose, while the
    message stays long enough for an operator to act on at 3am.
    """

    def __init__(self, reason: str, message: str, *, path: Path | None = None):
        super().__init__(message)
        self.reason = reason
        self.message = message
        self.path = path


def resolve_db_path(raw: str | Path) -> Path:
    """Absolute path for ``raw``, anchored to the deployment root when relative.

    Pure and side-effect free — it does not touch the filesystem, so it is safe to
    call in a log line or an error message about a database that may not exist.
    """
    path = Path(raw).expanduser()
    if path.is_absolute():
        return path
    return (DEPLOYMENT_ROOT / path).resolve()


def describe_database(raw: str | Path) -> dict[str, object]:
    """Everything an operator needs to see about which database is in play.

    Deliberately never raises: it is what an error path and a boot log both call,
    and a diagnostic that can fail is a diagnostic that vanishes exactly when it
    is needed.
    """
    try:
        resolved = resolve_db_path(raw)
    except Exception:  # pragma: no cover — Path() is total for str/Path
        return {"configured": str(raw), "resolved": None, "exists": False}
    try:
        exists = resolved.is_file()
        size = resolved.stat().st_size if exists else 0
    except OSError:
        exists, size = False, 0
    return {
        "configured": str(raw),
        "resolved": str(resolved),
        "was_relative": not Path(raw).expanduser().is_absolute(),
        "deployment_root": str(DEPLOYMENT_ROOT),
        "exists": exists,
        "size_bytes": size,
    }


def assert_safe_database(
    raw: str | Path,
    *,
    purpose: str = "safety-critical execution",
    require_tables: tuple[str, ...] = REQUIRED_TABLES,
) -> Path:
    """Return the absolute path, or raise :class:`UnsafeDatabase`.

    Checks run cheapest-and-most-fundamental first, so the reported reason is the
    most actionable one rather than whichever incidental check tripped.
    """
    resolved = resolve_db_path(raw)
    hint = (
        f"\n  configured DB_PATH : {raw}"
        f"\n  resolved to        : {resolved}"
        f"\n  deployment root    : {DEPLOYMENT_ROOT}"
    )

    if not resolved.exists():
        raise UnsafeDatabase(
            "missing",
            f"no database at {resolved} — refusing {purpose}. SQLite would CREATE "
            "one here, and every safety check would then read an empty file as "
            "'all clear': no kill switch, no open positions, no daily gross." + hint,
            path=resolved,
        )
    if not resolved.is_file():
        raise UnsafeDatabase(
            "not_a_file",
            f"{resolved} is not a regular file — refusing {purpose}." + hint,
            path=resolved,
        )

    try:
        size = resolved.stat().st_size
    except OSError as exc:
        raise UnsafeDatabase(
            "unreadable",
            f"cannot stat {resolved}: {exc} — refusing {purpose}." + hint,
            path=resolved,
        ) from exc

    if size == 0:
        # *** THE CASE THAT ACTUALLY HAPPENED. ***
        # `.exists()` is True for a zero-byte file, which is why the previous
        # guards let this through for months.
        raise UnsafeDatabase(
            "empty",
            f"{resolved} is ZERO BYTES — refusing {purpose}. An empty file is not "
            "a database with nothing in it; it is the signature of a process that "
            "opened the wrong path and had sqlite create one. Reading it would "
            "report no positions, no prior signatures and a clear kill switch, all "
            "of which would be false." + hint,
            path=resolved,
        )

    try:
        with resolved.open("rb") as fh:
            header = fh.read(len(_SQLITE_MAGIC))
    except OSError as exc:
        raise UnsafeDatabase(
            "unreadable",
            f"cannot read {resolved}: {exc} — refusing {purpose}." + hint,
            path=resolved,
        ) from exc
    if header != _SQLITE_MAGIC:
        raise UnsafeDatabase(
            "not_sqlite",
            f"{resolved} does not begin with the SQLite file header — refusing "
            f"{purpose}. This is not a database." + hint,
            path=resolved,
        )

    # READ-ONLY. A guard that can create or modify the thing it is guarding is
    # not a guard; `mode=ro` makes that structural rather than careful.
    try:
        conn = sqlite3.connect(f"file:{resolved}?mode=ro", uri=True, timeout=5.0)
    except sqlite3.Error as exc:
        raise UnsafeDatabase(
            "unreadable",
            f"cannot open {resolved} read-only: {exc} — refusing {purpose}." + hint,
            path=resolved,
        ) from exc
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        present = {r[0] for r in rows}

        missing_core = [t for t in require_tables if t not in present]
        if missing_core:
            raise UnsafeDatabase(
                "missing_tables",
                f"{resolved} is missing core table(s) {missing_core} — refusing "
                f"{purpose}. This is not the gecko database, or it was never "
                "initialized." + hint,
                path=resolved,
            )

        missing_bookkeeping = [t for t in _MIGRATION_TABLES if t not in present]
        if missing_bookkeeping:
            raise UnsafeDatabase(
                "unmigrated",
                f"{resolved} has no {missing_bookkeeping} — refusing {purpose}. "
                "Nothing records that this database has ever been migrated, so its "
                "schema cannot be trusted to match this code." + hint,
                path=resolved,
            )
        for table in _MIGRATION_TABLES:
            (count,) = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
            if count == 0:
                raise UnsafeDatabase(
                    "unmigrated",
                    f"{resolved} has an EMPTY {table} — refusing {purpose}. The "
                    "tables exist but no migration ever recorded itself, which is "
                    "the shape of a hand-made or truncated file." + hint,
                    path=resolved,
                )
    except sqlite3.Error as exc:
        raise UnsafeDatabase(
            "unreadable",
            f"cannot inspect {resolved}: {exc} — refusing {purpose}." + hint,
            path=resolved,
        ) from exc
    finally:
        conn.close()

    return resolved


def assert_creatable_database(
    raw: str | Path, *, purpose: str = "pipeline startup"
) -> Path:
    """Resolve for a path that may legitimately not exist yet.

    The pipeline must be able to create its database on a FRESH install, so
    "missing" cannot be an error at boot the way it is for an operator lane —
    there, a missing file always means the wrong directory, because the lane only
    ever runs against a deployment that already exists.

    What is never legitimate, at boot or anywhere else, is a file that EXISTS and
    is not a database: zero bytes, or missing the SQLite header. Those are the
    fingerprints of the wrong-path mistake rather than of a first run, and letting
    startup "recover" by migrating an empty file into a full schema is how a
    process ends up serving a real workload from a database with no history in it.

    Returns the absolute path, resolved against the deployment root when relative
    — so which database gets created, or opened, stops depending on cwd.
    """
    resolved = resolve_db_path(raw)
    if not resolved.exists():
        return resolved  # fresh install; Database.initialize() will create it

    hint = (
        f"\n  configured DB_PATH : {raw}"
        f"\n  resolved to        : {resolved}"
        f"\n  deployment root    : {DEPLOYMENT_ROOT}"
    )
    if not resolved.is_file():
        raise UnsafeDatabase(
            "not_a_file",
            f"{resolved} exists but is not a regular file — refusing {purpose}." + hint,
            path=resolved,
        )
    try:
        size = resolved.stat().st_size
        header = b"" if size == 0 else resolved.open("rb").read(len(_SQLITE_MAGIC))
    except OSError as exc:
        raise UnsafeDatabase(
            "unreadable",
            f"cannot read {resolved}: {exc} — refusing {purpose}." + hint,
            path=resolved,
        ) from exc

    if size == 0:
        raise UnsafeDatabase(
            "empty",
            f"{resolved} exists and is ZERO BYTES — refusing {purpose}. This is "
            "the fingerprint of a process that opened the wrong path, not of a "
            "first run. Migrating it would build a full schema with no history, "
            "and every safety check would then read 'all clear' from it." + hint,
            path=resolved,
        )
    if header != _SQLITE_MAGIC:
        raise UnsafeDatabase(
            "not_sqlite",
            f"{resolved} exists but does not begin with the SQLite file header — "
            f"refusing {purpose}." + hint,
            path=resolved,
        )
    return resolved
