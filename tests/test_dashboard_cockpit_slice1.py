"""Cockpit slice 1 tests (fable-review Phase 2 findings 1-4, 8; GA-35/GA-36).

Covers:
  - outcome integrity field on /api/trading/history (finding 1), incl.
    defensive support for the parallel-branch exit_provenance column
  - window-labeled + all-time stats on /api/trading/stats (finding 4)
  - live signal_params join on the Signal Trust surfaces (findings 2-3):
    suspended signals surface as suspended regardless of registry maturity
  - fabricated closes excluded from scorecards n/win-rate evidence
    (same predicate as scout/trading/auto_suspend.py:_rolling_stats)
  - registry staleness warning when registry_mtime > 7 days old
  - frontend copy-firewall for the new UI elements

OPERATOR INVARIANT under test: trust/stats surfaces read from the live store
the engine writes — never static snapshots.
"""

import json
import os
from datetime import datetime, timedelta, timezone

import pytest
from httpx import ASGITransport, AsyncClient

from dashboard.api import create_app
from scout.db import Database


@pytest.fixture
async def db(tmp_path):
    db_path = tmp_path / "test.db"
    d = Database(db_path)
    await d.initialize()
    yield d, str(db_path)
    await d.close()


@pytest.fixture
async def client(db):
    import dashboard.api as api_mod

    if api_mod._scout_db is not None:
        await api_mod._scout_db.close()
        api_mod._scout_db = None
    d, db_path = db
    app = create_app(db_path=db_path)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c, d
    if api_mod._scout_db is not None:
        await api_mod._scout_db.close()
        api_mod._scout_db = None


async def _insert_closed_trade(
    conn,
    token_id,
    signal_type="volume_spike",
    exit_reason="tp",
    pnl_usd=100.0,
    pnl_pct=10.0,
    closed_at=None,
    status="closed_tp",
):
    now = datetime.now(timezone.utc)
    opened = (now - timedelta(hours=2)).isoformat()
    closed = closed_at or now.isoformat()
    await conn.execute(
        """INSERT INTO paper_trades
           (token_id, symbol, name, chain, signal_type, signal_data,
            entry_price, amount_usd, quantity, tp_pct, sl_pct, tp_price, sl_price,
            status, pnl_usd, pnl_pct, exit_reason, opened_at, closed_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            token_id,
            token_id.upper(),
            token_id.title(),
            "coingecko",
            signal_type,
            json.dumps({}),
            100.0,
            1000.0,
            10.0,
            20.0,
            10.0,
            120.0,
            90.0,
            status,
            pnl_usd,
            pnl_pct,
            exit_reason,
            opened,
            closed,
        ),
    )
    await conn.commit()


# ---------------------------------------------------------------------------
# Finding 1 — outcome integrity on closed-trade rows
# ---------------------------------------------------------------------------


async def test_history_outcome_integrity_derived_from_exit_reason(client):
    c, d = client
    await _insert_closed_trade(
        d._conn, "fab", exit_reason="expired_stale_no_price", pnl_usd=0.0, pnl_pct=0.0
    )
    await _insert_closed_trade(
        d._conn, "stale", exit_reason="expired_stale_price", pnl_usd=-5.0, pnl_pct=-1.0
    )
    await _insert_closed_trade(d._conn, "clean", exit_reason="tp")

    resp = await c.get("/api/trading/history")
    assert resp.status_code == 200
    by_token = {r["token_id"]: r for r in resp.json()}
    assert by_token["fab"]["outcome_integrity"] == "force-closed-unpriced"
    assert by_token["stale"]["outcome_integrity"] == "stale-priced"
    assert by_token["clean"]["outcome_integrity"] == "priced"
    # exit_reason itself remains exposed for the UI
    assert by_token["fab"]["exit_reason"] == "expired_stale_no_price"


async def test_history_outcome_integrity_fallback_when_column_absent(client):
    """Transitional-defensive coverage: the dashboard's missing-column fallback
    is LOAD-BEARING during the deploy-#1 -> deploy-#2 gap (dashboard ships
    before the price_provenance_v1 migration reaches the prod DB). Post-#408
    the shared fixture's initialize() creates the column, so we DROP it to
    reconstruct the pre-migration schema. TOMBSTONE: delete this test and the
    fallback branch together once no deployment can run a pre-20260705 DB.
    """
    c, d = client
    await d._conn.execute("ALTER TABLE paper_trades DROP COLUMN exit_provenance")
    await d._conn.commit()
    await _insert_closed_trade(d._conn, "clean", exit_reason="tp")
    resp = await c.get("/api/trading/history")
    assert resp.status_code == 200
    row = resp.json()[0]
    assert row["exit_provenance"] is None
    assert row["outcome_integrity"] == "priced"


async def test_history_outcome_integrity_prefers_exit_provenance_when_present(client):
    """When exit_provenance (added by the price_provenance_v1 migration) is
    populated, it wins over exit_reason mapping."""
    c, d = client
    await _insert_closed_trade(d._conn, "prov", exit_reason="expired")
    await d._conn.execute(
        "UPDATE paper_trades SET exit_provenance = 'entry_fallback' WHERE token_id = 'prov'"
    )
    await _insert_closed_trade(d._conn, "mkt", exit_reason="expired_stale_price")
    await d._conn.execute(
        "UPDATE paper_trades SET exit_provenance = 'market' WHERE token_id = 'mkt'"
    )
    await d._conn.commit()

    resp = await c.get("/api/trading/history")
    assert resp.status_code == 200
    by_token = {r["token_id"]: r for r in resp.json()}
    assert by_token["prov"]["exit_provenance"] == "entry_fallback"
    assert by_token["prov"]["outcome_integrity"] == "force-closed-unpriced"
    # market provenance overrides the stale exit_reason mapping
    assert by_token["mkt"]["outcome_integrity"] == "priced"


# ---------------------------------------------------------------------------
# Finding 4 — window-labeled headline stats + all-time secondary figures
# ---------------------------------------------------------------------------


async def test_stats_are_window_labeled_and_carry_all_time(client):
    c, d = client
    old_close = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    await _insert_closed_trade(
        d._conn, "old", pnl_usd=50.0, pnl_pct=5.0, closed_at=old_close
    )
    await _insert_closed_trade(d._conn, "recent", pnl_usd=-10.0, pnl_pct=-1.0)

    resp = await c.get("/api/trading/stats")
    assert resp.status_code == 200
    data = resp.json()
    # top-level figures stay 7d-windowed (backward compatible) but labeled
    assert data["window_days"] == 7
    assert data["total_trades"] == 1
    # all-time figures come from the same live query, unwindowed
    assert data["all_time"]["total_trades"] == 2
    assert data["all_time"]["wins"] == 1
    assert data["all_time"]["total_pnl_usd"] == pytest.approx(40.0)
    assert data["all_time"]["win_rate_pct"] == pytest.approx(50.0)


# ---------------------------------------------------------------------------
# Findings 2-3 — fabricated exclusion in scorecards evidence + live join
# ---------------------------------------------------------------------------


async def test_scorecards_exclude_fabricated_rows_from_evidence(client):
    """Same predicate as auto_suspend._rolling_stats: a fabricated
    expired_stale_no_price close must not dilute n/win-rate."""
    c, d = client
    await _insert_closed_trade(
        d._conn,
        "fab",
        signal_type="evidence_signal",
        exit_reason="expired_stale_no_price",
        pnl_usd=0.0,
        pnl_pct=0.0,
        status="closed_expired",
    )
    await _insert_closed_trade(
        d._conn,
        "real",
        signal_type="evidence_signal",
        exit_reason="tp",
        pnl_usd=100.0,
        pnl_pct=10.0,
    )

    resp = await c.get("/api/signal_trust/scorecards")
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["meta"]["cohort_policy"] == "closed_paper_trades_excl_fabricated"
    row = next(r for r in payload["rows"] if r["signal_type"] == "evidence_signal")
    w7 = next(w for w in row["windows"] if w["days"] == 7)
    assert w7["closed"]["closed_n"] == 1
    assert w7["closed"]["wins"] == 1
    assert w7["closed"]["win_rate_pct"] == pytest.approx(100.0)
    assert w7["closed"]["total_pnl_usd"] == pytest.approx(100.0)


async def test_scorecards_rows_join_live_signal_params(client):
    """GA-35/GA-36: a suspended signal carries live suspension state on its
    scorecards row regardless of what the static registry says."""
    c, d = client
    await d._conn.execute("""UPDATE signal_params
           SET enabled = 0,
               suspended_at = '2026-06-06T00:00:00+00:00',
               suspended_reason = 'hard_loss'
           WHERE signal_type = 'chain_completed'""")
    await d._conn.commit()

    resp = await c.get("/api/signal_trust/scorecards")
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["meta"]["signal_params_joined"] is True
    row = next(r for r in payload["rows"] if r["signal_type"] == "chain_completed")
    assert row["live"] is not None
    assert row["live"]["enabled"] == 0
    assert row["live"]["suspended_at"] == "2026-06-06T00:00:00+00:00"
    assert row["live"]["suspended_reason"] == "hard_loss"
    # an untouched seeded signal remains enabled with no suspension state
    enabled_row = next(r for r in payload["rows"] if r["signal_type"] == "volume_spike")
    assert enabled_row["live"] is not None
    assert enabled_row["live"]["enabled"] == 1
    assert enabled_row["live"]["suspended_at"] is None


async def test_registry_entries_join_live_signal_params(client):
    c, d = client
    await d._conn.execute("""UPDATE signal_params
           SET enabled = 0,
               suspended_at = '2026-06-06T00:00:00+00:00',
               suspended_reason = 'hard_loss'
           WHERE signal_type = 'chain_completed'""")
    await d._conn.commit()

    resp = await c.get("/api/signal_trust_registry")
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["meta"]["signal_params_joined"] is True
    assert "registry_stale" in payload["meta"]
    entries = payload["registry"]["entries"]
    entry = next(e for e in entries if e["signal_type"] == "chain_completed")
    # registry still says trusted_experimental; live join surfaces suspension
    assert entry["maturity_state"] == "trusted_experimental"
    assert entry["live"]["enabled"] == 0
    assert entry["live"]["suspended_at"] == "2026-06-06T00:00:00+00:00"


def _valid_registry_doc():
    gates = [
        "visibility_only",
        "not_for_pruning",
        "not_for_suppression",
        "not_for_auto_disable",
        "not_for_sizing",
        "not_for_execution",
        "not_for_alerting",
        "not_for_source_ranking",
    ]
    return {
        "schema_version": "signal_trust_registry.v1",
        "experimental": True,
        **{g: True for g in gates},
        "notes": "test registry",
        "maturity_states": [
            "trusted_experimental",
            "context_only",
            "data_insufficient",
        ],
        "entries": [
            {
                "signal_type": "volume_spike",
                "maturity_state": "trusted_experimental",
                "data_quality": {"warning": "low n"},
                "operator_gate": gates,
                "next_gate": {"type": "n", "threshold": "n>=10"},
            }
        ],
    }


async def test_registry_stale_warning_when_mtime_older_than_7_days(
    client, tmp_path, monkeypatch
):
    c, _ = client
    reg = tmp_path / "old_registry.json"
    reg.write_text(json.dumps(_valid_registry_doc()), encoding="utf-8")
    ten_days_ago = (datetime.now(timezone.utc) - timedelta(days=10)).timestamp()
    os.utime(reg, (ten_days_ago, ten_days_ago))
    monkeypatch.setenv("GECKO_SIGNAL_TRUST_REGISTRY_PATH", str(reg))
    monkeypatch.setenv("GECKO_ALLOW_ARBITRARY_SIGNAL_TRUST_REGISTRY_PATH", "1")

    resp = await c.get("/api/signal_trust_registry")
    assert resp.status_code == 200
    meta = resp.json()["meta"]
    assert meta["registry_stale"] is True
    assert (
        meta["registry_stale_warning"]
        == "registry stale — maturity labels may not reflect current state"
    )


async def test_registry_fresh_file_is_not_stale(client, tmp_path, monkeypatch):
    c, _ = client
    reg = tmp_path / "fresh_registry.json"
    reg.write_text(json.dumps(_valid_registry_doc()), encoding="utf-8")
    monkeypatch.setenv("GECKO_SIGNAL_TRUST_REGISTRY_PATH", str(reg))
    monkeypatch.setenv("GECKO_ALLOW_ARBITRARY_SIGNAL_TRUST_REGISTRY_PATH", "1")

    resp = await c.get("/api/signal_trust_registry")
    assert resp.status_code == 200
    meta = resp.json()["meta"]
    assert meta["registry_stale"] is False
    assert "registry_stale_warning" not in meta


async def test_registry_live_join_degrades_without_503(tmp_path, monkeypatch):
    """Registry surface is visibility-only: a missing/uninitialized DB must
    degrade to signal_params_joined=false, never 503 the registry itself."""
    import dashboard.api as api_mod

    if api_mod._scout_db is not None:
        await api_mod._scout_db.close()
        api_mod._scout_db = None
    app = create_app(db_path=str(tmp_path / "missing.db"))
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        resp = await c.get("/api/signal_trust_registry")
    if api_mod._scout_db is not None:
        await api_mod._scout_db.close()
        api_mod._scout_db = None

    assert resp.status_code == 200
    meta = resp.json()["meta"]
    assert meta["signal_params_joined"] is False


# ---------------------------------------------------------------------------
# Frontend copy-firewall (finding 8 + new UI elements) — text-level checks,
# same pattern as tests/test_dashboard_frontend_layout.py.
# ---------------------------------------------------------------------------


def _read_component(name):
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    return (root / "dashboard" / "frontend" / "components" / name).read_text(
        encoding="utf-8"
    )


def test_trading_tab_renders_integrity_chip_and_window_labels():
    jsx = _read_component("TradingTab.jsx")
    assert "outcome_integrity" in jsx
    assert "force-closed-unpriced" in jsx
    assert "stale-priced" in jsx
    assert "IntegrityChip" in jsx
    # headline tiles are explicitly window-labeled with all-time secondary
    assert "Realized PnL ({windowDays}d)" in jsx
    assert "Win Rate ({windowDays}d)" in jsx
    assert "Total Trades ({windowDays}d)" in jsx
    assert "all-time" in jsx
    # live-store invariant stated at the derivation site
    assert "never from a static snapshot" in jsx


def test_signal_trust_tab_renders_live_badge_stale_warning_and_provenance():
    jsx = _read_component("SignalTrustTab.jsx")
    assert "SUSPENDED" in jsx
    assert "LiveStatusBadge" in jsx
    assert "registry stale — maturity labels may not reflect current state" in jsx
    assert "ProvenanceExpander" in jsx
    assert "provenance" in jsx
    # finding 8: the raw meta debug strings must not render as bare text —
    # they live inside the provenance expander now.
    assert "read_only=${String(scMeta.read_only" in jsx
    # live-store invariant stated on the component
    assert "never static snapshots" in jsx
