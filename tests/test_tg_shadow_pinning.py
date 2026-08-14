"""Stage B pinning tests — the two invariants the as-of reconstruction rests on.

Stage B reconstructs a caller's track record from raw rows instead of reading
the source-quality ledger's derived columns, because `_upsert_source_call()`
rewrites essentially every payload column and a value read from it today is not
the value that existed at the decision time being reproduced. That substitution
is only sound while two properties hold:

  1. `tg_social_messages` and `tg_social_signals` are INSERT-only, so a row's
     facts are the facts as of its own timestamps — forever.
  2. `source_call_price_snapshots` is forward-only, so `snapshot_at` orders
     monotonically and never claims a historical time. If a backfill path ever
     stamped one, the bound would admit observations that did not exist at the
     decision.

     NOTE: forward-only is necessary but NOT sufficient on its own. Review
     falsified the earlier claim that it was: monotonic stamping says nothing
     about when a row becomes VISIBLE, and the writer commits a whole cycle at
     once. The provider therefore also requires `created_at <= as_of`. This
     test pins the forward-only half; the commit-clock half is pinned in
     `test_tg_caller_features.py`.

These tests are regression alarms, not merely documentation. If either
invariant is deliberately broken, Stage B's evidence claims have to be
re-reviewed before the next gate_version activates.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from scout.db import Database
from scout.social.telegram.caller_features import TgCallerFeatureProvider
from scout.social.telegram.shadow import (
    register_caller_feature_provider,
    activate_shadow_generation,
    scan_and_evaluate,
)
from scout.social.telegram.snapshot import build_resolution_snapshot
from scout.social.telegram.models import ResolvedToken
from scout.source_quality.snapshot_writer import write_price_snapshots

NOW = datetime(2026, 8, 12, 12, 0, tzinfo=timezone.utc)

INSERT_ONLY_TABLES = ("tg_social_messages", "tg_social_signals")

_REPO_ROOT = Path(__file__).resolve().parents[1]
# Both trees are scanned: `scripts/` holds one-off backfills and audits, which
# are exactly where a "just fix these rows" UPDATE gets written, and such a
# script breaks Stage B's as-of reconstruction just as thoroughly as a change
# inside `scout/` would.
_SCANNED_ROOTS = (_REPO_ROOT / "scout", _REPO_ROOT / "scripts")

# SQL verbs assembled from fragments. The scan reads `scout/` and `scripts/`,
# never `tests/`, so a literal here could not match itself — but a checking
# pattern that appears verbatim in the checker is the classic way these guards
# start passing for the wrong reason, and the habit is cheaper than the
# incident.
_UPDATE = "UP" + "DATE"
_DELETE = "DE" + "LETE"
_REPLACE = "REP" + "LACE"

_TABLES_ALT = "|".join(INSERT_ONLY_TABLES)

_MUTATION_PATTERNS = {
    "direct_update": rf"\b{_UPDATE}\s+(?:OR\s+\w+\s+)?(?:{_TABLES_ALT})\b",
    "delete": rf"\b{_DELETE}\s+FROM\s+(?:{_TABLES_ALT})\b",
    "replace": rf"\b(?:INSERT\s+OR\s+{_REPLACE}|{_REPLACE})\s+INTO\s+(?:{_TABLES_ALT})\b",
    "upsert_do_update": (
        rf"\bINSERT\s+INTO\s+(?:{_TABLES_ALT})\b[^;]{{0,600}}?DO\s+{_UPDATE}"
    ),
}


def _normalized_sources() -> dict[Path, str]:
    """Every `scout/` and `scripts/` module, whitespace-normalized so a
    statement split across source lines reads as one string to the patterns
    above."""
    sources: dict[Path, str] = {}
    for root in _SCANNED_ROOTS:
        if not root.exists():
            continue
        for path in sorted(root.rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            sources[path] = re.sub(r"\s+", " ", path.read_text(encoding="utf-8"))
    return sources


@pytest.mark.parametrize("pattern_name", sorted(_MUTATION_PATTERNS))
def test_no_source_text_mutates_the_insert_only_tg_tables(pattern_name):
    """Static half of pinning obligation (i)."""
    pattern = re.compile(_MUTATION_PATTERNS[pattern_name], re.IGNORECASE)
    offenders = [
        str(path.relative_to(_REPO_ROOT))
        for path, text in _normalized_sources().items()
        if pattern.search(text)
    ]
    assert offenders == [], (
        f"{pattern_name} path found against an INSERT-only TG table: {offenders}. "
        "Stage B's as-of reconstruction assumes these rows never change; "
        "re-review its evidence claims before shipping this."
    )


@pytest.mark.parametrize("root_index", [0, 1])
def test_every_scanned_root_is_actually_scanned(root_index):
    """N1 — pin the scan ROOTS, not just the patterns.

    `test_the_static_scan_can_actually_see_a_violation` proves the regexes
    match; it does not prove either directory is walked. Dropping `scripts/`
    from the root list would leave that test green while the whole one-off
    backfill/audit tree — exactly where a "just fix these rows" statement gets
    written — went unscanned. So plant a real violation inside EACH root and
    require the scan to find it.
    """
    root = _SCANNED_ROOTS[root_index]
    planted = root / "_pinning_probe_delete_me.py"
    banned = f"SQL = \"{_UPDATE} tg_social_signals SET symbol = 'X'\"\n"
    planted.write_text(banned, encoding="utf-8")
    try:
        pattern = re.compile(_MUTATION_PATTERNS["direct_update"], re.IGNORECASE)
        offenders = [
            path for path, text in _normalized_sources().items() if pattern.search(text)
        ]
        assert planted in offenders, (
            f"{root.name}/ is not being scanned — a mutation planted there was "
            "invisible to the guard"
        )
    finally:
        planted.unlink()


def test_the_static_scan_can_actually_see_a_violation(tmp_path, monkeypatch):
    """A guard that cannot fail is not a guard. Plant each banned shape in a
    file inside the scanned tree and assert the patterns catch it."""
    planted = {
        "direct_update": f"{_UPDATE} tg_social_signals SET symbol = 'X' WHERE id = 1",
        "delete": f"{_DELETE} FROM tg_social_messages WHERE id = 1",
        "replace": f"{_REPLACE} INTO tg_social_signals (id) VALUES (1)",
        "upsert_do_update": (
            "INSERT INTO tg_social_messages (id) VALUES (1) "
            f"ON CONFLICT(id) DO {_UPDATE} SET id = 2"
        ),
    }
    for name, sql in planted.items():
        pattern = re.compile(_MUTATION_PATTERNS[name], re.IGNORECASE)
        assert pattern.search(re.sub(r"\s+", " ", sql)), name


# ---------------------------------------------------------------------------
# Runtime half of obligation (i)
# ---------------------------------------------------------------------------


def _snapshot_json() -> str:
    return build_resolution_snapshot(
        ResolvedToken(
            token_id="dex:solana:abc",
            symbol="TOK",
            chain="solana",
            contract_address="abc",
            mcap=250_000.0,
            price_usd=1.0,
            volume_24h_usd=1000.0,
            age_days=5.0,
            safety_pass=True,
            safety_check_completed=True,
            safety_skipped_no_ca=False,
        )
    )


@pytest.fixture
async def db(tmp_path):
    database = Database(tmp_path / "pinning.db")
    await database.initialize()
    yield database
    await database.close()


async def test_the_shadow_decision_path_never_mutates_the_tg_tables(
    db, settings_factory
):
    """Runtime half of obligation (i).

    Guards that raise on any UPDATE or DELETE of the two tables are installed,
    then the whole armed decision path runs — provider registration, generation
    arming, and a catch-up scan that reads history and writes a decision. The
    scan must complete AND write, because a path that silently did nothing
    would satisfy a no-mutation assertion for the wrong reason.
    """
    for table in INSERT_ONLY_TABLES:
        for verb in ("UPDATE", "DELETE"):
            await db._conn.execute(
                f"CREATE TRIGGER guard_{verb.lower()}_{table} "
                f"BEFORE {verb} ON {table} BEGIN "
                f"SELECT RAISE(ABORT, '{table} is INSERT-only'); END"
            )
    await db._conn.commit()

    settings = settings_factory(
        TG_SHADOW_ENABLED=True, TG_CALLER_MIN_ELIGIBLE_CLUSTERS=0
    )
    register_caller_feature_provider(TgCallerFeatureProvider(db._db_path))
    gate_version = await activate_shadow_generation(db, settings)
    assert gate_version is not None

    created_at = datetime.now(timezone.utc)
    await db._conn.execute(
        "INSERT INTO tg_social_messages (id, channel_handle, msg_id, posted_at, "
        "parsed_at) VALUES (1, '@gem', 1, ?, ?)",
        (created_at.isoformat(), created_at.isoformat()),
    )
    await db._conn.execute(
        """INSERT INTO tg_social_signals
           (id, message_pk, token_id, symbol, contract_address, chain,
            mcap_at_sighting, resolution_state, source_channel_handle,
            created_at, resolution_snapshot_json)
           VALUES (1, 1, 'dex:solana:abc', 'TOK', 'abc', 'solana', 250000.0,
                   'RESOLVED', '@gem', ?, ?)""",
        (created_at.isoformat(), _snapshot_json()),
    )
    await db._conn.commit()

    result = await scan_and_evaluate(db, settings)

    assert result["armed"] is True
    assert result["scanned"] == 1
    assert result["written"] == 1
    cur = await db._conn.execute("SELECT COUNT(*) FROM tg_act_shadow")
    assert (await cur.fetchone())[0] == 1


async def test_the_runtime_guard_can_actually_fire(db):
    """The mutation control for the trigger guard above."""
    import aiosqlite

    now_iso = datetime.now(timezone.utc).isoformat()
    await db._conn.execute(
        "INSERT INTO tg_social_messages (id, channel_handle, msg_id, posted_at, "
        "parsed_at) VALUES (1, '@gem', 1, ?, ?)",
        (now_iso, now_iso),
    )
    await db._conn.execute(
        "CREATE TRIGGER guard_update_tg_social_messages "
        "BEFORE UPDATE ON tg_social_messages BEGIN "
        "SELECT RAISE(ABORT, 'tg_social_messages is INSERT-only'); END"
    )
    await db._conn.commit()

    with pytest.raises(aiosqlite.IntegrityError, match="INSERT-only"):
        await db._conn.execute(
            "UPDATE tg_social_messages SET sender = 'x' WHERE id = 1"
        )


# ---------------------------------------------------------------------------
# Obligation (ii) — forward-only snapshot_at
# ---------------------------------------------------------------------------


def _pool():
    return SimpleNamespace(
        network="solana",
        pool_address="POOLX",
        base_token_address=None,
        reserve_usd=15_000.0,
        source="gt",
    )


def _candle(close: float):
    return SimpleNamespace(
        timestamp=1_700_000_000,
        open=1.0,
        high=2.0,
        low=0.5,
        close=close,
        volume_usd=100.0,
        source="gt",
    )


async def _resolve_pool(*, chain, contract_address):
    return _pool()


async def _fetch_ohlcv(*, network, pool_address):
    return [_candle(1.5)]


async def _seed_source_call(db: Database, *, call_ts: datetime, contract: str) -> None:
    await db._conn.execute(
        """INSERT INTO source_calls
           (source_type, source_id, source_event_id, token_id, symbol,
            contract_address, chain, call_ts, call_kind, cluster_identity,
            cluster_identity_kind, duplicate_cluster_key, resolved_state,
            outcome_status, missing_fields, created_at, updated_at)
           VALUES ('tg', '@gem', ?, ?, 'TOK', ?, 'solana', ?, 'ca_call', ?,
                   'contract', ?, 'eligible_contract', 'pending', '[]', ?, ?)""",
        (
            f"evt-{contract}",
            f"dex:solana:{contract}",
            contract,
            call_ts.isoformat(),
            f"solana|{contract}",
            f"cluster-{contract}",
            call_ts.isoformat(),
            call_ts.isoformat(),
        ),
    )
    await db._conn.commit()


async def test_snapshot_writer_stamps_forward_only_snapshot_at(db):
    """Obligation (ii): every inserted `snapshot_at` is >= the max already
    present. `snapshot_at <= as_of` is Stage B's entire price-knowability bound;
    a writer that could stamp an EARLIER time than rows already stored would
    make a past decision see an observation it could not have seen, and replay
    would stop reproducing it.
    """
    await _seed_source_call(db, call_ts=NOW - timedelta(hours=1), contract="aaa")
    await write_price_snapshots(
        db._conn, now=NOW, resolve_pool=_resolve_pool, fetch_ohlcv=_fetch_ohlcv
    )

    cur = await db._conn.execute(
        "SELECT MAX(snapshot_at) FROM source_call_price_snapshots"
    )
    first_max = (await cur.fetchone())[0]
    assert (
        first_max is not None
    ), "writer wrote nothing — the assertion below is vacuous"

    # A LATER cycle. Its rows must not predate what is already recorded.
    await _seed_source_call(db, call_ts=NOW, contract="bbb")
    await write_price_snapshots(
        db._conn,
        now=NOW + timedelta(minutes=15),
        resolve_pool=_resolve_pool,
        fetch_ohlcv=_fetch_ohlcv,
    )

    cur = await db._conn.execute(
        "SELECT snapshot_at FROM source_call_price_snapshots ORDER BY id"
    )
    stamps = [row[0] for row in await cur.fetchall()]
    assert len(stamps) >= 2

    running_max = stamps[0]
    for stamp in stamps[1:]:
        assert stamp >= running_max, (
            f"snapshot_at went backwards: {stamp} after {running_max}. "
            "Stage B's price-knowability bound assumes snapshot_at is the "
            "RECORDING time, never a claimed historical one."
        )
        running_max = stamp


async def test_snapshot_writer_stamps_the_cycle_clock_not_the_call_time(db):
    """The sharper form of the same invariant: a call made an hour ago is
    snapshotted at the CYCLE's time. A writer that stamped `call_ts` would be
    backfilling, and every Stage B knowability bound would silently admit it."""
    call_ts = NOW - timedelta(hours=3)
    await _seed_source_call(db, call_ts=call_ts, contract="aaa")

    await write_price_snapshots(
        db._conn, now=NOW, resolve_pool=_resolve_pool, fetch_ohlcv=_fetch_ohlcv
    )

    cur = await db._conn.execute("SELECT snapshot_at FROM source_call_price_snapshots")
    rows = await cur.fetchall()
    assert rows, "writer wrote nothing — the assertion below is vacuous"
    for row in rows:
        assert row[0] == NOW.isoformat()
        assert row[0] != call_ts.isoformat()
