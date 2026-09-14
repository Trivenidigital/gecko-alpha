"""Bounded recorded-cohort diagnostics; no prices, policy or initializer."""

import asyncio
import json
import sqlite3
import time
from collections import Counter
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

LEDGER_LIMIT = 250_000
DECISION_LIMIT = 50_000
TOKEN_LIMIT = 20_000
SIGNAL_LIMIT = 256
READ_SECONDS = 5.0


class ReadLimit(Exception):
    pass


def unavailable(reason):
    return {"meta": {"ok": False, "reason": reason, "observed_at": None}}


def _stamp(value):
    if not isinstance(value, str) or len(value) > 80 or len(value) < 19:
        return None
    try:
        parsed = datetime.fromisoformat(value)
        return (
            parsed.replace(tzinfo=timezone.utc)
            if parsed.tzinfo is None
            else parsed.astimezone(timezone.utc)
        )
    except (ValueError, OverflowError):
        return None


def _key(value):
    if isinstance(value, str) and len(value) > 128:
        raise ReadLimit()
    return (
        value
        if isinstance(value, str)
        and value.strip()
        and not any(ord(c) < 32 for c in value)
        else None
    )


def read_health(path, *, now=None):
    """Read one retained snapshot, failing closed on bounded-resource errors."""
    deadline = time.monotonic() + READ_SECONDS
    now = now or datetime.now(timezone.utc)
    start7, start30 = now - timedelta(days=7), now - timedelta(days=30)

    def check():
        if time.monotonic() >= deadline:
            raise ReadLimit()

    try:
        with closing(
            sqlite3.connect(
                Path(path).resolve().as_uri() + "?mode=ro", uri=True, timeout=0.25
            )
        ) as conn:
            conn.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, 65536)
            conn.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
            conn.execute("PRAGMA query_only=ON")
            conn.execute("BEGIN")
            check()
            cohort = dict(
                rows=0,
                distinct_tokens=0,
                recorded_r24h=0,
                recorded_r7d=0,
                earliest_anchor_tokens_with_recorded_r7d=0,
                invalid_token_rows=0,
            )
            diagnostics = dict(
                broader_selected_gated_out_rows=0,
                malformed_verdict_rows=0,
                excluded_ledger_timestamp_rows=0,
                excluded_decision_timestamp_rows=0,
            )
            labels = Counter(
                {
                    "complete": 0,
                    "partial": 0,
                    "pending": 0,
                    "unlabelable": 0,
                    "unknown": 0,
                }
            )
            anchors, samples, decisions = {}, Counter(), Counter()

            def signal_key(raw):
                key = _key(raw)
                if (
                    key not in samples
                    and key not in decisions
                    and len(samples.keys() | decisions.keys()) >= SIGNAL_LIMIT
                ):
                    raise ReadLimit()
                return key

            def rows(sql, args, limit):
                cursor = conn.execute(sql, args)
                count = 0
                try:
                    while True:
                        check()
                        batch = cursor.fetchmany(128)
                        check()
                        if not batch:
                            return
                        for row in batch:
                            check()
                            count += 1
                            if count > limit:
                                raise ReadLimit()
                            yield row
                finally:
                    cursor.close()

            sql = """SELECT id,token_id,surface,gate_verdicts,emitted_at,
                     r24h IS NOT NULL,r7d IS NOT NULL,label_status
                     FROM signal_outcome_ledger WHERE kind='gated_out_sample'
                     AND julianday(emitted_at)>=julianday(?)-1.0/86400
                     AND julianday(emitted_at)<julianday(?)+1.0/86400"""
            for ident, token, surface, raw, emitted, r24, r7, status in rows(
                sql, (start30.isoformat(), now.isoformat()), LEDGER_LIMIT
            ):
                if type(ident) is not int:
                    return unavailable("schema_unavailable")
                stamp = _stamp(emitted)
                if stamp is None:
                    diagnostics["excluded_ledger_timestamp_rows"] += 1
                    continue
                if not start30 <= stamp < now:
                    continue
                diagnostics["broader_selected_gated_out_rows"] += 1
                if isinstance(raw, (str, bytes)) and len(raw) > 16384:
                    raise ReadLimit()
                try:
                    verdict = json.loads(raw) if isinstance(raw, str) else None
                except (ValueError, RecursionError):
                    verdict = None
                if not isinstance(verdict, dict):
                    diagnostics["malformed_verdict_rows"] += 1
                    continue
                if (
                    verdict.get("reason") != "suppressed"
                    or verdict.get("source_layer") != "dispatcher"
                ):
                    continue
                cohort["rows"] += 1
                cohort["recorded_r24h"] += r24
                cohort["recorded_r7d"] += r7
                labels[
                    (
                        status
                        if isinstance(status, str) and status in labels
                        else "unknown"
                    )
                ] += 1
                token = _key(token)
                if token is None:
                    cohort["invalid_token_rows"] += 1
                else:
                    if token not in anchors and len(anchors) >= TOKEN_LIMIT:
                        raise ReadLimit()
                    anchor = (stamp, ident, r7)
                    if token not in anchors or anchor[:2] < anchors[token][:2]:
                        anchors[token] = anchor
                if stamp >= start7:
                    samples[signal_key(surface)] += 1
            sql = """SELECT signal_type,created_at FROM trade_decision_events
                     WHERE reason='suppressed'
                     AND julianday(created_at)>=julianday(?)-1.0/86400
                     AND julianday(created_at)<julianday(?)+1.0/86400"""
            for surface, emitted in rows(
                sql, (start7.isoformat(), now.isoformat()), DECISION_LIMIT
            ):
                stamp = _stamp(emitted)
                if stamp is None:
                    diagnostics["excluded_decision_timestamp_rows"] += 1
                elif start7 <= stamp < now:
                    decisions[signal_key(surface)] += 1
            check()
            cohort["distinct_tokens"] = len(anchors)
            cohort["earliest_anchor_tokens_with_recorded_r7d"] = sum(
                x[2] for x in anchors.values()
            )
            cohort["label_status"] = dict(labels)
            population = []
            for key in sorted(
                samples.keys() | decisions.keys(), key=lambda x: (x is None, x or "")
            ):
                a, b = samples[key], decisions[key]
                state = (
                    "equal_counts"
                    if a == b
                    else (
                        "ledger_only"
                        if not b
                        else (
                            "decision_only"
                            if not a
                            else "ledger_excess" if a > b else "ledger_fewer"
                        )
                    )
                )
                population.append(
                    dict(signal_type=key, ledger_rows=a, decision_rows=b, state=state)
                )
            check()
            return dict(
                meta=dict(
                    ok=True,
                    observed_at=now.isoformat(),
                    window_start=start7.isoformat(),
                    lookback_start=start30.isoformat(),
                    retained_rows_only=True,
                    timestamp_diagnostics_scope="selected_parse_exclusions_only",
                    table_wide_timestamp_counts=None,
                    coverage="unknown",
                    provenance="unverified",
                    conclusions="insufficient_evidence_for_cost_or_ranking",
                ),
                state="recorded_counts_only" if cohort["rows"] else "insufficient_data",
                population=population,
                cohort=cohort,
                diagnostics=diagnostics,
            )
    except (ReadLimit, MemoryError):
        return unavailable("read_limit")
    except sqlite3.Error as exc:
        if time.monotonic() >= deadline or isinstance(exc, sqlite3.DataError):
            return unavailable("read_limit")
        return unavailable("read_unavailable")


class HealthReader:
    """App-local single worker; client cancellation never relinquishes ownership."""

    def __init__(self, path):
        self.path = Path(path).resolve()
        self.active = None

    async def _run(self):
        try:
            return await asyncio.to_thread(read_health, self.path)
        finally:
            self.active = None

    async def get(self):
        if self.active is not None:
            return unavailable("busy")
        self.active = asyncio.create_task(self._run())
        # Retrieve exceptions even if the only HTTP observer has disconnected.
        self.active.add_done_callback(
            lambda task: None if task.cancelled() else task.exception()
        )
        return await asyncio.shield(self.active)

    async def drain(self):
        if self.active is not None:
            await asyncio.shield(self.active)
