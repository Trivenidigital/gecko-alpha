"""Pilot threshold watchdog — read-only observer for pre-registered gates.

WHY THIS EXISTS. The `losers_contrarian` pilot (PR #515) and the `gainers_early`
instrumentation run are deliberately DATA-BOUND: they complete when a count is
reached, not when a calendar elapses. A data-bound gate with no durable observer
is an incomplete operational design — the same shape that let six consecutive
backup failures go unactioned for a week.

WHAT IT DOES NOT DO — this is the load-bearing part.

It reports OPERATIONAL STATE ONLY: booleans and counts. It never computes P&L
trends, win rate, mean MAE, D(-10/-12/-15), C(X), or any interim cohort table.
Mid-cohort interpretation is exactly what the pre-registration exists to
prevent, and a watchdog that pages an operator with a running D(X) would defeat
it far more effectively than a human deciding to peek.

`n_eff` is reported at ONE moment only: after admissions have closed AND the
open tail has fully resolved. It is deliberately NOT alerted at `n_eff >= 120`,
because that is a power floor, not an action boundary — it can be crossed while
admissions are still running, and surfacing it early invites the inspection it
is meant to defer.

SAFETY POSTURE.
  - Opens SQLite `mode=ro`. Schema writes are refused by SQLite, not merely
    avoided by us.
  - NEVER calls `Database.initialize()`. A read-only observer entering the
    migration machinery is precisely the failure class removed in PR #520:
    the Solana cron watchdog ran ~40 migrations against a live 6.8 GB database
    every two minutes and produced 74 `database is locked` failures.
  - Does not mutate the production DB or signal state. It does not flip
    `enabled`, geometry, or sizing. It DOES write dedup marker files under
    `--state-dir` after a successful send — that is its only write. At
    admission close it PAGES that the §7.5 rollback is due; it does not perform
    it. Removing the memory dependency is a smaller change than introducing an
    autonomous mutating supervisor into a frozen experiment.

Exit codes:
  0 — no trigger fired (or disabled no-op)
  5 — one or more triggers fired (paged, cooldown-suppressed, or dry-run)
  1 — DB missing, runtime error, or alert-dispatch failure
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import aiosqlite
import structlog

_TRUTHY = {"1", "true", "yes", "on"}
_log = structlog.get_logger()

PILOT_SIGNAL = "losers_contrarian"
GAINERS_SIGNAL = "gainers_early"
MAE_MIGRATION = "bl_trade_adverse_excursion_v1"


def _configure_logging() -> None:
    """stderr only, so stdout stays clean JSON. Called from __main__ ONLY —
    configuring structlog at import time is a process-wide mutation that would
    silently empty other tests' captured logs."""
    structlog.configure(logger_factory=structlog.PrintLoggerFactory(file=sys.stderr))


def _parse_iso(raw: str | None) -> datetime | None:
    """Parse a stored isoformat timestamp; None when unparseable/absent."""
    if not raw:
        return None
    txt = raw.strip()
    if txt.endswith("Z"):
        txt = txt[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(txt)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _is_enabled(value: str) -> bool:
    return value.strip().lower() in _TRUTHY


# ----------------------------------------------------------------------
# Admission-writer counting — pure, so it is testable without live processes
# ----------------------------------------------------------------------


def count_admission_writers(procs: list[tuple[int, list[str]]], self_pid: int) -> int:
    """Count ADMISSION WRITER instances from (pid, argv) pairs.

    *** DO NOT REPLACE THIS WITH `pgrep -f` COUNTING. ***

    The pipeline runs as `uv run python -m scout.main`, which is TWO processes
    for ONE writer: the `uv` wrapper and its Python child. Both cmdlines contain
    the string `-m scout.main`, so a substring count reports 2 and a naive
    "overlap" check fires permanently.

    Worse, a checking process whose own command line mentions the pattern counts
    itself. That has already produced false readings twice during this incident
    — a `pgrep -f` residue check reported 71 where the true population was 70,
    and the extra one was the measuring shell.

    Definition used here: a writer is a process whose argv runs the module
    directly, i.e. argv[0]'s basename is a python interpreter AND `-m
    scout.main` appears in its arguments. The `uv` wrapper is excluded by
    argv[0]. `self_pid` is excluded unconditionally.
    """
    writers = 0
    for pid, argv in procs:
        if pid == self_pid or not argv:
            continue
        exe = Path(argv[0]).name.lower()
        # Strip a trailing .exe so Windows-shaped argv is handled identically.
        if exe.endswith(".exe"):
            exe = exe[:-4]
        if not (exe == "python" or exe.startswith("python3") or exe == "python3"):
            continue
        if "-m" in argv and "scout.main" in argv:
            writers += 1
    return writers


def _read_process_table() -> list[tuple[int, list[str]]]:
    """Best-effort /proc scan. Returns [] where /proc is unavailable (Windows,
    containers) — callers treat an empty table as 'writer count unknown' rather
    than as zero, so a missing /proc can never masquerade as 'no overlap'."""
    procs: list[tuple[int, list[str]]] = []
    proc_root = Path("/proc")
    if not proc_root.is_dir():
        return procs
    for entry in proc_root.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            raw = (entry / "cmdline").read_bytes()
        except (OSError, PermissionError):
            continue
        argv = [p for p in raw.decode("utf-8", "replace").split("\0") if p]
        if argv:
            procs.append((int(entry.name), argv))
    return procs


# ----------------------------------------------------------------------
# Read-only cohort state
# ----------------------------------------------------------------------


async def _pilot_anchor(conn: aiosqlite.Connection) -> tuple[str | None, int]:
    """(PILOT_T0, enabled) for the pilot signal.

    PILOT_T0 is `drawdown_baseline_at` — stamped atomically by
    `revive_signal_with_baseline` and identical to the revival audit row's
    `applied_at`. It is the same anchor `_pilot_admission_allowed()` counts
    from, so the watchdog and the cap can never disagree about cohort
    membership.
    """
    cur = await conn.execute(
        "SELECT drawdown_baseline_at, enabled FROM signal_params "
        "WHERE signal_type = ?",
        (PILOT_SIGNAL,),
    )
    row = await cur.fetchone()
    if row is None:
        return None, 0
    return row[0], int(row[1] or 0)


async def _cohort_state(conn: aiosqlite.Connection, t0: str) -> dict:
    """Counts only. No P&L aggregation beyond the K2 kill threshold, which is a
    pre-registered ABORT condition rather than a result."""
    cur = await conn.execute(
        "SELECT COUNT(*), "
        "SUM(status = 'open'), "
        "SUM(status LIKE 'closed%'), "
        "SUM(status LIKE 'closed%' AND pre_leg1_mae_pct IS NOT NULL), "
        "COALESCE(SUM(CASE WHEN status LIKE 'closed%' THEN pnl_usd END), 0) "
        "FROM paper_trades WHERE signal_type = ? AND opened_at >= ?",
        (PILOT_SIGNAL, t0),
    )
    entries, open_n, closed_n, eligible_n, net = await cur.fetchone()
    return {
        "entries": int(entries or 0),
        "open": int(open_n or 0),
        "closed": int(closed_n or 0),
        "n_eff": int(eligible_n or 0),
        "net_usd": round(float(net or 0.0), 2),
    }


async def _entry_rate_days(
    conn: aiosqlite.Connection, t0: str, days: int
) -> list[int] | None:
    """Entries per COMPLETE UTC calendar day for the last *days* days.

    *** ZERO DAYS MUST APPEAR AS ZEROS. ***

    A GROUP BY returns only dates that HAVE rows, so a true sequence of
    2, 1, 0, 2, 1 comes back as four buckets. K4 requires `days` values all
    below the threshold, so the missing zero made the criterion LESS likely to
    fire on the very day it should have fired hardest — backwards. The buckets
    are therefore pre-seeded to 0 and filled from the query.

    Temporal definition, pinned rather than left implicit: the window is the
    last *days* COMPLETE UTC calendar days, excluding today. Today is partial,
    and counting it would let a normal morning look like a collapsed day. The
    frozen rule reads "< 3/day for 5 consecutive days"; complete days are the
    only reading under which each value is comparable to the others.

    Returns None when the pilot is younger than the window — the five-day
    window does not exist yet, so K4 is inapplicable rather than satisfied.
    That is an APPLICABILITY condition (the data does not span the window), not
    a minimum-sample grace period of the kind removed from K3.
    """
    t0_dt = _parse_iso(t0)
    if t0_dt is None:
        return None
    today = datetime.now(timezone.utc).date()
    window = [today - timedelta(days=n) for n in range(days, 0, -1)]
    if t0_dt.date() > window[0]:
        return None

    lo, hi = window[0].isoformat(), (window[-1] + timedelta(days=1)).isoformat()
    cur = await conn.execute(
        "SELECT substr(opened_at, 1, 10) d, COUNT(*) FROM paper_trades "
        "WHERE signal_type = ? AND opened_at >= ? "
        "AND substr(opened_at, 1, 10) >= ? AND substr(opened_at, 1, 10) < ? "
        "GROUP BY d",
        (PILOT_SIGNAL, t0, lo, hi),
    )
    counts = {r[0]: int(r[1]) for r in await cur.fetchall()}
    return [counts.get(d.isoformat(), 0) for d in window]


async def _gainers_instrumentation_count(
    conn: aiosqlite.Connection,
) -> tuple[int, str | None]:
    """Closed gainers rows with FULLY-OBSERVED whole-life MAE.

    Anchored on the MAE migration's own `cutover_ts`, not a hand-written date.
    A trade opened BEFORE the column existed accumulates `mae_pct` only from the
    deploy onward, so its value is partial — the same partial-observation trap
    that `pre_leg1_mae_pct` was introduced to avoid. Requiring
    `opened_at >= cutover_ts` keeps 'MAE-eligible' meaning fully observed.
    """
    cur = await conn.execute(
        "SELECT cutover_ts FROM paper_migrations WHERE name = ?", (MAE_MIGRATION,)
    )
    row = await cur.fetchone()
    if row is None or not row[0]:
        return 0, None
    cutover = row[0]
    cur = await conn.execute(
        "SELECT COUNT(*) FROM paper_trades WHERE signal_type = ? "
        "AND status LIKE 'closed%' AND mae_pct IS NOT NULL AND opened_at >= ?",
        (GAINERS_SIGNAL, cutover),
    )
    # The cutover is ALSO the gainers run's dedup anchor. Keying its alert to
    # the losers PILOT_T0 would let a future losers revival re-page an already
    # completed gainers gate, while a genuinely new gainers instrumentation run
    # could never re-arm its own trigger.
    return int((await cur.fetchone())[0] or 0), cutover


# ----------------------------------------------------------------------
# Trigger evaluation — pure, given already-read state
# ----------------------------------------------------------------------


def evaluate_triggers(
    *,
    t0: str | None,
    enabled: int,
    cohort: dict,
    entry_rate: list[int] | None,
    writers: int | None,
    gainers_n: int,
    max_entries: int,
    k2_net_usd: float,
    k3_null_pct: float,
    k4_min_per_day: int,
    k4_days: int,
    k5_max_open: int,
    n_eff_floor: int,
    gainers_gate: int,
) -> list[dict]:
    """Return fired triggers. Counts and booleans only — never a verdict."""
    fired: list[dict] = []
    if t0 is None:
        return fired

    admissions_closed = enabled == 0 or cohort["entries"] >= max_entries

    # 1. Instrumentation validation — BINARY. Deliberately carries no MAE value:
    #    a single observation is not evidence about D(X), and printing it in a
    #    page is the most likely way it would get read as such.
    if cohort["n_eff"] >= 1:
        fired.append(
            {
                "trigger": "pilot_instrumentation_validated",
                "urgency": "info",
                "detail": "first post-T0 close carries pre_leg1_mae_pct — "
                "measurement pipe validated",
            }
        )

    # 2. Kill conditions. K1 (auto-suspend) is self-enforcing and already flips
    #    `enabled`; it is observed via K-class reporting rather than duplicated.
    if cohort["closed"] > 0 and cohort["net_usd"] <= k2_net_usd and not admissions_closed:
        fired.append(
            {
                "trigger": "K2_net_loss",
                "urgency": "urgent",
                "detail": f"cohort net {cohort['net_usd']} <= {k2_net_usd} "
                "before admission close — halt + rollback",
            }
        )

    # NO minimum-sample floor. An earlier revision only evaluated K3 once 20
    # trades had closed — an unauthorized weakening of a FROZEN kill criterion.
    # The registered rule is "NULL rate > 5% on new eligible closes → halt",
    # full stop. A first post-T0 close carrying NULL IS an instrumentation
    # failure worth surfacing; under the floor it could stay silent for another
    # 19 closes, which is exactly the window in which the pilot's only
    # deliverable quietly stops being produced. Amending K3 is an operator
    # decision about the pilot, not something this observer may redefine.
    if cohort["closed"] > 0:
        null_pct = 100.0 * (cohort["closed"] - cohort["n_eff"]) / cohort["closed"]
        if null_pct > k3_null_pct:
            fired.append(
                {
                    "trigger": "K3_instrumentation_broken",
                    "urgency": "urgent",
                    "detail": f"{null_pct:.1f}% of closes lack pre_leg1_mae_pct "
                    f"(> {k3_null_pct}%) — the pilot's only deliverable is void",
                }
            )

    # `entry_rate is None` = window does not exist yet (pilot younger than it).
    if (
        not admissions_closed
        and entry_rate is not None
        and len(entry_rate) == k4_days
        and all(d < k4_min_per_day for d in entry_rate)
    ):
        fired.append(
            {
                "trigger": "K4_entry_rate_collapsed",
                "urgency": "urgent",
                "detail": f"< {k4_min_per_day}/day for {k4_days} consecutive days "
                f"— {max_entries} entries unreachable in a sane window",
            }
        )

    if cohort["open"] > k5_max_open:
        fired.append(
            {
                "trigger": "K5_concurrency_tripwire",
                "urgency": "urgent",
                "detail": f"{cohort['open']} open > {k5_max_open} — monitored "
                "tripwire breached, halt + rollback",
            }
        )

    # K6 — from DB/process state, not from whether a log line survived rotation.
    # Cohort overshoot IS the durable form of `pilot_entry_cap_exceeded`.
    if cohort["entries"] > max_entries:
        fired.append(
            {
                "trigger": "K6_cohort_overshoot",
                "urgency": "urgent",
                "detail": f"{cohort['entries']} entries > cap {max_entries} — "
                "concurrent admission writers suspected; cohort is not the "
                "pre-registered n",
            }
        )
    if writers is not None and writers > 1:
        fired.append(
            {
                "trigger": "K6_multiple_admission_writers",
                "urgency": "urgent",
                "detail": f"{writers} admission writers — the entry cap is exact "
                "only under one",
            }
        )

    # 3. Admission close — §7.5 rollback is due. Not performed here.
    if cohort["entries"] >= max_entries and enabled == 1:
        fired.append(
            {
                "trigger": "pilot_admission_close_rollback_due",
                "urgency": "urgent",
                "detail": f"{cohort['entries']}/{max_entries} entries and still "
                "enabled=1 — §7.5 requires enabled=0 before analysis",
            }
        )

    # 4. Tail resolved. The ONLY point at which n_eff is reported.
    if admissions_closed and cohort["open"] == 0 and cohort["entries"] > 0:
        fired.append(
            {
                "trigger": "pilot_tail_resolved",
                "urgency": "info",
                "detail": f"admissions closed, tail resolved; n_eff="
                f"{cohort['n_eff']} "
                f"({'>=' if cohort['n_eff'] >= n_eff_floor else '<'} "
                f"{n_eff_floor} → "
                f"{'verdict may proceed' if cohort['n_eff'] >= n_eff_floor else 'NO_VERDICT'})",
            }
        )

    # 5. Gainers instrumentation gate.
    if gainers_n >= gainers_gate:
        fired.append(
            {
                "trigger": "gainers_instrumentation_gate",
                "urgency": "info",
                "detail": f"{gainers_n}/{gainers_gate} MAE-eligible closes — "
                "halt admissions and analyse (whole-life MAE is NOT valid for "
                "initial stop width)",
            }
        )

    return fired


# ----------------------------------------------------------------------
# Dedup + delivery
# ----------------------------------------------------------------------


def _state_key(trigger: str, anchor: str) -> str:
    """Dedup key = trigger + pilot/run anchor. Re-arming on a NEW pilot is
    automatic because the anchor changes; a re-fire within one pilot is
    suppressed."""
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in anchor)[:40]
    return f"fired_{trigger}_{safe}"


def _already_fired(state_dir: str, key: str) -> bool:
    return (Path(state_dir) / key).exists()


def _mark_fired(state_dir: str, key: str, now: datetime) -> None:
    d = Path(state_dir)
    d.mkdir(parents=True, exist_ok=True)
    (d / key).write_text(now.isoformat())


def _compose(fired: list[dict]) -> str:
    """Plain text. NO Markdown: signal names carry underscores and Telegram's
    MarkdownV1 renders them as italics, returning HTTP 200 with a mangled body
    (global CLAUDE.md §12b)."""
    urgent = [f for f in fired if f["urgency"] == "urgent"]
    head = (
        "PILOT THRESHOLD — ACTION REQUIRED"
        if urgent
        else "PILOT THRESHOLD — informational"
    )
    lines = [head, ""]
    for f in fired:
        mark = "!!" if f["urgency"] == "urgent" else "--"
        lines.append(f"{mark} {f['trigger']}")
        lines.append(f"   {f['detail']}")
    lines.append("")
    lines.append("Operational state only. No verdict computed.")
    return "\n".join(lines)


async def _send_via_alerter(text: str) -> None:
    """Real plain-text Telegram send. Lazy heavy imports (aiohttp + alerter).

    `raise_on_failure=True` is LOAD-BEARING (§12b). The default alerter SWALLOWS
    non-200 and network errors — it logs a warning and returns. Without the flag
    the `_alert_delivered` log below would fire even when Telegram rejected the
    page, and worse, `main()` would then write dedup state marking an
    UNDELIVERED alert as delivered — permanently suppressing that trigger for
    the life of the pilot. A watchdog that silently loses its own page is worse
    than no watchdog.

    `parse_mode=None`: trigger names carry underscores (`K5_concurrency_tripwire`)
    which MarkdownV1 renders as italics, mangling the body while Telegram still
    returns HTTP 200.
    """
    import aiohttp

    from scout.alerter import send_telegram_message
    from scout.config import Settings

    settings = Settings()
    async with aiohttp.ClientSession() as session:
        await send_telegram_message(
            text,
            session,
            settings,
            parse_mode=None,
            raise_on_failure=True,
            source="pilot_threshold_watchdog",
        )


# Indirection point so tests can exercise the dispatch seam without network.
_SEND = _send_via_alerter


async def _send(text: str) -> None:
    _log.info("pilot_watchdog_alert_dispatched", chars=len(text))
    await _SEND(text)
    _log.info("pilot_watchdog_alert_delivered")


async def _evaluate(db_path: str, args) -> dict:
    async with aiosqlite.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
        t0, enabled = await _pilot_anchor(conn)
        cohort = (
            await _cohort_state(conn, t0)
            if t0
            else {"entries": 0, "open": 0, "closed": 0, "n_eff": 0, "net_usd": 0.0}
        )
        rate = await _entry_rate_days(conn, t0, args.k4_days) if t0 else None
        gainers_n, gainers_anchor = await _gainers_instrumentation_count(conn)

    procs = _read_process_table()
    writers = count_admission_writers(procs, self_pid=0) if procs else None

    fired = evaluate_triggers(
        t0=t0,
        enabled=enabled,
        cohort=cohort,
        entry_rate=rate,
        writers=writers,
        gainers_n=gainers_n,
        max_entries=args.max_entries,
        k2_net_usd=args.k2_net_usd,
        k3_null_pct=args.k3_null_pct,
        k4_min_per_day=args.k4_min_per_day,
        k4_days=args.k4_days,
        k5_max_open=args.k5_max_open,
        n_eff_floor=args.n_eff_floor,
        gainers_gate=args.gainers_gate,
    )
    return {
        "pilot_t0": t0,
        "enabled": enabled,
        "cohort": cohort,
        "writers": writers,
        "gainers_n": gainers_n,
        "gainers_anchor": gainers_anchor,
        "fired": fired,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="scout.db")
    parser.add_argument("--enabled", default="false")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--state-dir", default="/var/lib/gecko-alpha/pilot-watchdog")
    parser.add_argument("--max-entries", type=int, default=200)
    parser.add_argument("--k2-net-usd", type=float, default=-400.0)
    parser.add_argument("--k3-null-pct", type=float, default=5.0)
    parser.add_argument("--k4-min-per-day", type=int, default=3)
    parser.add_argument("--k4-days", type=int, default=5)
    parser.add_argument("--k5-max-open", type=int, default=60)
    parser.add_argument("--n-eff-floor", type=int, default=120)
    parser.add_argument("--gainers-gate", type=int, default=100)
    args = parser.parse_args(argv)

    if not _is_enabled(args.enabled) and not args.dry_run:
        print(json.dumps({"ok": True, "disabled": True}))
        return 0

    db_path = Path(args.db).expanduser()
    if not db_path.exists():
        print(json.dumps({"ok": False, "error": "db_not_found", "db": str(db_path)}))
        return 1

    try:
        result = asyncio.run(_evaluate(str(db_path), args))
    except Exception as exc:  # noqa: BLE001
        _log.exception("pilot_watchdog_error")
        print(json.dumps({"ok": False, "error": str(exc)}))
        return 1

    fired = result["fired"]
    pilot_anchor = result["pilot_t0"] or "no-pilot"
    gainers_anchor = result["gainers_anchor"] or "no-gainers-run"

    def _anchor_for(trigger: str) -> str:
        """Each trigger dedups against ITS OWN run, not a shared timestamp."""
        return gainers_anchor if trigger.startswith("gainers_") else pilot_anchor

    to_send = [
        f
        for f in fired
        if not _already_fired(args.state_dir, _state_key(f["trigger"], _anchor_for(f["trigger"])))
    ]

    result["suppressed"] = len(fired) - len(to_send)
    if to_send and not args.dry_run:
        try:
            asyncio.run(_send(_compose(to_send)))
        except Exception as exc:  # noqa: BLE001
            _log.exception("pilot_watchdog_dispatch_failed")
            result["dispatch_error"] = str(exc)
            print(json.dumps({"ok": False, **result}, default=str))
            return 1
        now = datetime.now(timezone.utc)
        for f in to_send:
            _mark_fired(
                args.state_dir, _state_key(f["trigger"], _anchor_for(f["trigger"])), now
            )

    result["ok"] = True
    result["dispatched"] = len(to_send) if not args.dry_run else 0
    print(json.dumps(result, default=str))
    return 5 if fired else 0


if __name__ == "__main__":
    _configure_logging()
    sys.exit(main())
