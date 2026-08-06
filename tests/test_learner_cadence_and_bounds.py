"""Two residual defects from PR #510, and the telemetry scope qualifier.

**Dead cadence anchor.** The loop read `last_daily_learn_at` /
`last_weekly_learn_at` to decide whether a cycle was already done, but nothing
had written those keys since attempt/success/failure stamps replaced them. Both
were frozen in prod (`2026-08-05T01:01:12`, `2026-08-02T02:13:34`), so
`now - anchor` grew without bound and the gap check could never fail. A restart
inside the eligible window re-fired an already-completed cycle.

**Bounds drift.** `STRATEGY_BOUNDS` existed twice — 14 keys in
`scout/narrative/strategy.py`, 13 in a hand-maintained copy inside
`dashboard/api.py`. The endpoint therefore applied no bounds validation to
`counter_suppress_threshold` while `Strategy.set` and the learner both enforced
`(0, 100)`.

**Scope qualifier.** A bare `NO_CHANGE` reads as a claim about all 14 tunable
parameters. It is only ever a claim about the 3 the evaluator can reconstruct.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone


from scout.narrative.agent import (
    daily_learn_due,
    load_cadence_anchors,
    resolve_cadence_anchor,
    weekly_learn_due,
)
from scout.narrative.deterministic_learner import (
    SEARCHABLE,
    ProposalVerdict,
    evaluate,
)
from scout.narrative.strategy_bounds import STRATEGY_BOUNDS

EPOCH = datetime.min.replace(tzinfo=timezone.utc)
LEARN_HOUR = 1
WEEKLY_DAY = 6  # Sunday


class _FakeStrategy:
    """Mimics the two Strategy methods the anchor resolver uses.

    `get` raises KeyError for an unknown key; `get_timestamp` substitutes its
    own default and so cannot distinguish absent from epoch — which is exactly
    why the resolver has to probe with `get` first.
    """

    def __init__(self, values: dict[str, str] | None = None):
        self._v = dict(values or {})

    def get(self, key):
        if key not in self._v:
            raise KeyError(key)
        return self._v[key]

    def get_timestamp(self, key, default=None):
        raw = self._v.get(key)
        if not raw:
            return default if default is not None else datetime.min
        return datetime.fromisoformat(str(raw))

    def set_timestamp(self, key, value):
        self._v[key] = value.isoformat()


def _anchor(strategy):
    """The DAILY anchor as the loop itself loads it.

    Deliberately routed through `load_cadence_anchors` — the function the loop
    calls — rather than `resolve_cadence_anchor` directly. Testing the helper
    alone proved the helper worked while the loop still read the frozen key;
    only a source grep caught that, and a grep is a weak pin.
    """
    return load_cadence_anchors(strategy, EPOCH)[0]


def _weekly_anchor(strategy):
    return load_cadence_anchors(strategy, EPOCH)[1]


class TestRestartWithinTheWindowRunsExactlyOneCycle:
    """*** THE LOAD-BEARING REGRESSION TEST. ***"""

    async def test_a_restart_minutes_after_a_cycle_does_not_re_fire_it(self):
        # Prod values: the cycle that actually ran on 2026-08-06.
        fired_at = datetime(2026, 8, 6, 1, 12, 39, tzinfo=timezone.utc)
        strategy = _FakeStrategy(
            {
                # The frozen legacy key, exactly as prod held it.
                "last_daily_learn_at": "2026-08-05T01:01:12.107171+00:00",
            }
        )

        # --- tick 1: the cycle is due and runs, stamping the attempt key
        assert daily_learn_due(fired_at, _anchor(strategy), learn_hour=LEARN_HOUR)
        strategy.set_timestamp("last_daily_learn_attempt_at", fired_at)

        # --- restart 8 minutes later, still inside the 01:00 UTC hour
        restart_now = fired_at + timedelta(minutes=8)
        assert restart_now.hour == LEARN_HOUR, "restart must be inside the window"

        assert not daily_learn_due(
            restart_now, _anchor(strategy), learn_hour=LEARN_HOUR
        ), (
            "a restart inside the eligible hour re-fired an already-completed "
            "cycle: the anchor is not tracking the cycle that ran"
        )

    async def test_the_old_anchor_would_have_re_fired(self):
        """Pins WHY the fix is needed, not just that it works.

        Reading the legacy key — the pre-fix behaviour — the same restart is
        judged due, because that key stopped advancing.
        """
        fired_at = datetime(2026, 8, 6, 1, 12, 39, tzinfo=timezone.utc)
        frozen_legacy = datetime.fromisoformat("2026-08-05T01:01:12.107171+00:00")
        restart_now = fired_at + timedelta(minutes=8)

        assert daily_learn_due(restart_now, frozen_legacy, learn_hour=LEARN_HOUR), (
            "if this is False the frozen-anchor defect never existed and this "
            "whole fix is unmotivated"
        )

    async def test_the_next_day_still_fires(self):
        """The fix must not wedge the scheduler shut."""
        fired_at = datetime(2026, 8, 6, 1, 12, 39, tzinfo=timezone.utc)
        strategy = _FakeStrategy()
        strategy.set_timestamp("last_daily_learn_attempt_at", fired_at)

        tomorrow = fired_at + timedelta(days=1)
        assert daily_learn_due(tomorrow, _anchor(strategy), learn_hour=LEARN_HOUR)

    async def test_many_restarts_in_the_window_still_yield_one_cycle(self):
        """Simulates a crash-loop across the whole eligible hour."""
        strategy = _FakeStrategy(
            {"last_daily_learn_at": "2026-08-05T01:01:12.107171+00:00"}
        )
        start = datetime(2026, 8, 6, 1, 0, 0, tzinfo=timezone.utc)
        fires = 0
        for minute in range(60):  # a restart every minute of the 01:00 hour
            now = start + timedelta(minutes=minute)
            if daily_learn_due(now, _anchor(strategy), learn_hour=LEARN_HOUR):
                fires += 1
                strategy.set_timestamp("last_daily_learn_attempt_at", now)
        assert fires == 1, f"expected exactly one cycle in the window, got {fires}"

    async def test_weekly_restart_within_the_window_does_not_re_fire(self):
        # Sunday 02:00 UTC — weekday()==6, hour==(1+1)%24
        fired_at = datetime(2026, 8, 2, 2, 13, 34, tzinfo=timezone.utc)
        assert fired_at.weekday() == WEEKLY_DAY
        strategy = _FakeStrategy(
            {"last_weekly_learn_at": "2026-07-26T02:13:34.810277+00:00"}
        )

        assert weekly_learn_due(
            fired_at,
            _weekly_anchor(strategy),
            learn_hour=LEARN_HOUR,
            learn_day=WEEKLY_DAY,
        )
        strategy.set_timestamp("last_weekly_learn_attempt_at", fired_at)

        restart_now = fired_at + timedelta(minutes=20)
        assert not weekly_learn_due(
            restart_now,
            _weekly_anchor(strategy),
            learn_hour=LEARN_HOUR,
            learn_day=WEEKLY_DAY,
        )


class TestAnchorResolution:
    def test_the_attempt_stamp_wins_when_present(self):
        strategy = _FakeStrategy(
            {
                "last_daily_learn_attempt_at": "2026-08-06T01:12:39+00:00",
                "last_daily_learn_at": "2026-08-05T01:01:12+00:00",
            }
        )
        assert _anchor(strategy) == datetime(2026, 8, 6, 1, 12, 39, tzinfo=timezone.utc)

    def test_the_legacy_key_is_the_pre_cutover_fallback(self):
        """First run after upgrading: no attempt stamp exists yet, and the
        legacy key is the last true record of when a cycle ran. Falling
        straight to epoch would fire a redundant cycle on the first tick."""
        strategy = _FakeStrategy({"last_daily_learn_at": "2026-08-05T01:01:12+00:00"})
        assert _anchor(strategy) == datetime(2026, 8, 5, 1, 1, 12, tzinfo=timezone.utc)

    def test_a_stale_legacy_key_cannot_leak_back_in(self):
        """Once an attempt stamp exists the frozen key is never consulted,
        even though it keeps sitting in the table forever."""
        strategy = _FakeStrategy(
            {
                "last_daily_learn_attempt_at": "2026-08-06T01:12:39+00:00",
                "last_daily_learn_at": "2020-01-01T00:00:00+00:00",
            }
        )
        restart = datetime(2026, 8, 6, 1, 20, tzinfo=timezone.utc)
        assert not daily_learn_due(restart, _anchor(strategy), learn_hour=LEARN_HOUR)

    def test_neither_key_present_falls_back_to_the_default(self):
        assert _anchor(_FakeStrategy()) == EPOCH

    def test_an_empty_value_is_treated_as_absent(self):
        strategy = _FakeStrategy(
            {
                "last_daily_learn_attempt_at": "",
                "last_daily_learn_at": "2026-08-05T01:01:12+00:00",
            }
        )
        assert _anchor(strategy) == datetime(2026, 8, 5, 1, 1, 12, tzinfo=timezone.utc)

    def test_the_loop_loads_anchors_through_the_tested_path(self):
        """Wiring check, not the proof.

        The behavioural tests above go through `load_cadence_anchors`; this
        asserts the loop does too, so they cannot both pass while the loop
        reads something else. Kept deliberately narrow: one call site, one
        banned direct read.
        """
        from pathlib import Path

        import scout.narrative.agent as agent

        src = Path(agent.__file__).read_text("utf-8")
        assert "load_cadence_anchors(strategy, _epoch)" in src
        for banned in (
            'strategy.get_timestamp("last_daily_learn_at"',
            'strategy.get_timestamp("last_weekly_learn_at"',
        ):
            assert banned not in src, f"loop still reads the frozen key: {banned}"

    def test_both_anchors_come_back_from_one_call(self):
        strategy = _FakeStrategy(
            {
                "last_daily_learn_attempt_at": "2026-08-06T01:12:39+00:00",
                "last_weekly_learn_attempt_at": "2026-08-02T02:13:34+00:00",
            }
        )
        daily, weekly = load_cadence_anchors(strategy, EPOCH)
        assert daily == datetime(2026, 8, 6, 1, 12, 39, tzinfo=timezone.utc)
        assert weekly == datetime(2026, 8, 2, 2, 13, 34, tzinfo=timezone.utc)

    def test_the_helper_and_the_wiring_agree(self):
        """If these ever diverge, the behavioural tests are testing a function
        the loop does not use — the exact hole a source grep was papering over."""
        strategy = _FakeStrategy(
            {"last_daily_learn_attempt_at": "2026-08-06T01:12:39+00:00"}
        )
        assert load_cadence_anchors(strategy, EPOCH)[0] == resolve_cadence_anchor(
            strategy, "last_daily_learn_attempt_at", "last_daily_learn_at", EPOCH
        )


class TestOneAuthoritativeBoundsRegistry:
    def test_there_is_exactly_one_definition_in_the_tree(self):
        """A second literal copy is how the drift happened. Catch the next one."""
        import re
        from pathlib import Path

        root = Path(__file__).resolve().parents[1]
        definitions = []
        for path in list(root.glob("scout/**/*.py")) + list(
            root.glob("dashboard/**/*.py")
        ):
            text = path.read_text("utf-8", errors="ignore")
            # An assignment to a dict literal, not an import or a re-export.
            if re.search(r"^\s*STRATEGY_BOUNDS[^=\n]*=\s*\{", text, re.MULTILINE):
                definitions.append(str(path.relative_to(root)).replace("\\", "/"))
        assert definitions == ["scout/narrative/strategy_bounds.py"], (
            f"STRATEGY_BOUNDS is defined as a literal in {definitions}; "
            "there must be exactly one registry"
        )

    def test_every_consumer_gets_the_same_object(self):
        from dashboard.api import create_app  # noqa: F401  (import side effects)
        from scout.narrative.strategy import STRATEGY_BOUNDS as via_strategy
        from scout.narrative.strategy_bounds import STRATEGY_BOUNDS as registry

        assert via_strategy is registry, "strategy.py re-export drifted into a copy"

    def test_the_endpoint_now_bounds_the_previously_unbounded_key(self):
        """The exact gap: present for Strategy.set and the learner, absent from
        the dashboard copy, so manual PUTs to it were unvalidated."""
        assert "counter_suppress_threshold" in STRATEGY_BOUNDS
        assert STRATEGY_BOUNDS["counter_suppress_threshold"] == (0, 100)

    def test_the_registry_covers_fourteen_parameters(self):
        assert len(STRATEGY_BOUNDS) == 14

    def test_the_registry_module_stays_a_leaf(self):
        """It is imported by the dashboard; an import here would drag
        `scout.db` into the web process and recreate the copy-paste pressure."""
        import ast
        from pathlib import Path

        import scout.narrative.strategy_bounds as mod

        tree = ast.parse(Path(mod.__file__).read_text("utf-8"))
        imports = [
            n
            for n in ast.walk(tree)
            if isinstance(n, (ast.Import, ast.ImportFrom))
            and not (isinstance(n, ast.ImportFrom) and n.module == "__future__")
        ]
        assert imports == [], "strategy_bounds must import nothing"

    async def test_a_locked_out_of_bounds_value_is_refused_by_the_endpoint(
        self, tmp_path
    ):
        """End-to-end: the endpoint now rejects what it used to accept."""
        import aiosqlite
        from httpx import ASGITransport, AsyncClient

        from dashboard.api import create_app

        path = str(tmp_path / "s.db")
        conn = await aiosqlite.connect(path)
        await conn.execute(
            "CREATE TABLE agent_strategy (key TEXT PRIMARY KEY, value TEXT NOT NULL,"
            " locked INTEGER DEFAULT 0, updated_by TEXT, updated_at TEXT)"
        )
        await conn.execute(
            "INSERT INTO agent_strategy (key, value, locked, updated_by)"
            " VALUES ('counter_suppress_threshold', '65', 0, 'agent')"
        )
        await conn.commit()
        await conn.close()

        app = create_app(path)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as c:
            bad = await c.put(
                "/api/narrative/strategy/counter_suppress_threshold",
                json={"value": "5000"},
            )
            good = await c.put(
                "/api/narrative/strategy/counter_suppress_threshold",
                json={"value": "70"},
            )
        assert (
            bad.status_code == 400
        ), "the endpoint still accepts an out-of-bounds counter_suppress_threshold"
        assert good.status_code == 200 and good.json()["value"] == "70"


def _prod_shaped_params() -> dict[str, object]:
    """`agent_strategy` as prod actually holds it, not just the bounded keys.

    This distinction is load-bearing. The real table contains
    `narrative_fit_score_min` (60) and `laggard_min_mcap` (1_000_000) — both
    have a reconstruction predicate in SEARCHABLE but NO entry in the bounds
    registry, so both must be excluded from `searched_parameters`.

    A fixture built as `{k: 1 for k in STRATEGY_BOUNDS}` cannot see that: those
    two keys are absent, so the `k in current_params` test alone already
    excludes them and the `k in bounds` clause goes unexercised. Deleting that
    clause left the suite green while prod would have reported 5 searched
    parameters instead of 3.
    """
    params: dict[str, object] = {k: 1 for k in STRATEGY_BOUNDS}
    params["narrative_fit_score_min"] = 60
    params["laggard_min_mcap"] = 1_000_000
    # Non-numeric rows the real table also carries.
    params["lessons_learned"] = ""
    params["user_alert_mode"] = "all"
    return params


class TestTheVerdictCarriesItsScope:
    def test_the_no_change_value_states_its_search_space(self):
        assert (
            ProposalVerdict.NO_CHANGE.value == "NO_CHANGE_WITHIN_VERIFIED_SEARCH_SPACE"
        )

    def test_scope_counts_are_reported_on_every_outcome(self):
        from test_deterministic_learner import BOUNDS, CURRENT, _population

        p = evaluate(_population(300, "1.0"), CURRENT, BOUNDS)
        fields = p.log_fields()
        assert "searched_parameters" in fields
        assert "unidentifiable_parameters" in fields
        assert fields["searched_parameters"] == len(p.searched_parameters)
        assert fields["unidentifiable_parameters"] == len(p.unidentifiable_parameters)

    def test_the_counts_partition_the_registry(self):
        """Against the REAL registry: 3 searched, 11 unidentifiable, 14 total."""
        p = evaluate([], _prod_shaped_params(), STRATEGY_BOUNDS)

        searched = set(p.searched_parameters)
        unidentifiable = set(p.unidentifiable_parameters)

        assert searched == {
            "counter_suppress_threshold",
            "laggard_max_mcap",
            "min_trigger_count",
        }
        assert len(searched) == 3
        assert len(unidentifiable) == 11
        assert searched | unidentifiable == set(STRATEGY_BOUNDS)
        assert not (searched & unidentifiable)

    def test_unidentifiable_means_no_reconstruction_predicate(self):
        p = evaluate([], _prod_shaped_params(), STRATEGY_BOUNDS)
        for key in p.unidentifiable_parameters:
            assert key not in SEARCHABLE

    def test_scope_is_reported_even_on_an_insufficient_sample(self):
        """The early return must still say what it would have covered."""
        p = evaluate([], _prod_shaped_params(), STRATEGY_BOUNDS)
        assert p.verdict is ProposalVerdict.INSUFFICIENT_EVIDENCE
        assert len(p.searched_parameters) == 3

    def test_the_durable_record_carries_the_names(self):
        rec = evaluate([], _prod_shaped_params(), STRATEGY_BOUNDS).as_record()
        assert rec["searched_parameters"] == [
            "counter_suppress_threshold",
            "laggard_max_mcap",
            "min_trigger_count",
        ]
        assert len(rec["unidentifiable_parameters"]) == 11
