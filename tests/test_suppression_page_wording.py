"""J1 — the suppressed-and-idle page must describe the state it is about.

The old body said the combo was in "permanent-suppression state" and told the
operator that "revival requires explicit operator action via
revive_signal_with_baseline". Both halves were wrong in ways that cost time:

* The state is NOT permanent. It clears on a passing parole retest (D4). What
  makes it worth paging is that nothing will START that retest on its own,
  because the combo is not trading and therefore accrues no evidence.
* `revive_signal_with_baseline` is the remedy for the SIGNAL axis
  (`signal_params.enabled`). It does not clear `combo_performance.suppressed`
  on any combo, and on a multi-signal combo it does not touch the combo axis
  at all — so an operator following the page could "revive" and watch nothing
  change.

These tests pin the body against both errors. They deliberately assert on
CONTENT rather than exact prose, so the wording can be improved without
breaking them — but the two axes and the non-permanence must survive.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from scout.db import Database
from scout.trading import combo_refresh


class _StubSender:
    def __init__(self):
        self.calls = 0
        self.messages: list[str] = []

    async def __call__(self, *args):
        self.calls += 1
        texts = [a for a in args if isinstance(a, str)]
        assert len(texts) == 1, f"cannot identify the message body in {args!r}"
        self.messages.append(texts[0])


async def _seed_suppressed_idle(db, combo_key):
    """Suppressed, and no trade anywhere in the refresh window."""
    now = datetime.now(timezone.utc)
    await db._conn.execute(
        "INSERT INTO combo_performance "
        "(combo_key, window, trades, wins, losses, total_pnl_usd, avg_pnl_pct, "
        " win_rate_pct, suppressed, suppressed_at, parole_at, "
        " parole_trades_remaining, refresh_failures, last_refreshed) "
        "VALUES (?, '30d', 25, 4, 21, -100.0, -2.0, 16.0, 1, ?, ?, 0, 0, ?)",
        (
            combo_key,
            (now - timedelta(days=40)).isoformat(),
            (now - timedelta(days=26)).isoformat(),
            (now - timedelta(hours=1)).isoformat(),
        ),
    )
    await db._conn.commit()


async def _page_body(db, settings, combo_key, monkeypatch) -> str:
    sender = _StubSender()
    monkeypatch.setattr(combo_refresh, "_send_permanent_suppression_alert", sender)
    await _seed_suppressed_idle(db, combo_key)
    window_cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    await combo_refresh._process_permanent_suppression(db, settings, window_cutoff)
    assert sender.calls == 1, f"expected exactly one page, got {sender.calls}"
    return sender.messages[0]


async def test_page_no_longer_claims_the_state_is_permanent(
    tmp_path, settings_factory, monkeypatch
):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    try:
        body = await _page_body(db, settings_factory(), "gainers_early", monkeypatch)
        assert "permanent-suppression state" not in body
        assert "permanently" not in body
        # And it says what IS true about clearing.
        assert "not permanent" in body.lower()
        assert "retest" in body
    finally:
        await db.close()


async def test_page_names_both_axes_and_their_distinct_remedies(
    tmp_path, settings_factory, monkeypatch
):
    """THE CORRECTION THAT MATTERS. An operator who reads only
    `revive_signal_with_baseline` will act on the signal axis and see the combo
    stay suppressed. The page has to name both gates."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    try:
        body = await _page_body(db, settings_factory(), "gainers_early", monkeypatch)
        # Both axes named by their actual column, not by prose alone.
        assert "combo_performance.suppressed" in body
        assert "signal_params.enabled" in body
        # Both remedies named, and attached to the right axis.
        combo_axis = body[body.index("combo_performance.suppressed") :]
        combo_axis = combo_axis[: combo_axis.index("signal_params.enabled")]
        assert "retest" in combo_axis, "the combo axis must point at the retest"
        signal_axis = body[body.index("signal_params.enabled") :]
        assert "revive_signal_with_baseline" in signal_axis
    finally:
        await db.close()


async def test_page_qualifies_what_revive_does_on_the_combo_axis(
    tmp_path, settings_factory, monkeypatch
):
    """`revive_signal_with_baseline` is not a no-op on the combo axis, and the
    page must not overcorrect into saying it is: on a BASE combo it re-opens
    the parole window and refills the retest allowance (verified in
    scout/db.py), while KEEPING suppressed=1. On a multi-signal combo it does
    nothing to that axis. Both halves are stated."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    try:
        body = await _page_body(db, settings_factory(), "gainers_early", monkeypatch)
        assert "parole window" in body
        assert "multi-signal" in body
        assert "does not clear the suppression" in body
    finally:
        await db.close()


async def test_page_stays_plain_text_safe(tmp_path, settings_factory, monkeypatch):
    """The body is full of underscored identifiers (`signal_params.enabled`,
    `revive_signal_with_baseline`, `gainers_early`). It goes out
    `parse_mode=None`; a stray Markdown emphasis marker would still be a
    smell, so pin its absence."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    try:
        body = await _page_body(db, settings_factory(), "gainers_early", monkeypatch)
        assert "*" not in body
        assert "_" in body, "fixture is wrong if the body has no underscores at all"
    finally:
        await db.close()


async def test_marker_semantics_are_untouched(tmp_path, settings_factory, monkeypatch):
    """J1 is a WORDING change. The dedup marker, the once-per-entry behaviour
    and the suppression fields must all be exactly as before."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    try:
        s = settings_factory()
        sender = _StubSender()
        monkeypatch.setattr(combo_refresh, "_send_permanent_suppression_alert", sender)
        await _seed_suppressed_idle(db, "gainers_early")

        cur = await db._conn.execute(
            "SELECT suppressed, suppressed_at, parole_at, parole_trades_remaining "
            "FROM combo_performance WHERE combo_key='gainers_early' AND window='30d'"
        )
        before = tuple(await cur.fetchone())

        window_cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
        for _ in range(3):
            await combo_refresh._process_permanent_suppression(db, s, window_cutoff)

        assert sender.calls == 1, "once per entry, not once per refresh"
        cur = await db._conn.execute(
            "SELECT suppressed, suppressed_at, parole_at, parole_trades_remaining, "
            "       perm_suppression_alerted_at "
            "FROM combo_performance WHERE combo_key='gainers_early' AND window='30d'"
        )
        row = await cur.fetchone()
        assert tuple(row)[:4] == before, "a wording change moved suppression state"
        assert row[4] is not None, "the dedup marker was not stamped"
    finally:
        await db.close()


def test_no_permanence_overstatement_remains_in_combo_refresh():
    """Source sweep. The word survives in identifiers that are wire-visible
    (`_process_permanent_suppression`, the `permanent_suppression` return key,
    the `combo_refresh_permanent_suppression` source label) — renaming those
    would be an API change, not a wording fix. What must NOT survive is prose
    telling a reader the state cannot be undone."""
    import inspect

    src = inspect.getsource(combo_refresh)
    # Scoped to prose that asserts SUPPRESSION cannot be undone. A blanket ban
    # on "forever" is wrong here: the module legitimately uses it about ledger
    # row volume ("one identical row every refresh, forever"), which is an
    # accurate statement about a different subject entirely.
    banned = (
        "permanently suppressed",
        "permanent, operator-invisible",
        "``parole_exhausted`` forever",
        "permanent-suppression state",
    )
    hits = [phrase for phrase in banned if phrase in src]
    assert hits == [], f"permanence overstatement still present: {hits}"
