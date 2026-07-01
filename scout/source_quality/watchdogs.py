"""Silent-failure watchdogs for the X price-snapshot pipeline (design #392 C4 §4.5).

Read-only evaluation over ``source_calls`` + ``source_call_price_snapshots`` +
``source_call_price_snapshot_runs``. Returns structured findings; a separate
default-off cron script alerts on non-``ok`` findings (``parse_mode=None`` + §12b
dispatched/delivered logs).

**Suppression is load-bearing.** The C2 snapshot writer is default-off
(deploy-without-activate), so "writer never ran" must yield ``suppressed``, not
``alert`` — the watchdogs only alarm once the writer has actually produced runs.
This mirrors ``scripts/check_source_calls_lag.py``'s pending-vs-broken split.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import aiosqlite

from scout.source_quality.ledger import (
    _priceable_identity,
    compute_x_price_coverage,
    parse_utc,
)


@dataclass(frozen=True)
class WatchdogFinding:
    check: str
    status: str  # 'ok' | 'alert' | 'suppressed'
    detail: dict[str, Any]


async def _latest_run(conn: aiosqlite.Connection):
    cur = await conn.execute(
        "SELECT ran_at, snapshots_written FROM source_call_price_snapshot_runs "
        "ORDER BY ran_at DESC LIMIT 1"
    )
    return await cur.fetchone()


async def _recent_runs(conn: aiosqlite.Connection, limit: int):
    cur = await conn.execute(
        "SELECT identities_seen, provider_errors "
        "FROM source_call_price_snapshot_runs ORDER BY ran_at DESC LIMIT ?",
        (limit,),
    )
    return await cur.fetchall()


async def evaluate_snapshot_watchdogs(
    conn: aiosqlite.Connection,
    *,
    now: datetime | None = None,
    writer_staleness_min: int = 30,
    provider_error_rate_alert: float = 0.5,
    matured_all_null_alert: int = 1,
    horizon_hours: int = 28,
    recent_runs: int = 5,
) -> list[WatchdogFinding]:
    """Evaluate the C4 coverage/freshness/provider-error watchdogs (read-only)."""
    now_dt = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    horizon_cutoff = now_dt - timedelta(hours=horizon_hours)
    findings: list[WatchdogFinding] = []

    latest = await _latest_run(conn)
    runs_exist = latest is not None

    # Active eligible_contract X calls still inside the forward horizon.
    cur = await conn.execute(
        "SELECT contract_address, chain, token_id, call_ts FROM source_calls "
        "WHERE source_type='x' AND resolved_state='eligible_contract' "
        "AND outcome_status IN ('pending','partial')"
    )
    eligible_rows = [
        r
        for r in await cur.fetchall()
        if (cts := parse_utc(r["call_ts"])) is not None and cts >= horizon_cutoff
    ]
    active_eligible = len(eligible_rows)

    # 1. writer_freshness — has the writer run recently?
    if not runs_exist:
        findings.append(
            WatchdogFinding(
                "writer_freshness", "suppressed", {"reason": "writer_never_ran"}
            )
        )
    else:
        last_ran = parse_utc(latest["ran_at"])
        age_min = (now_dt - last_ran).total_seconds() / 60.0 if last_ran else None
        if age_min is not None and age_min > writer_staleness_min:
            findings.append(
                WatchdogFinding(
                    "writer_freshness",
                    "alert",
                    {
                        "last_ran_at": latest["ran_at"],
                        "age_min": round(age_min, 1),
                        "threshold_min": writer_staleness_min,
                    },
                )
            )
        else:
            findings.append(
                WatchdogFinding(
                    "writer_freshness", "ok", {"last_ran_at": latest["ran_at"]}
                )
            )

    # 2. fresh_calls_no_snapshots — active calls but the last run produced zero.
    if active_eligible == 0:
        findings.append(
            WatchdogFinding("fresh_calls_no_snapshots", "ok", {"active_eligible": 0})
        )
    elif not runs_exist:
        findings.append(
            WatchdogFinding(
                "fresh_calls_no_snapshots",
                "suppressed",
                {"reason": "writer_never_ran", "active_eligible": active_eligible},
            )
        )
    elif latest["snapshots_written"] == 0:
        findings.append(
            WatchdogFinding(
                "fresh_calls_no_snapshots",
                "alert",
                {"active_eligible": active_eligible, "last_snapshots_written": 0},
            )
        )
    else:
        findings.append(
            WatchdogFinding(
                "fresh_calls_no_snapshots", "ok", {"active_eligible": active_eligible}
            )
        )

    # 3. eligible_no_snapshots — per-call coverage gap.
    cur = await conn.execute(
        "SELECT DISTINCT identity_key FROM source_call_price_snapshots "
        "WHERE identity_kind='contract'"
    )
    snap_keys = {row["identity_key"] for row in await cur.fetchall()}
    gap = 0
    for r in eligible_rows:
        ident = _priceable_identity(r)
        if ident is not None and ident[0] == "contract" and ident[1] not in snap_keys:
            gap += 1
    if not runs_exist:
        findings.append(
            WatchdogFinding(
                "eligible_no_snapshots",
                "suppressed",
                {"reason": "writer_never_ran", "count": gap},
            )
        )
    elif gap > 0:
        findings.append(
            WatchdogFinding("eligible_no_snapshots", "alert", {"count": gap})
        )
    else:
        findings.append(WatchdogFinding("eligible_no_snapshots", "ok", {"count": 0}))

    # 4. matured_all_null — matured resolved-identity calls with no price/forward.
    cov = await compute_x_price_coverage(conn, now=now_dt)
    if cov.matured_all_null >= matured_all_null_alert:
        findings.append(
            WatchdogFinding(
                "matured_all_null", "alert", {"count": cov.matured_all_null}
            )
        )
    else:
        findings.append(
            WatchdogFinding("matured_all_null", "ok", {"count": cov.matured_all_null})
        )

    # 5. provider_error_spike — provider-error rate over recent runs.
    recent = await _recent_runs(conn, recent_runs)
    total_ident = sum(row["identities_seen"] for row in recent)
    total_err = sum(row["provider_errors"] for row in recent)
    if not recent or total_ident == 0:
        findings.append(
            WatchdogFinding(
                "provider_error_spike",
                "suppressed",
                {"reason": "no_runs_or_identities"},
            )
        )
    else:
        rate = total_err / total_ident
        if rate >= provider_error_rate_alert:
            findings.append(
                WatchdogFinding(
                    "provider_error_spike",
                    "alert",
                    {
                        "rate": round(rate, 3),
                        "provider_errors": total_err,
                        "identities": total_ident,
                    },
                )
            )
        else:
            findings.append(
                WatchdogFinding("provider_error_spike", "ok", {"rate": round(rate, 3)})
            )

    return findings
