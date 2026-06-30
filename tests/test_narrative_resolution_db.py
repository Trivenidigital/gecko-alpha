"""Narrative resolution observability — migration/backfill, insert-status,
split metrics, and watchdog logic. Makes 'fresh inbound but zero resolved'
legible (composition vs failure)."""

import pytest

from scout.db import Database
from scout.api.narrative_resolver import (
    insert_narrative_alert,
    narrative_resolution_alarms,
)

SOL = "9cRCn9rGT8V2imeM2BaKs13yhMEais3ruM3rPvTGpump"


class _Payload:
    def __init__(self, **kw):
        d = dict(
            event_id="e", tweet_id="t", tweet_author="a", tweet_ts="2026-06-30T00:00:00Z",
            tweet_text="x", tweet_text_hash="h", extracted_cashtag=None, extracted_ca=None,
            extracted_chain=None, resolved_coin_id=None, narrative_theme=None,
            urgency_signal=None, classifier_confidence=None, classifier_version="v1",
        )
        d.update(kw)
        self.__dict__.update(d)


@pytest.fixture
async def db(tmp_path):
    d = Database(tmp_path / "narr.db")
    await d.initialize()
    yield d
    await d.close()


async def test_resolution_status_column_added(db):
    cur = await db._conn.execute("PRAGMA table_info(narrative_alerts_inbound)")
    cols = {r[1] for r in await cur.fetchall()}
    assert "resolution_status" in cols


async def test_insert_sets_status_cashtag_only(db):
    await insert_narrative_alert(db._db_path, _Payload(event_id="c1", extracted_ca=None))
    cur = await db._conn.execute(
        "SELECT resolution_status FROM narrative_alerts_inbound WHERE event_id='c1'"
    )
    assert (await cur.fetchone())[0] == "cashtag_only"


async def test_insert_sets_status_ca_resolved_and_unresolved(db):
    await insert_narrative_alert(
        db._db_path, _Payload(event_id="r1", extracted_ca="0xabc", extracted_chain="ethereum",
                              resolved_coin_id="tensor"))
    await insert_narrative_alert(
        db._db_path, _Payload(event_id="u1", extracted_ca="0xdef", extracted_chain="ethereum",
                              resolved_coin_id=None))
    cur = await db._conn.execute(
        "SELECT event_id, resolution_status FROM narrative_alerts_inbound ORDER BY event_id"
    )
    rows = {r[0]: r[1] for r in await cur.fetchall()}
    assert rows["r1"] == "ca_resolved"
    assert rows["u1"] == "ca_unresolved"


async def test_backfill_classifies_legacy_null_status_rows(db):
    # simulate pre-migration rows (resolution_status NULL)
    await db._conn.execute(
        "INSERT INTO narrative_alerts_inbound (event_id, tweet_id, tweet_author, tweet_ts, "
        "tweet_text, tweet_text_hash, extracted_ca, extracted_chain, resolved_coin_id, "
        "classifier_version, resolution_status) VALUES "
        "('L1','t','a','ts','x','h',NULL,NULL,NULL,'v1',NULL),"
        "('L2','t','a','ts','x','h','0xAAA','ethereum',NULL,'v1',NULL)"
    )
    await db._conn.commit()
    await db._migrate_narrative_resolution_status_v1()  # idempotent re-run backfills
    cur = await db._conn.execute(
        "SELECT event_id, resolution_status FROM narrative_alerts_inbound WHERE event_id IN ('L1','L2') ORDER BY event_id"
    )
    rows = {r[0]: r[1] for r in await cur.fetchall()}
    assert rows["L1"] == "cashtag_only"
    assert rows["L2"] == "ca_unresolved"


async def test_backfill_retro_resolves_ca_via_contract_coin_map(db):
    await db.record_contract_coin_map(SOL, "solana", "the-black-bull", "platforms", "high")
    await db._conn.execute(
        "INSERT INTO narrative_alerts_inbound (event_id, tweet_id, tweet_author, tweet_ts, "
        "tweet_text, tweet_text_hash, extracted_ca, extracted_chain, resolved_coin_id, "
        "classifier_version, resolution_status) VALUES "
        "('RR', 't','a','ts','x','h',?, 'solana', NULL, 'v1', NULL)",
        (SOL,),
    )
    await db._conn.commit()
    await db._migrate_narrative_resolution_status_v1()
    cur = await db._conn.execute(
        "SELECT resolved_coin_id, resolution_status FROM narrative_alerts_inbound WHERE event_id='RR'"
    )
    row = await cur.fetchone()
    assert row[0] == "the-black-bull"  # retro-resolved
    assert row[1] == "ca_resolved"


async def test_resolution_stats_split(db):
    for ev, ca, coin in [("a", None, None), ("b", None, None), ("c", "0x1", "tensor"), ("d", "0x2", None)]:
        await insert_narrative_alert(
            db._db_path, _Payload(event_id=ev, extracted_ca=ca,
                                  extracted_chain="ethereum" if ca else None, resolved_coin_id=coin))
    s = await db.narrative_resolution_stats()
    assert s["total"] == 4
    assert s["cashtag_only"] == 2
    assert s["ca_bearing"] == 2
    assert s["ca_resolved"] == 1
    assert s["ca_unresolved"] == 1
    assert s["unclassified"] == 0
    assert s["ca_resolve_rate"] == 0.5


def test_watchdog_alarms_logic():
    # CA-bearing exist but 0 resolved -> alarm; overall-near-zero alone -> NO alarm
    a = narrative_resolution_alarms(
        {"total": 1000, "cashtag_only": 990, "ca_bearing": 10, "ca_resolved": 0,
         "ca_unresolved": 10, "unclassified": 0, "ca_resolve_rate": 0.0},
        resolver_error_count=0,
    )
    assert any("ca" in x.lower() and "resolve" in x.lower() for x in a)


def test_watchdog_no_alarm_on_healthy_composition():
    # 97% cashtag-only but CA path resolving -> NO alarm (composition is expected)
    a = narrative_resolution_alarms(
        {"total": 1000, "cashtag_only": 970, "ca_bearing": 30, "ca_resolved": 20,
         "ca_unresolved": 10, "unclassified": 0, "ca_resolve_rate": 0.667},
        resolver_error_count=0,
    )
    assert a == []


def test_watchdog_alarms_on_unclassified_and_resolver_errors():
    a = narrative_resolution_alarms(
        {"total": 10, "cashtag_only": 5, "ca_bearing": 5, "ca_resolved": 5,
         "ca_unresolved": 0, "unclassified": 3, "ca_resolve_rate": 1.0},
        resolver_error_count=99,
    )
    assert any("unclassified" in x.lower() for x in a)
    assert any("resolver_error" in x.lower() or "resolver error" in x.lower() for x in a)
