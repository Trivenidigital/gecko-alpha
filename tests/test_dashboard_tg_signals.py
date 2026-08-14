"""TG signal intelligence surface — API contract tests.

Covers the signal-centric endpoint added for the TG tab redesign plus the two
additive blocks on the existing message-centric endpoint (`lane_status`,
`funnel_24h`), and pins the literals this layer duplicates from the writers.

Fixtures build rows the way the LISTENER writes them — including the
placeholder-token unresolved shape and the cashtag path's NULL contract — so a
classification bug has something to be wrong about. A fixture built only from
happy-path resolved rows could not see either.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import aiosqlite
import pytest

from dashboard import db as dash_db
from scout.db import Database

ROOT = Path(__file__).resolve().parents[1]

_SKIP_AIOHTTP = pytest.mark.skipif(
    sys.platform == "win32" and os.environ.get("SKIP_AIOHTTP_TESTS") == "1",
    reason="Windows + SKIP_AIOHTTP_TESTS=1: skip aiohttp tests",
)


@pytest.fixture(autouse=True)
def _reset_dashboard_module_state():
    """Same reset as tests/test_dashboard_tg_social_extensions.py — a cached
    `_scout_db` from a previous test points at a different tmp_path."""
    import dashboard.api as dash_api

    dash_api._scout_db = None
    dash_api._db_path = "scout.db"
    yield
    dash_api._scout_db = None
    dash_api._db_path = "scout.db"


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _snapshot(**overrides) -> str:
    payload = {
        "snapshot_schema_version": 1,
        "price_usd": 0.0036,
        "volume_24h_usd": 5_993_778.0,
        "age_days": None,
        "liquidity_usd": None,
        "safety_pass": True,
        "safety_check_completed": True,
        "safety_skipped_no_ca": False,
    }
    payload.update(overrides)
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


async def _seed_db(db_path: str) -> None:
    sd = Database(db_path)
    await sd.initialize()
    await sd.close()


async def _insert_message(
    db_path: str,
    *,
    pk: int,
    channel: str,
    msg_id: int,
    posted_at: str,
    text: str = "",
    cashtags: str = "[]",
    contracts: str = "[]",
    urls: str = "[]",
) -> None:
    async with aiosqlite.connect(db_path) as conn:
        await conn.execute(
            "INSERT INTO tg_social_messages "
            "(id, channel_handle, msg_id, posted_at, sender, text, cashtags, "
            " contracts, urls, parsed_at) "
            "VALUES (?, ?, ?, ?, 'sender', ?, ?, ?, ?, ?)",
            (
                pk,
                channel,
                msg_id,
                posted_at,
                text,
                cashtags,
                contracts,
                urls,
                posted_at,
            ),
        )
        await conn.commit()


async def _insert_signal(
    db_path: str,
    *,
    message_pk: int,
    token_id: str,
    symbol: str,
    contract_address: str | None,
    chain: str | None,
    mcap: float | None,
    state: str,
    channel: str,
    created_at: str,
    paper_trade_id: int | None = None,
    snapshot_json: str | None = None,
) -> int:
    async with aiosqlite.connect(db_path) as conn:
        cur = await conn.execute(
            "INSERT INTO tg_social_signals "
            "(message_pk, token_id, symbol, contract_address, chain, "
            " mcap_at_sighting, resolution_state, source_channel_handle, "
            " paper_trade_id, created_at, resolution_snapshot_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                message_pk,
                token_id,
                symbol,
                contract_address,
                chain,
                mcap,
                state,
                channel,
                paper_trade_id,
                created_at,
                snapshot_json,
            ),
        )
        await conn.commit()
        return cur.lastrowid


async def _insert_shadow(
    db_path: str,
    *,
    signal_id: int,
    gate_version: str,
    actionable: int,
    reason: str,
    created_at: str,
    features: dict | None = None,
) -> None:
    features_json = json.dumps(
        {
            "config_fingerprint": "f" * 64,
            "gate_version": gate_version,
            "decision_as_of": created_at,
            "features": features or {"history_eligible_distinct_clusters": 3},
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    async with aiosqlite.connect(db_path) as conn:
        await conn.execute(
            "INSERT INTO tg_act_shadow "
            "(signal_id, gate_version, actionable, reason, features_json, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (signal_id, gate_version, actionable, reason, features_json, created_at),
        )
        await conn.commit()


async def _insert_generation(db_path: str, *, gate_version: str, activated_at: str):
    async with aiosqlite.connect(db_path) as conn:
        await conn.execute(
            "INSERT INTO tg_act_shadow_generations (gate_version, activated_at) "
            "VALUES (?, ?)",
            (gate_version, activated_at),
        )
        await conn.commit()


async def _insert_health(
    db_path: str, *, component: str, state: str, detail: str | None, updated_at: str
):
    async with aiosqlite.connect(db_path) as conn:
        await conn.execute(
            "INSERT INTO tg_social_health "
            "(component, listener_state, last_message_at, updated_at, detail) "
            "VALUES (?, ?, ?, ?, ?)",
            (component, state, updated_at, updated_at, detail),
        )
        await conn.commit()


async def _insert_paper_trade(db_path: str, *, opened_at: str, token_id: str) -> int:
    async with aiosqlite.connect(db_path) as conn:
        cur = await conn.execute(
            "INSERT INTO paper_trades "
            "(token_id, symbol, name, chain, signal_type, signal_data, "
            " entry_price, amount_usd, quantity, tp_price, sl_price, opened_at) "
            "VALUES (?, 'SYM', 'Sym', 'solana', 'tg_social', '{}', "
            " 0.001, 300.0, 300000.0, 0.0012, 0.0009, ?)",
            (token_id, opened_at),
        )
        await conn.commit()
        return cur.lastrowid


# ---------------------------------------------------------------------------
# Signal-centric endpoint
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_signals_surface_carries_snapshot_shadow_and_identity(tmp_path):
    """The three things the message-centric endpoint never returned: the
    decision inputs, the counterfactual decision, and which listener path
    produced the identity."""
    db_path = str(tmp_path / "test.db")
    await _seed_db(db_path)
    now = datetime.now(timezone.utc)
    await _insert_message(
        db_path,
        pk=1,
        channel="@thanos_mind",
        msg_id=11,
        posted_at=_iso(now - timedelta(minutes=10)),
        text="$GTA is running",
        cashtags='["GTA"]',
        contracts='["BVEaDToN"]',
    )
    sid = await _insert_signal(
        db_path,
        message_pk=1,
        token_id="dex:solana:BVEaDToN",
        symbol="GTA",
        contract_address="BVEaDToN",
        chain="solana",
        mcap=722_643.0,
        state="RESOLVED",
        channel="@thanos_mind",
        created_at=_iso(now - timedelta(minutes=9)),
        snapshot_json=_snapshot(),
    )
    await _insert_shadow(
        db_path,
        signal_id=sid,
        gate_version="tg-shadow-v1+aaaa",
        actionable=1,
        reason="shadow_pass",
        created_at=_iso(now - timedelta(minutes=8)),
    )

    payload = await dash_db.get_tg_social_signals(db_path, limit=10)
    assert len(payload["signals"]) == 1
    row = payload["signals"][0]

    assert row["signal_id"] == sid
    assert row["symbol"] == "GTA"
    assert row["channel_handle"] == "@thanos_mind"
    assert row["identity_kind"] == "dex_pseudo_id"
    assert row["priceable"] is True
    assert row["snapshot"]["price_usd"] == 0.0036
    assert row["snapshot"]["safety_check_completed"] is True
    assert row["shadow"]["actionable"] is True
    assert row["shadow"]["reason"] == "shadow_pass"
    assert row["shadow"]["gate_version"] == "tg-shadow-v1+aaaa"
    assert row["shadow"]["features"]["history_eligible_distinct_clusters"] == 3
    assert row["trade_link"]["state"] == "none"
    assert row["message"]["text"] == "$GTA is running"
    assert payload["capabilities"]["shadow_table_present"] is True
    assert payload["capabilities"]["snapshot_column_present"] is True


@pytest.mark.asyncio
async def test_unresolved_row_is_unpriceable_with_absent_snapshot(tmp_path):
    """The listener's unresolved path writes a PLACEHOLDER token_id and no
    snapshot. Both have to survive as explicit absence — an unresolved row that
    reported `priceable=True` would inflate every coverage denominator."""
    db_path = str(tmp_path / "test.db")
    await _seed_db(db_path)
    now = datetime.now(timezone.utc)
    await _insert_message(
        db_path,
        pk=1,
        channel="@c",
        msg_id=1,
        posted_at=_iso(now - timedelta(minutes=5)),
    )
    await _insert_signal(
        db_path,
        message_pk=1,
        token_id=dash_db.TG_UNRESOLVED_PLACEHOLDER,
        symbol=dash_db.TG_UNRESOLVED_PLACEHOLDER,
        contract_address=None,
        chain=None,
        mcap=None,
        state="UNRESOLVED_TERMINAL",
        channel="@c",
        created_at=_iso(now - timedelta(minutes=4)),
        snapshot_json=None,
    )

    row = (await dash_db.get_tg_social_signals(db_path, limit=10))["signals"][0]
    assert row["identity_kind"] == "unresolved"
    assert row["priceable"] is False
    assert row["snapshot"] is None
    assert row["shadow"] is None
    # Unresolved is a resolver OUTCOME, not a row awaiting a human.
    assert row["needs_review"] is False


@pytest.mark.asyncio
async def test_cashtag_path_row_is_priceable_but_classified_separately(tmp_path):
    """The cashtag path persists a real CoinGecko id with contract_address
    NULL. It is priceable (a CG coin id can be priced) yet must NOT be
    presented as a contract-confirmed match."""
    db_path = str(tmp_path / "test.db")
    await _seed_db(db_path)
    now = datetime.now(timezone.utc)
    await _insert_message(
        db_path,
        pk=1,
        channel="@c",
        msg_id=1,
        posted_at=_iso(now),
        cashtags='["MOMOTA"]',
    )
    await _insert_signal(
        db_path,
        message_pk=1,
        token_id="momota",
        symbol="MOMOTA",
        contract_address=None,
        chain=None,
        mcap=3_658_554.0,
        state="RESOLVED",
        channel="@c",
        created_at=_iso(now),
        snapshot_json=_snapshot(
            safety_check_completed=False, safety_pass=False, safety_skipped_no_ca=True
        ),
    )

    row = (await dash_db.get_tg_social_signals(db_path, limit=10))["signals"][0]
    assert row["identity_kind"] == "cg_coin_cashtag_only"
    assert row["priceable"] is True
    # Safety never completed -> the operator's triage bucket.
    assert row["needs_review"] is True


@pytest.mark.asyncio
async def test_dangling_trade_link_reports_missing_not_none(tmp_path):
    """A signal whose paper_trade_id points at a row that is gone is an
    integrity error. Reporting it as "no trade" is how such an error stays
    invisible — the whole point of splitting the old UNLINKED badge."""
    db_path = str(tmp_path / "test.db")
    await _seed_db(db_path)
    now = datetime.now(timezone.utc)
    await _insert_message(db_path, pk=1, channel="@c", msg_id=1, posted_at=_iso(now))
    await _insert_signal(
        db_path,
        message_pk=1,
        token_id="some-coin",
        symbol="SOME",
        contract_address="0xabc",
        chain="ethereum",
        mcap=1_000_000.0,
        state="RESOLVED",
        channel="@c",
        created_at=_iso(now),
        paper_trade_id=999_999,  # never inserted
        snapshot_json=_snapshot(),
    )

    row = (await dash_db.get_tg_social_signals(db_path, limit=10))["signals"][0]
    assert row["trade_link"]["state"] == "missing"
    assert row["trade_link"]["paper_trade_id"] == 999_999
    assert row["needs_review"] is True


@pytest.mark.asyncio
async def test_linked_trade_carries_outcome(tmp_path):
    db_path = str(tmp_path / "test.db")
    await _seed_db(db_path)
    now = datetime.now(timezone.utc)
    trade_id = await _insert_paper_trade(
        db_path, opened_at=_iso(now), token_id="linked-coin"
    )
    await _insert_message(db_path, pk=1, channel="@c", msg_id=1, posted_at=_iso(now))
    await _insert_signal(
        db_path,
        message_pk=1,
        token_id="linked-coin",
        symbol="LINK",
        contract_address="0xabc",
        chain="ethereum",
        mcap=1_000_000.0,
        state="RESOLVED",
        channel="@c",
        created_at=_iso(now),
        paper_trade_id=trade_id,
        snapshot_json=_snapshot(),
    )

    row = (await dash_db.get_tg_social_signals(db_path, limit=10))["signals"][0]
    assert row["trade_link"]["state"] == "linked"
    assert row["trade_link"]["paper_trade_id"] == trade_id
    assert row["trade_link"]["status"] == "open"
    assert row["needs_review"] is False


@pytest.mark.asyncio
async def test_latest_generation_decision_is_the_one_returned(tmp_path):
    """A signal legitimately carries one decision per gate_version. The row
    shows the newest AND names its gate_version, so a verdict is never read as
    belonging to a generation that did not produce it."""
    db_path = str(tmp_path / "test.db")
    await _seed_db(db_path)
    now = datetime.now(timezone.utc)
    await _insert_message(db_path, pk=1, channel="@c", msg_id=1, posted_at=_iso(now))
    sid = await _insert_signal(
        db_path,
        message_pk=1,
        token_id="some-coin",
        symbol="SOME",
        contract_address="0xabc",
        chain="ethereum",
        mcap=1_000_000.0,
        state="RESOLVED",
        channel="@c",
        created_at=_iso(now - timedelta(hours=3)),
        snapshot_json=_snapshot(),
    )
    await _insert_shadow(
        db_path,
        signal_id=sid,
        gate_version="tg-shadow-v1+old",
        actionable=0,
        reason="shadow_block_caller_insufficient_n",
        created_at=_iso(now - timedelta(hours=2)),
    )
    await _insert_shadow(
        db_path,
        signal_id=sid,
        gate_version="tg-shadow-v1+new",
        actionable=1,
        reason="shadow_pass",
        created_at=_iso(now - timedelta(hours=1)),
    )

    payload = await dash_db.get_tg_social_signals(db_path, limit=10)
    row = payload["signals"][0]
    assert row["shadow"]["gate_version"] == "tg-shadow-v1+new"
    assert row["shadow"]["reason"] == "shadow_pass"
    assert payload["counts"]["shadow_pass"] == 1


@pytest.mark.asyncio
async def test_counts_are_computed_over_the_returned_window(tmp_path):
    """Filter chips read these counts. If they were computed over a different
    population than the rows returned, a chip would promise rows the table
    cannot show."""
    db_path = str(tmp_path / "test.db")
    await _seed_db(db_path)
    now = datetime.now(timezone.utc)
    for i in range(5):
        await _insert_message(
            db_path,
            pk=i + 1,
            channel="@c",
            msg_id=i + 1,
            posted_at=_iso(now - timedelta(minutes=i)),
        )
        resolved = i % 2 == 0
        await _insert_signal(
            db_path,
            message_pk=i + 1,
            token_id=f"coin-{i}" if resolved else dash_db.TG_UNRESOLVED_PLACEHOLDER,
            symbol=f"C{i}" if resolved else dash_db.TG_UNRESOLVED_PLACEHOLDER,
            contract_address=None,
            chain=None,
            mcap=1_000_000.0 if resolved else None,
            state="RESOLVED" if resolved else "UNRESOLVED_TRANSIENT",
            channel="@c",
            created_at=_iso(now - timedelta(minutes=i)),
            snapshot_json=_snapshot() if resolved else None,
        )

    payload = await dash_db.get_tg_social_signals(db_path, limit=3)
    assert len(payload["signals"]) == 3
    assert payload["counts"]["total"] == 3
    assert (
        payload["counts"]["resolved"] + payload["counts"]["unresolved"]
        == payload["counts"]["total"]
    )


@pytest.mark.asyncio
async def test_missing_shadow_table_degrades_instead_of_failing(tmp_path):
    """Builds the OLD shape: a database whose tg_act_shadow migration has not
    run. `capabilities` must say so, so the frontend can distinguish "nothing
    passed" from "this DB cannot answer"."""
    db_path = str(tmp_path / "pre_shadow.db")
    await _seed_db(db_path)
    async with aiosqlite.connect(db_path) as conn:
        await conn.execute("DROP TABLE tg_act_shadow")
        await conn.execute("DROP TABLE tg_act_shadow_generations")
        await conn.commit()
    now = datetime.now(timezone.utc)
    await _insert_message(db_path, pk=1, channel="@c", msg_id=1, posted_at=_iso(now))
    await _insert_signal(
        db_path,
        message_pk=1,
        token_id="some-coin",
        symbol="SOME",
        contract_address="0xabc",
        chain="ethereum",
        mcap=1_000_000.0,
        state="RESOLVED",
        channel="@c",
        created_at=_iso(now),
        snapshot_json=_snapshot(),
    )

    payload = await dash_db.get_tg_social_signals(db_path, limit=10)
    assert payload["capabilities"]["shadow_table_present"] is False
    assert len(payload["signals"]) == 1
    assert payload["signals"][0]["shadow"] is None

    state = await dash_db.get_tg_social_shadow_state(db_path)
    assert state["shadow_table_present"] is False
    assert state["generations"] == []
    assert state["decisions_total"] == 0


# ---------------------------------------------------------------------------
# Shadow generation state + funnel
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_active_generation_comes_from_writer_marker_not_newest_registry_row(
    tmp_path,
):
    """The load-bearing case. Re-enable resumes an EXISTING gate_version
    without rewriting its registry row, so after v1 -> v2 -> (operator returns
    to the v1 config) the newest `activated_at` is v2 while the writer runs v1.
    Reading the registry would name the wrong generation and report the one
    actually producing evidence as unwatched."""
    db_path = str(tmp_path / "test.db")
    await _seed_db(db_path)
    now = datetime.now(timezone.utc)
    await _insert_generation(
        db_path,
        gate_version="tg-shadow-v1+one",
        activated_at=_iso(now - timedelta(days=3)),
    )
    await _insert_generation(
        db_path,
        gate_version="tg-shadow-v1+two",
        activated_at=_iso(now - timedelta(days=1)),
    )
    await _insert_health(
        db_path,
        component=dash_db.TG_SHADOW_ACTIVE_GENERATION_COMPONENT,
        state="running",
        detail=f"{dash_db.TG_SHADOW_ACTIVE_GENERATION_DETAIL_PREFIX}tg-shadow-v1+one",
        updated_at=_iso(now),
    )

    state = await dash_db.get_tg_social_shadow_state(db_path)
    assert state["active_gate_version"] == "tg-shadow-v1+one"
    assert state["active_generation_activated_at"] == _iso(now - timedelta(days=3))
    assert len(state["generations"]) == 2


@pytest.mark.asyncio
async def test_no_marker_reports_not_armed_rather_than_guessing(tmp_path):
    db_path = str(tmp_path / "test.db")
    await _seed_db(db_path)
    now = datetime.now(timezone.utc)
    await _insert_generation(
        db_path, gate_version="tg-shadow-v1+one", activated_at=_iso(now)
    )

    state = await dash_db.get_tg_social_shadow_state(db_path)
    assert state["active_gate_version"] is None
    assert state["active_generation_activated_at"] is None


@pytest.mark.asyncio
async def test_funnel_counts_each_stage_from_persisted_rows(tmp_path):
    db_path = str(tmp_path / "test.db")
    await _seed_db(db_path)
    now = datetime.now(timezone.utc)
    # In window: one parseable message + one bare message.
    await _insert_message(
        db_path,
        pk=1,
        channel="@c",
        msg_id=1,
        posted_at=_iso(now - timedelta(hours=1)),
        cashtags='["GTA"]',
    )
    await _insert_message(
        db_path, pk=2, channel="@c", msg_id=2, posted_at=_iso(now - timedelta(hours=2))
    )
    # Out of window — must not be counted.
    await _insert_message(
        db_path,
        pk=3,
        channel="@c",
        msg_id=3,
        posted_at=_iso(now - timedelta(hours=48)),
        cashtags='["OLD"]',
    )
    trade_id = await _insert_paper_trade(
        db_path, opened_at=_iso(now), token_id="traded-coin"
    )
    await _insert_signal(
        db_path,
        message_pk=1,
        token_id="traded-coin",
        symbol="T",
        contract_address="0xabc",
        chain="ethereum",
        mcap=1_000_000.0,
        state="RESOLVED",
        channel="@c",
        created_at=_iso(now - timedelta(hours=1)),
        paper_trade_id=trade_id,
        snapshot_json=_snapshot(),
    )
    await _insert_signal(
        db_path,
        message_pk=2,
        token_id=dash_db.TG_UNRESOLVED_PLACEHOLDER,
        symbol=dash_db.TG_UNRESOLVED_PLACEHOLDER,
        contract_address=None,
        chain=None,
        mcap=None,
        state="UNRESOLVED_TERMINAL",
        channel="@c",
        created_at=_iso(now - timedelta(hours=2)),
    )
    await _insert_signal(
        db_path,
        message_pk=3,
        token_id="old-coin",
        symbol="OLD",
        contract_address="0xold",
        chain="ethereum",
        mcap=1_000.0,
        state="RESOLVED",
        channel="@c",
        created_at=_iso(now - timedelta(hours=48)),
    )

    funnel = await dash_db.get_tg_social_funnel_24h(db_path)
    stages = funnel["stages"]
    assert funnel["window_hours"] == 24
    assert stages["messages"] == 2
    assert stages["parsed"] == 1
    assert stages["signals"] == 2
    assert stages["resolved"] == 1
    assert stages["priceable"] == 1
    assert stages["paper_traded"] == 1
    # Nothing armed: the last two stages are zero BY DESIGN, and the flag is
    # what keeps that distinguishable from "evaluated and found nothing".
    assert funnel["shadow_armed"] is False
    assert stages["shadow_eligible"] == 0
    assert stages["shadow_pass"] == 0


@pytest.mark.asyncio
async def test_shadow_eligible_respects_the_activation_boundary(tmp_path):
    """Rows created before the active generation's activation are outside it by
    design and must not be counted as eligible-but-unprocessed."""
    db_path = str(tmp_path / "test.db")
    await _seed_db(db_path)
    now = datetime.now(timezone.utc)
    activated_at = _iso(now - timedelta(hours=6))
    await _insert_generation(
        db_path, gate_version="tg-shadow-v1+one", activated_at=activated_at
    )
    await _insert_health(
        db_path,
        component=dash_db.TG_SHADOW_ACTIVE_GENERATION_COMPONENT,
        state="running",
        detail=f"{dash_db.TG_SHADOW_ACTIVE_GENERATION_DETAIL_PREFIX}tg-shadow-v1+one",
        updated_at=_iso(now),
    )
    await _insert_message(db_path, pk=1, channel="@c", msg_id=1, posted_at=_iso(now))
    await _insert_message(db_path, pk=2, channel="@c", msg_id=2, posted_at=_iso(now))
    before = await _insert_signal(
        db_path,
        message_pk=1,
        token_id="before-coin",
        symbol="B",
        contract_address="0xb",
        chain="ethereum",
        mcap=1_000_000.0,
        state="RESOLVED",
        channel="@c",
        created_at=_iso(now - timedelta(hours=12)),
        snapshot_json=_snapshot(),
    )
    after = await _insert_signal(
        db_path,
        message_pk=2,
        token_id="after-coin",
        symbol="A",
        contract_address="0xa",
        chain="ethereum",
        mcap=1_000_000.0,
        state="RESOLVED",
        channel="@c",
        created_at=_iso(now - timedelta(hours=1)),
        snapshot_json=_snapshot(),
    )
    await _insert_shadow(
        db_path,
        signal_id=after,
        gate_version="tg-shadow-v1+one",
        actionable=1,
        reason="shadow_pass",
        created_at=_iso(now - timedelta(minutes=30)),
    )

    funnel = await dash_db.get_tg_social_funnel_24h(db_path)
    assert funnel["shadow_armed"] is True
    assert funnel["active_gate_version"] == "tg-shadow-v1+one"
    assert funnel["stages"]["resolved"] == 2
    assert (
        funnel["stages"]["shadow_eligible"] == 1
    ), f"signal {before} predates activation and must be excluded"
    assert funnel["stages"]["shadow_pass"] == 1


# ---------------------------------------------------------------------------
# Endpoint wiring
# ---------------------------------------------------------------------------


@_SKIP_AIOHTTP
@pytest.mark.asyncio
async def test_signals_endpoint_serves_the_query(tmp_path):
    db_path = str(tmp_path / "test.db")
    await _seed_db(db_path)
    now = datetime.now(timezone.utc)
    await _insert_message(db_path, pk=1, channel="@c", msg_id=1, posted_at=_iso(now))
    await _insert_signal(
        db_path,
        message_pk=1,
        token_id="some-coin",
        symbol="SOME",
        contract_address="0xabc",
        chain="ethereum",
        mcap=1_000_000.0,
        state="RESOLVED",
        channel="@c",
        created_at=_iso(now),
        snapshot_json=_snapshot(),
    )

    from httpx import ASGITransport, AsyncClient
    from dashboard.api import create_app

    app = create_app(db_path=db_path)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        resp = await ac.get("/api/tg_social/signals?limit=5")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["signals"]) == 1
    assert body["signals"][0]["symbol"] == "SOME"
    assert set(body) == {"signals", "counts", "capabilities"}


@_SKIP_AIOHTTP
@pytest.mark.asyncio
async def test_alerts_endpoint_adds_lane_status_and_funnel_without_dropping_keys(
    tmp_path,
):
    """The redesign extends the message-centric endpoint; it must not change
    what already shipped. Existing consumers (and the Channels sub-tab) still
    read channels/health/stats_24h/alerts/settings_loaded."""
    db_path = str(tmp_path / "test.db")
    await _seed_db(db_path)
    now = datetime.now(timezone.utc)
    await _insert_health(
        db_path,
        component="listener",
        state="running",
        detail=None,
        updated_at=_iso(now),
    )
    await _insert_message(db_path, pk=1, channel="@c", msg_id=1, posted_at=_iso(now))

    from httpx import ASGITransport, AsyncClient
    from dashboard.api import create_app

    app = create_app(db_path=db_path)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        resp = await ac.get("/api/tg_social/alerts?limit=5")
    assert resp.status_code == 200
    body = resp.json()

    for legacy_key in (
        "channels",
        "health",
        "stats_24h",
        "alerts",
        "settings_loaded",
    ):
        assert legacy_key in body, f"pre-existing key {legacy_key} was dropped"

    lane = body["lane_status"]
    assert lane["trading"]["signal_type"] == "tg_social"
    assert lane["shadow"]["state"] in {
        "off",
        "enabled_not_armed",
        "collecting",
        "unknown",
    }
    assert lane["listener"]["state"] == "running"
    assert set(body["funnel_24h"]["stages"]) == {
        "messages",
        "parsed",
        "signals",
        "resolved",
        "priceable",
        "shadow_eligible",
        "shadow_pass",
        "paper_traded",
    }


def test_lane_status_reports_unknown_rather_than_guessing_when_settings_absent(
    monkeypatch,
):
    """A dashboard that could not read its config must NOT render a quarantined
    lane as an open one. `None` is the honest value; False would be a guess."""
    import dashboard.api as dash_api

    monkeypatch.setattr(dash_api, "_DASHBOARD_SETTINGS", None)
    lane = dash_api._build_tg_lane_status({}, {"active_gate_version": None})
    assert lane["settings_loaded"] is False
    assert lane["trading"]["quarantined"] is None
    assert lane["shadow"]["enabled"] is None
    assert lane["shadow"]["state"] == "unknown"


def test_lane_status_enabled_but_unarmed_is_its_own_state(monkeypatch):
    """`TG_SHADOW_ENABLED=true` with no armed generation is the evaluator's
    loud refusal (no registered feature provider). It is invisible in row
    counts, so it gets its own state rather than collapsing into "off"."""
    import dashboard.api as dash_api

    class _Fake:
        SIGNAL_DISPATCH_QUARANTINE = ["tg_social"]
        TG_SHADOW_ENABLED = True
        TG_SOCIAL_ENABLED = True

    monkeypatch.setattr(dash_api, "_DASHBOARD_SETTINGS", _Fake())
    lane = dash_api._build_tg_lane_status({}, {"active_gate_version": None})
    assert lane["shadow"]["state"] == "enabled_not_armed"
    assert lane["shadow"]["armed"] is False
    assert lane["trading"]["quarantined"] is True


# ---------------------------------------------------------------------------
# Pins against the writers whose literals this layer duplicates
# ---------------------------------------------------------------------------


def test_unresolved_placeholder_matches_the_listener_literal():
    """`dashboard.db` classifies rows by this placeholder. If the listener
    changed it, every unresolved row would silently classify as a real
    identity — and be counted as priceable."""
    listener_src = (ROOT / "scout" / "social" / "telegram" / "listener.py").read_text(
        encoding="utf-8"
    )
    assert f'token_id="{dash_db.TG_UNRESOLVED_PLACEHOLDER}"' in listener_src
    assert f'symbol="{dash_db.TG_UNRESOLVED_PLACEHOLDER}"' in listener_src


def test_shadow_health_constants_match_the_shadow_module():
    """A drifted component name reads as "nothing is armed" forever — the
    dashboard would report an armed generation as off and nobody would see an
    error. Same failure mode the scan-heartbeat pin guards in
    scripts/check_tg_shadow_lag.py."""
    from scout.social.telegram import shadow

    assert (
        dash_db.TG_SHADOW_ACTIVE_GENERATION_COMPONENT
        == shadow.SHADOW_ACTIVE_GENERATION_COMPONENT
    )
    assert (
        dash_db.TG_SHADOW_ACTIVE_GENERATION_DETAIL_PREFIX
        == shadow.ACTIVE_GENERATION_DETAIL_PREFIX
    )
    # And the parse must survive a round trip through the writer's own builder.
    assert shadow.active_generation_detail("tg-shadow-v1+abcd").startswith(
        dash_db.TG_SHADOW_ACTIVE_GENERATION_DETAIL_PREFIX
    )


def test_tg_social_signal_type_matches_the_quarantine_entry():
    """The Overview states "trading disabled — quarantine active" on the basis
    of this exact string appearing in SIGNAL_DISPATCH_QUARANTINE."""
    import dashboard.api as dash_api
    from scout.config import Settings

    default = Settings.model_fields["SIGNAL_DISPATCH_QUARANTINE"].default
    assert dash_api.TG_SOCIAL_SIGNAL_TYPE in default


def test_frontend_labels_cover_every_backend_shadow_reason():
    """Every member of SHADOW_REASONS needs a human label. A reason with no
    entry renders as a sanitized raw code — readable, but the row's primary
    label would be taxonomy, which is exactly what this redesign removes."""
    from scout.social.telegram.shadow import SHADOW_REASONS

    js = (ROOT / "dashboard" / "frontend" / "components" / "tgSignals.js").read_text(
        encoding="utf-8"
    )
    missing = [reason for reason in SHADOW_REASONS if f"  {reason}: {{" not in js]
    assert not missing, f"SHADOW_REASON_INFO is missing labels for: {missing}"


def test_frontend_labels_cover_every_resolution_state():
    from scout.social.telegram.models import ResolutionState

    js = (ROOT / "dashboard" / "frontend" / "components" / "tgSignals.js").read_text(
        encoding="utf-8"
    )
    missing = [
        state.value for state in ResolutionState if f"  {state.value}: {{" not in js
    ]
    assert not missing, f"RESOLUTION_INFO is missing labels for: {missing}"
