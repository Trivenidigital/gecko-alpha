"""PR2 production wiring — deploy-dark, and armed exactly once.

The merge claim this file has to substantiate is narrow and load-bearing: with
`TG_SHADOW_ENABLED` absent or false, shipping Stage B to production writes
NOTHING. Not a generation row, not a health row, not a `tg_act_shadow` row, and
not a registered provider. Activation is a later, separate operator action, and
`activated_at` must be the activation boundary rather than the deploy time — a
row created at deploy would retroactively pull every call made between deploy
and activation into the generation.

The armed half matters just as much: a wiring path that is provably inert is
worthless if it is ALSO inert when switched on.
"""

from __future__ import annotations

import asyncio
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from scout.db import Database
from scout.social.telegram.caller_features import TgCallerFeatureProvider
from scout.social.telegram.models import ResolvedToken
from scout.social.telegram.shadow import (
    SHADOW_ACTIVE_GENERATION_COMPONENT,
    get_registered_provider,
)
from scout.social.telegram.shadow_runner import scan_interval_sec, tg_shadow_loop
from scout.social.telegram.snapshot import build_resolution_snapshot

# Every table and health component the shadow program can write.
_SHADOW_TABLES = ("tg_act_shadow", "tg_act_shadow_generations")


@pytest.fixture
async def db(tmp_path):
    database = Database(tmp_path / "wiring.db")
    await database.initialize()
    yield database
    await database.close()


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


async def _seed_resolved_signal(
    db: Database, *, after_activation: bool = False
) -> None:
    """One RESOLVED signal — eligible work, so 'nothing was written' cannot be
    explained by 'there was nothing to write'.

    `after_activation` stamps `created_at` past the moment the loop will arm.
    The generation cutover puts pre-activation rows outside every generation by
    construction — `activated_at` is the activation boundary, never deploy time
    — so a call that a scan is expected to DECIDE has to post-date it. That is
    the modeled population: calls arriving after the operator flips the flag.
    """
    stamp = datetime.now(timezone.utc)
    if after_activation:
        stamp += timedelta(minutes=1)
    now_iso = stamp.isoformat()
    await db._conn.execute(
        "INSERT INTO tg_social_messages (id, channel_handle, msg_id, posted_at, "
        "parsed_at) VALUES (1, '@gem', 1, ?, ?)",
        (now_iso, now_iso),
    )
    await db._conn.execute(
        """INSERT INTO tg_social_signals
           (id, message_pk, token_id, symbol, contract_address, chain,
            mcap_at_sighting, resolution_state, source_channel_handle,
            created_at, resolution_snapshot_json)
           VALUES (1, 1, 'dex:solana:abc', 'TOK', 'abc', 'solana', 250000.0,
                   'RESOLVED', '@gem', ?, ?)""",
        (now_iso, _snapshot_json()),
    )
    await db._conn.commit()


async def _shadow_row_counts(db: Database) -> dict[str, int]:
    counts: dict[str, int] = {}
    for table in _SHADOW_TABLES:
        cur = await db._conn.execute(f"SELECT COUNT(*) FROM {table}")
        counts[table] = (await cur.fetchone())[0]
    cur = await db._conn.execute("SELECT COUNT(*) FROM tg_social_health")
    counts["tg_social_health"] = (await cur.fetchone())[0]
    return counts


# ---------------------------------------------------------------------------
# Dark
# ---------------------------------------------------------------------------


async def test_flag_off_writes_nothing_at_all(db, settings_factory):
    """The deploy-dark proof. Eligible work exists and is deliberately ignored."""
    await _seed_resolved_signal(db)
    before = await _shadow_row_counts(db)

    await tg_shadow_loop(db=db, settings=settings_factory(TG_SHADOW_ENABLED=False))

    assert await _shadow_row_counts(db) == before
    assert before == {
        "tg_act_shadow": 0,
        "tg_act_shadow_generations": 0,
        "tg_social_health": 0,
    }


async def test_flag_off_registers_no_provider(db, settings_factory):
    """A provider registered while dark would let the listener's live hook arm
    the moment someone flipped the flag without a restart — activation would
    then happen with no generation row and no scan behind it."""
    await tg_shadow_loop(db=db, settings=settings_factory(TG_SHADOW_ENABLED=False))

    assert get_registered_provider() is None


async def test_flag_off_returns_rather_than_looping(db, settings_factory):
    """It must RETURN, not spin: the dark path is the deployed path, and a task
    that never completes is a task that has to be cancelled at shutdown."""
    await asyncio.wait_for(
        tg_shadow_loop(db=db, settings=settings_factory(TG_SHADOW_ENABLED=False)),
        timeout=5,
    )


def test_main_spawns_the_loop_only_behind_the_flag():
    """Static half of the darkness proof: the task is not created at all when
    the flag is off, so nothing depends on the loop's own early return."""
    main_src = re.sub(
        r"\s+",
        " ",
        (Path(__file__).resolve().parents[1] / "scout" / "main.py").read_text(
            encoding="utf-8"
        ),
    )
    assert "tg_shadow_loop" in main_src, "wiring missing from main.py"
    # Every mention of the loop is preceded, within the same block, by the gate.
    for match in re.finditer(r"tg_shadow_loop", main_src):
        window = main_src[max(0, match.start() - 400) : match.start()]
        assert (
            "if settings.TG_SHADOW_ENABLED:" in window
        ), "tg_shadow_loop referenced outside a TG_SHADOW_ENABLED guard"


# ---------------------------------------------------------------------------
# Armed
# ---------------------------------------------------------------------------


async def _run_until_decision(db: Database, settings, *, timeout: float = 10.0) -> None:
    """Start the real loop and wait for its first scan to land a decision."""
    task = asyncio.create_task(tg_shadow_loop(db=db, settings=settings))
    try:
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            cur = await db._conn.execute("SELECT COUNT(*) FROM tg_act_shadow")
            if (await cur.fetchone())[0] >= 1:
                return
            if task.done():
                await task  # surface whatever stopped it
                raise AssertionError("loop exited before writing a decision")
            await asyncio.sleep(0.01)
        raise AssertionError("timed out waiting for the first shadow decision")
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


async def test_flag_on_arms_exactly_once_and_writes_a_decision(db, settings_factory):
    """The armed counterpart. Without this, 'writes nothing when off' would be
    satisfied by wiring that writes nothing when on either."""
    await _seed_resolved_signal(db, after_activation=True)
    settings = settings_factory(
        TG_SHADOW_ENABLED=True, TG_CALLER_MIN_ELIGIBLE_CLUSTERS=0
    )

    await _run_until_decision(db, settings)

    cur = await db._conn.execute("SELECT COUNT(*) FROM tg_act_shadow_generations")
    assert (await cur.fetchone())[0] == 1
    cur = await db._conn.execute("SELECT COUNT(*) FROM tg_act_shadow")
    assert (await cur.fetchone())[0] == 1
    # The active-generation marker is what a read-only observer reads.
    cur = await db._conn.execute(
        "SELECT COUNT(*) FROM tg_social_health WHERE component = ?",
        (SHADOW_ACTIVE_GENERATION_COMPONENT,),
    )
    assert (await cur.fetchone())[0] == 1


async def test_flag_on_registers_the_real_provider(db, settings_factory):
    """The fixture provider must never be what production arms against."""
    await _seed_resolved_signal(db, after_activation=True)
    settings = settings_factory(
        TG_SHADOW_ENABLED=True, TG_CALLER_MIN_ELIGIBLE_CLUSTERS=0
    )

    await _run_until_decision(db, settings)

    provider = get_registered_provider()
    assert isinstance(provider, TgCallerFeatureProvider)
    assert provider.caller_feature_semantic_version == "tg-caller-v1"


async def test_restarting_the_loop_resumes_the_same_generation(db, settings_factory):
    """Disable/enable must RESUME, not fork: the registry row already exists and
    is not rewritten, and the decision already written is not duplicated."""
    await _seed_resolved_signal(db, after_activation=True)
    settings = settings_factory(
        TG_SHADOW_ENABLED=True, TG_CALLER_MIN_ELIGIBLE_CLUSTERS=0
    )
    await _run_until_decision(db, settings)
    cur = await db._conn.execute(
        "SELECT gate_version, activated_at FROM tg_act_shadow_generations"
    )
    first = await cur.fetchall()

    await _run_until_decision(db, settings)

    cur = await db._conn.execute(
        "SELECT gate_version, activated_at FROM tg_act_shadow_generations"
    )
    assert [tuple(r) for r in await cur.fetchall()] == [tuple(r) for r in first]
    cur = await db._conn.execute("SELECT COUNT(*) FROM tg_act_shadow")
    assert (await cur.fetchone())[0] == 1


# ---------------------------------------------------------------------------
# Scan cadence
# ---------------------------------------------------------------------------


def test_scan_interval_stays_inside_the_watchdog_cadence(settings_factory):
    """The watchdog pages when no scan completed within the cadence budget. A
    scanner running exactly AT the cadence races that alarm every cycle."""
    settings = settings_factory(TG_SHADOW_SCAN_CADENCE_MIN=30)

    assert scan_interval_sec(settings) == 15 * 60
    assert scan_interval_sec(settings) < settings.TG_SHADOW_SCAN_CADENCE_MIN * 60


def test_scan_interval_has_a_floor(settings_factory):
    """A tiny cadence must not turn the loop into a spin — AND the floor must
    not break the strictly-inside-the-budget relation.

    A bare `max(60, ...)` returned exactly 60 at a 1-minute cadence: equal to
    the budget, not inside it, so the scanner raced the dead-writer alarm on
    every cycle at precisely the setting where the alarm is tightest.
    """
    settings = settings_factory(TG_SHADOW_SCAN_CADENCE_MIN=1)
    interval = scan_interval_sec(settings)

    assert interval > 0
    assert interval < settings.TG_SHADOW_SCAN_CADENCE_MIN * 60


@pytest.mark.parametrize("cadence", [1, 2, 5, 30, 120])
def test_scan_interval_is_always_strictly_inside_the_budget(cadence, settings_factory):
    """The relation is the contract; the halving is just how it is met."""
    settings = settings_factory(TG_SHADOW_SCAN_CADENCE_MIN=cadence)
    interval = scan_interval_sec(settings)

    assert 0 < interval < cadence * 60, cadence
