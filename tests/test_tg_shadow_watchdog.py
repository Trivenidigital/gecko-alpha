"""W5 — `scripts/check_tg_shadow_lag.py` three-state matrix.

The watchdog exists to convert a persistent evidence gap into a page. It has
to separate three states that look identical from a row count alone:

  quiet         no eligible work is overdue, OR a scan is mid-flight
  writer_failing rows survived a completed scan and are still unshadowed
  writer_dead    rows are overdue and no scan has completed within cadence

Collapsing the last two would be the §12a failure this watchdog prevents:
catch-up suppression must never mask a crashed writer.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from scripts.check_tg_shadow_lag import (
    ACTIVE_GENERATION_COMPONENT,
    ACTIVE_GENERATION_DETAIL_PREFIX,
    SCAN_HEALTH_COMPONENT_PREFIX,
    _Config,
    _compose,
    evaluate_tg_shadow_lag,
    main,
    parse_active_gate_version,
    scan_health_component,
)

# Sentinel: write the marker for `_GATE` iff a generation exists. Tests that
# care pass an explicit gate_version, or None for "writer never published one".
_AUTO_MARKER = "__auto__"

_GATE = "tg-shadow-v1+0123456789abcdef"
# A retired generation. Its `activated_at` is older, so `_GATE` is current.
_OLD_GATE = "tg-shadow-v1+9999999999999999"
_LAG_MIN = 60
_CADENCE_MIN = 30


def _build_db(
    db_path,
    *,
    now: datetime,
    generation_age_h: float | None = 6.0,
    resolved_ages_min: tuple[float, ...] = (),
    shadowed_signal_ids: tuple[int, ...] = (),
    last_scan_age_min: float | None = None,
    shadow_gate_version: str = _GATE,
    heartbeat_gate_version: str | None = None,
    extra_generations: tuple[tuple[str, float], ...] = (),
    active_marker: str | None = _AUTO_MARKER,
) -> None:
    conn = sqlite3.connect(str(db_path))
    conn.executescript("""
        CREATE TABLE tg_social_signals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            resolution_state TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE tg_act_shadow (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            signal_id INTEGER NOT NULL,
            gate_version TEXT NOT NULL,
            actionable INTEGER NOT NULL,
            reason TEXT NOT NULL,
            features_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(signal_id, gate_version)
        );
        CREATE TABLE tg_act_shadow_generations (
            gate_version TEXT PRIMARY KEY,
            activated_at TEXT NOT NULL
        );
        CREATE TABLE tg_social_health (
            component TEXT PRIMARY KEY,
            listener_state TEXT NOT NULL,
            last_message_at TEXT,
            updated_at TEXT NOT NULL,
            detail TEXT
        );
        """)
    if generation_age_h is not None:
        conn.execute(
            "INSERT INTO tg_act_shadow_generations VALUES (?, ?)",
            (_GATE, (now - timedelta(hours=generation_age_h)).isoformat()),
        )
    for age_min in resolved_ages_min:
        conn.execute(
            "INSERT INTO tg_social_signals (resolution_state, created_at) "
            "VALUES ('RESOLVED', ?)",
            ((now - timedelta(minutes=age_min)).isoformat(),),
        )
    for signal_id in shadowed_signal_ids:
        conn.execute(
            "INSERT INTO tg_act_shadow (signal_id, gate_version, actionable, "
            "reason, features_json, created_at) VALUES (?, ?, 0, 'r', '{}', ?)",
            (signal_id, shadow_gate_version, now.isoformat()),
        )
    for gv, age_h in extra_generations:
        conn.execute(
            "INSERT INTO tg_act_shadow_generations VALUES (?, ?)",
            (gv, (now - timedelta(hours=age_h)).isoformat()),
        )
    # The writer publishes which generation it is actually running. Without
    # this row the watchdog treats the deployment as not-yet-armed, so every
    # armed-state fixture needs it.
    marker_gate = _GATE if active_marker == _AUTO_MARKER else active_marker
    if marker_gate is not None and (generation_age_h is not None or extra_generations):
        conn.execute(
            "INSERT INTO tg_social_health "
            "(component, listener_state, last_message_at, updated_at, detail) "
            "VALUES (?, 'running', ?, ?, ?)",
            (
                ACTIVE_GENERATION_COMPONENT,
                now.isoformat(),
                now.isoformat(),
                f"{ACTIVE_GENERATION_DETAIL_PREFIX}{marker_gate}",
            ),
        )
    if last_scan_age_min is not None:
        scan_at = (now - timedelta(minutes=last_scan_age_min)).isoformat()
        conn.execute(
            "INSERT INTO tg_social_health "
            "(component, listener_state, last_message_at, updated_at, detail) "
            "VALUES (?, 'running', ?, ?, 'scanned=0 written=0')",
            (
                scan_health_component(heartbeat_gate_version or _GATE),
                scan_at,
                scan_at,
            ),
        )
    conn.commit()
    conn.close()


def _evaluate(db_path, now: datetime) -> dict:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        return evaluate_tg_shadow_lag(
            conn,
            now=now,
            lag_threshold_min=_LAG_MIN,
            scan_cadence_min=_CADENCE_MIN,
        )
    finally:
        conn.close()


@pytest.fixture
def now() -> datetime:
    return datetime(2026, 8, 12, 12, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Three-state matrix
# ---------------------------------------------------------------------------


def test_quiet_when_no_work_arrived(tmp_path, now):
    """A quiet Telegram day is not a failure. Paging on it trains the operator
    to ignore the channel."""
    db = tmp_path / "s.db"
    _build_db(db, now=now, last_scan_age_min=5)
    state = _evaluate(db, now)
    assert state["status"] == "quiet"
    assert state["overdue_count"] == 0


def test_quiet_when_eligible_rows_are_not_yet_overdue(tmp_path, now):
    db = tmp_path / "s.db"
    _build_db(db, now=now, resolved_ages_min=(10, 30, 59), last_scan_age_min=5)
    state = _evaluate(db, now)
    assert state["status"] == "quiet"
    assert state["overdue_count"] == 0


def test_quiet_when_every_overdue_row_is_shadowed(tmp_path, now):
    db = tmp_path / "s.db"
    _build_db(
        db,
        now=now,
        resolved_ages_min=(120, 180),
        shadowed_signal_ids=(1, 2),
        last_scan_age_min=5,
    )
    state = _evaluate(db, now)
    assert state["status"] == "quiet"
    assert state["overdue_count"] == 0


def test_writer_failing_when_rows_survived_a_completed_scan(tmp_path, now):
    """The row was overdue, a scan ran anyway, and the row is still
    unshadowed — the writer is alive and losing work."""
    db = tmp_path / "s.db"
    _build_db(db, now=now, resolved_ages_min=(120,), last_scan_age_min=5)
    state = _evaluate(db, now)
    assert state["status"] == "writer_failing"
    assert state["overdue_count"] == 1
    assert state["last_scan_at"] is not None


def test_writer_dead_when_no_scan_within_cadence(tmp_path, now):
    db = tmp_path / "s.db"
    _build_db(db, now=now, resolved_ages_min=(120,), last_scan_age_min=90)
    state = _evaluate(db, now)
    assert state["status"] == "writer_dead"
    assert state["overdue_count"] == 1


def test_writer_dead_when_no_scan_has_ever_completed(tmp_path, now):
    """Absence of scans is itself the failure — an unstarted writer must not
    hide behind 'no scan has completed since the row became overdue'."""
    db = tmp_path / "s.db"
    _build_db(db, now=now, resolved_ages_min=(120,), last_scan_age_min=None)
    state = _evaluate(db, now)
    assert state["status"] == "writer_dead"
    assert state["last_scan_at"] is None


def test_dead_wins_over_failing(tmp_path, now):
    """Both conditions hold; the more actionable diagnosis is reported."""
    db = tmp_path / "s.db"
    _build_db(
        db,
        now=now,
        generation_age_h=24.0,
        resolved_ages_min=(600,),
        last_scan_age_min=120,
    )
    state = _evaluate(db, now)
    assert state["status"] == "writer_dead"


def test_catch_up_in_flight_does_not_page(tmp_path, now):
    """The row crossed the lag threshold one minute ago; the most recent
    completed scan predates that crossing and the writer is scanning within
    cadence. Nothing has yet failed to shadow it."""
    db = tmp_path / "s.db"
    _build_db(db, now=now, resolved_ages_min=(61,), last_scan_age_min=20)
    state = _evaluate(db, now)
    assert state["status"] == "quiet"
    assert (
        state["overdue_count"] == 1
    ), "the row IS overdue; it just isn't a failure yet"


# ---------------------------------------------------------------------------
# Generation awareness
# ---------------------------------------------------------------------------


def test_silent_without_a_generation(tmp_path, now):
    """Deploy-dark: tables installed, activation never happened. Nothing is
    eligible, so there is nothing to page about."""
    db = tmp_path / "s.db"
    _build_db(db, now=now, generation_age_h=None, resolved_ages_min=(600,))
    state = _evaluate(db, now)
    assert state["status"] == "no_generation"
    assert state["overdue_count"] == 0


def test_rows_predating_activation_are_not_eligible(tmp_path, now):
    """First activation starts from zero eligible rows — otherwise flipping
    the flag on a database with months of history pages immediately."""
    db = tmp_path / "s.db"
    _build_db(db, now=now, generation_age_h=1.0, resolved_ages_min=(600,))
    state = _evaluate(db, now)
    assert state["status"] == "quiet"
    assert state["overdue_count"] == 0


def test_decisions_under_a_different_gate_version_do_not_count(tmp_path, now):
    """A threshold change starts a new generation prospectively; the previous
    generation's rows are not this generation's evidence."""
    db = tmp_path / "s.db"
    _build_db(
        db,
        now=now,
        resolved_ages_min=(120,),
        shadowed_signal_ids=(1,),
        shadow_gate_version="tg-shadow-v1+deadbeefdeadbeef",
        last_scan_age_min=5,
    )
    state = _evaluate(db, now)
    assert state["status"] == "writer_failing"
    assert state["overdue_count"] == 1


def test_unresolved_rows_are_not_eligible(tmp_path, now):
    db = tmp_path / "s.db"
    _build_db(db, now=now, last_scan_age_min=5)
    conn = sqlite3.connect(str(db))
    conn.execute(
        "INSERT INTO tg_social_signals (resolution_state, created_at) VALUES "
        "('UNRESOLVED_TERMINAL', ?)",
        ((now - timedelta(hours=3)).isoformat(),),
    )
    conn.commit()
    conn.close()
    assert _evaluate(db, now)["status"] == "quiet"


# ---------------------------------------------------------------------------
# main() wiring
#
# `main()` reads the real clock, so these fixtures are built relative to
# `datetime.now(timezone.utc)` rather than the frozen `now` used above. Pinning
# them to a fixed anchor would pass on the day it was written and fail every
# day after.
# ---------------------------------------------------------------------------


@pytest.fixture
def real_now() -> datetime:
    return datetime.now(timezone.utc)


def test_disabled_flag_is_silent_and_sends_nothing(tmp_path, real_now, monkeypatch):
    db = tmp_path / "s.db"
    _build_db(
        db,
        now=real_now,
        generation_age_h=24.0,
        resolved_ages_min=(600,),
        last_scan_age_min=None,
    )
    sent: list[str] = []
    monkeypatch.setattr(
        "scripts.check_tg_shadow_lag._SEND", _recorder(sent), raising=False
    )
    code = main(
        [
            "--db",
            str(db),
            "--enabled",
            "false",
            "--lag-threshold-min",
            str(_LAG_MIN),
            "--scan-cadence-min",
            str(_CADENCE_MIN),
        ]
    )
    assert code == 0
    assert sent == []


def test_page_is_dispatched_and_delivered_on_a_failing_writer(
    tmp_path, real_now, monkeypatch
):
    import structlog

    db = tmp_path / "s.db"
    _build_db(
        db,
        now=real_now,
        generation_age_h=24.0,
        resolved_ages_min=(600,),
        last_scan_age_min=5,
    )
    sent: list[str] = []
    monkeypatch.setattr("scripts.check_tg_shadow_lag._SEND", _recorder(sent))

    with structlog.testing.capture_logs() as logs:
        code = main(
            [
                "--db",
                str(db),
                "--enabled",
                "true",
                "--lag-threshold-min",
                str(_LAG_MIN),
                "--scan-cadence-min",
                str(_CADENCE_MIN),
            ]
        )
    assert code == 5
    assert len(sent) == 1

    events = [e["event"] for e in logs]
    assert "tg_shadow_lag_alert_dispatched" in events
    assert "tg_shadow_lag_alert_delivered" in events


def test_no_page_when_quiet(tmp_path, real_now, monkeypatch):
    db = tmp_path / "s.db"
    _build_db(db, now=real_now, last_scan_age_min=5)
    sent: list[str] = []
    monkeypatch.setattr("scripts.check_tg_shadow_lag._SEND", _recorder(sent))
    code = main(
        [
            "--db",
            str(db),
            "--enabled",
            "true",
            "--lag-threshold-min",
            str(_LAG_MIN),
            "--scan-cadence-min",
            str(_CADENCE_MIN),
        ]
    )
    assert code == 0
    assert sent == []


def test_missing_db_is_an_error_not_a_silent_pass(tmp_path):
    code = main(
        [
            "--db",
            str(tmp_path / "absent.db"),
            "--enabled",
            "true",
            "--lag-threshold-min",
            str(_LAG_MIN),
            "--scan-cadence-min",
            str(_CADENCE_MIN),
        ]
    )
    assert code == 1


def test_page_body_carries_count_and_last_scan(now):
    body = _compose(
        {
            "status": "writer_failing",
            "gate_version": _GATE,
            "activated_at": now.isoformat(),
            "overdue_count": 7,
            "oldest_overdue_at": (now - timedelta(hours=3)).isoformat(),
            "last_scan_at": (now - timedelta(minutes=5)).isoformat(),
        },
        _Config(
            enabled=True, lag_threshold_min=_LAG_MIN, scan_cadence_min=_CADENCE_MIN
        ),
    )
    assert "7" in body
    assert (now - timedelta(minutes=5)).isoformat() in body
    assert _GATE in body
    # Plain text: the body carries underscore-bearing identifiers that
    # MarkdownV1 would silently eat.
    assert "*" not in body and "_" in body


def _recorder(sink: list[str]):
    async def _record(text: str) -> None:
        sink.append(text)

    return _record


def test_watchdog_reads_the_component_the_writer_stamps():
    """The writer and the watchdog name the health row in two different
    modules. If they drift, the watchdog reads a component nobody writes,
    reports `last_scan_at = None` forever, and pages on every eligible row —
    or worse, is dismissed as noisy and ignored."""
    from scout.social.telegram.shadow import (
        SHADOW_SCAN_COMPONENT_PREFIX,
        scan_heartbeat_component,
    )

    assert SCAN_HEALTH_COMPONENT_PREFIX == SHADOW_SCAN_COMPONENT_PREFIX
    assert scan_health_component(_GATE) == scan_heartbeat_component(_GATE)


def test_stale_heartbeat_from_a_retired_generation_does_not_suppress_the_page(
    tmp_path, now
):
    """The failure a shared heartbeat row would create.

    A threshold bump retires the old gate_version and activates the current
    one. The OLD generation's writer stamped a heartbeat 5 minutes ago; the NEW
    generation's writer has never run. Under a single shared component that
    fresh row would read as "a scan completed recently", and the dead writer
    would go unpaged for as long as the operator left it. Bound to the
    gate_version, the current generation correctly has no heartbeat at all."""
    db = tmp_path / "s.db"
    _build_db(
        db,
        now=now,
        generation_age_h=6.0,
        extra_generations=((_OLD_GATE, 48.0),),
        resolved_ages_min=(120,),
        last_scan_age_min=5,
        heartbeat_gate_version=_OLD_GATE,
        active_marker=_GATE,
    )
    state = _evaluate(db, now)
    assert state["gate_version"] == _GATE
    assert state["status"] == "writer_dead"
    assert state["last_scan_at"] is None


def test_current_generation_heartbeat_does_suppress_the_dead_branch(tmp_path, now):
    """Control for the test above: identical shape, heartbeat stamped under the
    CURRENT gate_version, so condition (b) does not fire and the diagnosis is
    the more specific `writer_failing`."""
    db = tmp_path / "s.db"
    _build_db(
        db,
        now=now,
        generation_age_h=6.0,
        extra_generations=((_OLD_GATE, 48.0),),
        resolved_ages_min=(120,),
        last_scan_age_min=5,
        heartbeat_gate_version=_GATE,
    )
    state = _evaluate(db, now)
    assert state["status"] == "writer_failing"
    assert state["last_scan_at"] is not None


# ---------------------------------------------------------------------------
# Active-generation resolution (marker, not registry chronology)
# ---------------------------------------------------------------------------


def test_resumed_older_generation_is_the_active_one(tmp_path, now):
    """The defect this marker exists to fix.

    Registry: v1 activated 48h ago, v2 activated 6h ago. The operator has
    returned to the v1 configuration, so the writer RESUMED v1 — which by
    design does not rewrite v1's registry row. The newest row is still v2.

    Resolving by chronology picks v2, finds no v2 heartbeat, and pages
    `writer_dead` — a false alarm about a generation nobody is running, while
    v1 (which is healthy here: its overdue rows are shadowed and its heartbeat
    is fresh) goes unmonitored. Resolving from the marker picks v1 and stays
    silent.

    Correct behaviour is therefore SILENCE, and the bug's signature is a page —
    the two outcomes cannot be confused.
    """
    db = tmp_path / "s.db"
    _build_db(
        db,
        now=now,
        generation_age_h=6.0,  # _GATE — chronologically newest
        extra_generations=((_OLD_GATE, 48.0),),  # v1 — older, and the one resumed
        resolved_ages_min=(180,),
        shadowed_signal_ids=(1,),
        shadow_gate_version=_OLD_GATE,
        last_scan_age_min=5,
        heartbeat_gate_version=_OLD_GATE,
        active_marker=_OLD_GATE,
    )
    state = _evaluate(db, now)
    assert state["gate_version"] == _OLD_GATE, "resolved the wrong generation"
    assert state["status"] == "quiet"
    assert state["overdue_count"] == 0


def test_resumed_older_generation_still_reports_its_own_failures(tmp_path, now):
    """Companion to the test above: resolving to v1 must not mean ignoring v1.
    Same resume shape, but v1's rows are NOT shadowed, so v1's real failing
    state surfaces — under v1's gate_version, not v2's."""
    db = tmp_path / "s.db"
    _build_db(
        db,
        now=now,
        generation_age_h=6.0,
        extra_generations=((_OLD_GATE, 48.0),),
        resolved_ages_min=(180,),
        last_scan_age_min=5,
        heartbeat_gate_version=_OLD_GATE,
        active_marker=_OLD_GATE,
    )
    state = _evaluate(db, now)
    assert state["gate_version"] == _OLD_GATE
    assert state["status"] == "writer_failing"
    assert state["overdue_count"] == 1


def test_registry_rows_without_a_marker_are_not_yet_armed(tmp_path, now):
    """Ambiguous rather than broken: a registry row with no marker means the
    writer never announced itself. Paging on it would fire on any partially
    activated deployment, so it is logged and left silent."""
    db = tmp_path / "s.db"
    _build_db(
        db,
        now=now,
        resolved_ages_min=(600,),
        last_scan_age_min=None,
        active_marker=None,
    )
    state = _evaluate(db, now)
    assert state["status"] == "no_active_marker"
    assert state["overdue_count"] == 0


def test_marker_naming_an_unknown_generation_pages(tmp_path, now):
    """Marker and registry disagree — shadow health is unevaluable, and
    silence would hide that. Distinct from a dead writer."""
    db = tmp_path / "s.db"
    _build_db(
        db,
        now=now,
        resolved_ages_min=(120,),
        last_scan_age_min=5,
        active_marker="tg-shadow-v1+neverregistered",
    )
    state = _evaluate(db, now)
    assert state["status"] == "active_generation_inconsistent"
    assert state["inconsistency"] == "marker_names_a_gate_version_with_no_registry_row"
    body = _compose(
        state,
        _Config(
            enabled=True, lag_threshold_min=_LAG_MIN, scan_cadence_min=_CADENCE_MIN
        ),
    )
    assert "tg-shadow-v1+neverregistered" in body
    assert "registry" in body


def test_unparseable_marker_pages(tmp_path, now):
    db = tmp_path / "s.db"
    _build_db(db, now=now, resolved_ages_min=(120,), last_scan_age_min=5)
    conn = sqlite3.connect(str(db))
    conn.execute(
        "UPDATE tg_social_health SET detail = 'garbage' WHERE component = ?",
        (ACTIVE_GENERATION_COMPONENT,),
    )
    conn.commit()
    conn.close()
    state = _evaluate(db, now)
    assert state["status"] == "active_generation_inconsistent"
    assert state["inconsistency"] == "unparseable_marker"


@pytest.mark.parametrize(
    "detail, expected",
    [
        ("gate_version=tg-shadow-v1+abc", "tg-shadow-v1+abc"),
        ("  gate_version=tg-shadow-v1+abc  ", "tg-shadow-v1+abc"),
        ("gate_version=", None),
        ("garbage", None),
        ("", None),
        (None, None),
    ],
)
def test_marker_detail_parser(detail, expected):
    assert parse_active_gate_version(detail) == expected


def test_watchdog_parses_the_marker_the_writer_writes():
    """Cross-module pin, same reason as the scan-component pin: the writer
    formats this string and the watchdog parses it, in two files that share no
    import."""
    from scout.social.telegram.shadow import (
        ACTIVE_GENERATION_DETAIL_PREFIX as WRITER_PREFIX,
        SHADOW_ACTIVE_GENERATION_COMPONENT,
        active_generation_detail,
    )

    assert ACTIVE_GENERATION_COMPONENT == SHADOW_ACTIVE_GENERATION_COMPONENT
    assert ACTIVE_GENERATION_DETAIL_PREFIX == WRITER_PREFIX
    assert parse_active_gate_version(active_generation_detail(_GATE)) == _GATE
