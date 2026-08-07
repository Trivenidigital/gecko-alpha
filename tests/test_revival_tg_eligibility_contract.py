"""A paper-only revival must not silently re-enable operator alerting.

`revive_signal_with_baseline` jointly restores `tg_alert_eligible=1` for any
signal in `DEFAULT_ALLOW_SIGNALS` (the R2-I1 fold). That is correct for a normal
Tier-1b revival, and wrong for an instrumentation run.

On 2026-08-03 `gainers_early` was revived as an explicitly paper-only
instrumentation run — authorised with `live_eligible=0` AND
`tg_alert_eligible=0` — and came back with `tg_alert_eligible=1`, because it is
in the default allowlist. Nothing failed and nothing warned; the violation sat
in production until a state reconciliation four days later noticed the flag.

These tests pin both halves of the fix: an explicit opt-out exists, and the
restore-to-1 path can never again happen at info level.
"""

from __future__ import annotations

import pytest

from scout.db import Database
from scout.trading.tg_alert_dispatch import DEFAULT_ALLOW_SIGNALS


@pytest.fixture
async def db(tmp_path):
    d = Database(tmp_path / "revive.db")
    await d.initialize()
    yield d
    await d.close()


async def _suspend(d, signal_type):
    """Put the signal in the post-auto-suspend state: both flags cleared.

    UPDATE, not INSERT: `Database.initialize()` seeds every `signal_params` row
    with its defaults, and the table has NOT NULL exit-geometry columns that a
    bare INSERT would violate.
    """
    cur = await d._conn.execute(
        "UPDATE signal_params SET enabled = 0, tg_alert_eligible = 0, "
        "live_eligible = 0 WHERE signal_type = ?",
        (signal_type,),
    )
    assert cur.rowcount == 1, f"no seeded signal_params row for {signal_type}"
    await d._conn.commit()


async def _flags(d, signal_type):
    cur = await d._conn.execute(
        "SELECT enabled, tg_alert_eligible, live_eligible FROM signal_params "
        "WHERE signal_type = ?",
        (signal_type,),
    )
    return tuple(await cur.fetchone())


class TestPaperOnlyRevivalKeepsAlertingOff:
    """*** THE CONTRACT THAT WAS VIOLATED. ***"""

    async def test_explicit_opt_out_keeps_tg_eligibility_zero(self, db):
        assert "gainers_early" in DEFAULT_ALLOW_SIGNALS, (
            "fixture assumes the lane is in the default allowlist — that is "
            "precisely why the joint restore fires for it"
        )
        await _suspend(db, "gainers_early")

        await db.revive_signal_with_baseline(
            "gainers_early",
            reason="INSTRUMENTATION RUN — paper only",
            restore_tg_alert_eligible=False,
        )

        enabled, tg, live = await _flags(db, "gainers_early")
        assert enabled == 1, "the run must still be enabled"
        assert tg == 0, (
            "a paper-only revival re-enabled operator alerting: it authorises "
            "enabled=1 and nothing else"
        )
        assert live == 0, "revival must never touch live eligibility"

    async def test_the_default_still_restores_for_default_allow_signals(self, db):
        """No blast radius: existing callers keep the historical behaviour."""
        await _suspend(db, "gainers_early")
        await db.revive_signal_with_baseline(
            "gainers_early", reason="normal Tier-1b revival"
        )
        enabled, tg, _ = await _flags(db, "gainers_early")
        assert (enabled, tg) == (1, 1)

    async def test_non_default_allow_signal_is_unaffected_either_way(self, db):
        target = next(
            s
            for s in ("chain_completed", "slow_burn", "trending_catch")
            if s not in DEFAULT_ALLOW_SIGNALS
        )
        await _suspend(db, target)
        await db.revive_signal_with_baseline(target, reason="revive")
        enabled, tg, _ = await _flags(db, target)
        assert (enabled, tg) == (1, 0)

    async def test_explicit_true_restores_even_outside_the_allowlist(self, db):
        target = next(
            s
            for s in ("chain_completed", "slow_burn", "trending_catch")
            if s not in DEFAULT_ALLOW_SIGNALS
        )
        await _suspend(db, target)
        await db.revive_signal_with_baseline(
            target, reason="deliberate", restore_tg_alert_eligible=True
        )
        _, tg, _ = await _flags(db, target)
        assert tg == 1


class TestRestoringAlertingIsNeverSilent:
    """Turning alerting back on is an operator-visible state change.

    It previously logged at info alongside the not-restored case, so the two
    outcomes were indistinguishable without reading the payload.
    """

    async def test_restore_to_one_logs_at_warning(self, db, monkeypatch):
        seen: list[tuple[str, dict]] = []

        import scout.db as scout_db

        class _Spy:
            def warning(self, event, **kw):
                seen.append(("warning", {"event": event, **kw}))

            def info(self, event, **kw):
                seen.append(("info", {"event": event, **kw}))

            def __getattr__(self, _n):
                return lambda *a, **k: None

        monkeypatch.setattr(scout_db, "_db_log", _Spy())
        await _suspend(db, "gainers_early")
        await db.revive_signal_with_baseline("gainers_early", reason="revive")

        tg_events = [
            (lvl, p)
            for lvl, p in seen
            if p.get("event") == "signal_revived_tg_eligible"
        ]
        assert tg_events, "the TG decision was not logged at all"
        lvl, payload = tg_events[0]
        assert lvl == "warning", (
            "restoring operator alerting logged at info — indistinguishable "
            "from the not-restored case without parsing the payload"
        )
        assert payload["restored_to"] == 1
        assert payload["explicitly_requested"] is False

    async def test_opt_out_logs_at_info_not_warning(self, db, monkeypatch):
        seen: list[tuple[str, dict]] = []

        import scout.db as scout_db

        class _Spy:
            def warning(self, event, **kw):
                seen.append(("warning", {"event": event, **kw}))

            def info(self, event, **kw):
                seen.append(("info", {"event": event, **kw}))

            def __getattr__(self, _n):
                return lambda *a, **k: None

        monkeypatch.setattr(scout_db, "_db_log", _Spy())
        await _suspend(db, "gainers_early")
        await db.revive_signal_with_baseline(
            "gainers_early", reason="paper only", restore_tg_alert_eligible=False
        )

        tg_events = [
            (lvl, p)
            for lvl, p in seen
            if p.get("event") == "signal_revived_tg_eligible"
        ]
        assert tg_events
        lvl, payload = tg_events[0]
        assert lvl == "info"
        assert payload["restored_to"] == 0
        assert payload["explicitly_requested"] is True
