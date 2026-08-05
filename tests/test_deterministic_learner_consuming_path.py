"""The RUNTIME path: scheduled learning must survive a dead provider.

The previous architecture put an Anthropic call on the only learning path. When
the account's credits ran out it raised ``BadRequestError 400`` on every cycle
and learning stopped for ~34 days while the scheduler logged success.

These tests exercise the function the scheduler actually calls, with the
Anthropic SDK rigged to raise the exact captured billing error.
"""

from __future__ import annotations

import json

import aiosqlite
import pytest

from scout.narrative.deterministic_learner import (
    ProposalVerdict,
    run_deterministic_daily_learn,
)

# Verbatim from the production probe (request_id req_011CdkExUMSPnAoHAdFNkKUs).
BILLING_ERROR = (
    "Error code: 400 - {'type': 'error', 'error': {'type': 'invalid_request_error', "
    "'message': 'Your credit balance is too low to access the Anthropic API. "
    "Please go to Plans & Billing to upgrade or purchase credits.'}}"
)


class _ExplodingAnthropic:
    """Stands in for a provider whose account is dead."""

    def __init__(self, *a, **kw):
        raise RuntimeError(BILLING_ERROR)


class _Db:
    def __init__(self, conn):
        self._conn = conn


class _Strategy:
    """Records every mutation attempt so a test can assert none happened."""

    def __init__(self):
        self.sets: list[tuple] = []
        self.timestamps: list[tuple] = []

    async def set(self, *a, **kw):
        self.sets.append((a, kw))

    async def set_timestamp(self, *a, **kw):
        self.timestamps.append((a, kw))


async def _seed(tmp_path, n=400):
    """A schema-faithful predictions + agent_strategy + learn_logs fixture."""
    path = str(tmp_path / "d.db")
    conn = await aiosqlite.connect(path)
    conn.row_factory = aiosqlite.Row
    await conn.execute("""CREATE TABLE predictions (
             id INTEGER PRIMARY KEY, predicted_at TEXT, created_at TEXT,
             outcome_class TEXT, outcome_48h_change_pct REAL, peak_change_pct REAL,
             narrative_fit_score INTEGER, counter_risk_score INTEGER,
             market_cap_at_prediction REAL, trigger_count INTEGER,
             is_control INTEGER DEFAULT 0)""")
    await conn.execute("""CREATE TABLE agent_strategy (
             key TEXT PRIMARY KEY, value TEXT, locked INTEGER DEFAULT 0)""")
    await conn.execute("""CREATE TABLE learn_logs (
             id INTEGER PRIMARY KEY AUTOINCREMENT, cycle_number INTEGER,
             cycle_type TEXT, reflection_text TEXT, changes_made TEXT,
             hit_rate_before REAL, hit_rate_after REAL,
             created_at TEXT DEFAULT (datetime('now')))""")
    for i in range(n):
        await conn.execute(
            "INSERT INTO predictions (predicted_at, outcome_class, "
            "outcome_48h_change_pct, narrative_fit_score, counter_risk_score, "
            "market_cap_at_prediction, trigger_count, is_control) "
            "VALUES (?,?,?,?,?,?,?,0)",
            (
                f"2026-05-{(i % 28) + 1:02d}T00:00:00+00:00",
                "HIT" if i % 9 == 0 else "NEUTRAL",
                (1.5 if i % 9 == 0 else -0.4),
                50 + (i % 40),
                20 + (i % 60),
                2_000_000 + (i * 1000),
                1,
            ),
        )
    for k, v in (
        ("counter_suppress_threshold", "65"),
        ("laggard_max_mcap", "200000000"),
        ("min_trigger_count", "1"),
    ):
        await conn.execute(
            "INSERT INTO agent_strategy (key, value, locked) VALUES (?,?,0)", (k, v)
        )
    await conn.commit()
    return conn


class TestProviderDeathDoesNotStopLearning:
    async def test_learning_completes_while_anthropic_raises_billing_error(
        self, tmp_path, monkeypatch
    ):
        """*** THE LOAD-BEARING TEST. ***

        Rig the Anthropic SDK so ANY client construction raises the captured
        production billing error, then run the exact function the scheduler
        calls. It must still complete with a real verdict.
        """
        import anthropic

        monkeypatch.setattr(anthropic, "Anthropic", _ExplodingAnthropic)

        conn = await _seed(tmp_path)
        try:
            result = await run_deterministic_daily_learn(_Db(conn), _Strategy())
            assert result.verdict in set(ProposalVerdict)
            assert result.is_success or result.is_skipped
            assert result.population_size > 0
            assert result.snapshot_sha256
        finally:
            await conn.close()

    async def test_the_runtime_path_never_constructs_a_provider_client(
        self, tmp_path, monkeypatch
    ):
        """Stronger than "it survives": it must never TRY.

        A path that catches provider errors still has a provider dependency —
        latency, rate limits and outages all still reach it. This asserts the
        client is never constructed at all.
        """
        import anthropic

        constructed: list[int] = []

        class _Tripwire:
            def __init__(self, *a, **kw):
                constructed.append(1)

        monkeypatch.setattr(anthropic, "Anthropic", _Tripwire)

        conn = await _seed(tmp_path)
        try:
            await run_deterministic_daily_learn(_Db(conn), _Strategy())
            assert constructed == [], "runtime learner constructed a provider client"
        finally:
            await conn.close()


class TestNoParameterMutation:
    async def test_the_runtime_learner_sets_no_parameter(self, tmp_path):
        """It writes a provenance row, never a parameter."""
        conn = await _seed(tmp_path)
        strat = _Strategy()
        try:
            await run_deterministic_daily_learn(_Db(conn), strat)
            assert strat.sets == [], "runtime learner mutated a strategy parameter"
            cur = await conn.execute(
                "SELECT key, value, locked FROM agent_strategy ORDER BY key"
            )
            after = [tuple(r) for r in await cur.fetchall()]
            assert after == [
                ("counter_suppress_threshold", "65", 0),
                ("laggard_max_mcap", "200000000", 0),
                ("min_trigger_count", "1", 0),
            ]
        finally:
            await conn.close()

    async def test_it_persists_a_durable_provenance_record(self, tmp_path):
        conn = await _seed(tmp_path)
        try:
            result = await run_deterministic_daily_learn(_Db(conn), _Strategy())
            cur = await conn.execute(
                "SELECT cycle_type, reflection_text FROM learn_logs"
            )
            rows = await cur.fetchall()
            assert len(rows) == 1
            assert rows[0]["cycle_type"] == "daily_deterministic"
            rec = json.loads(rows[0]["reflection_text"])
            # Provenance the owner can audit a proposal against.
            for required in (
                "snapshot_sha256",
                "algorithm_version",
                "baseline",
                "candidates",
                "rejections",
                "verdict",
            ):
                assert required in rec, f"provenance record missing {required}"
            assert rec["verdict"] == result.verdict.value
        finally:
            await conn.close()

    async def test_locked_parameters_are_skipped_in_the_runtime_path(self, tmp_path):
        conn = await _seed(tmp_path)
        await conn.execute("UPDATE agent_strategy SET locked = 1")
        await conn.commit()
        try:
            result = await run_deterministic_daily_learn(_Db(conn), _Strategy())
            assert result.proposed == []
            assert any("locked" in n for n in result.notes)
        finally:
            await conn.close()


class TestSchedulerTelemetry:
    def test_the_agent_no_longer_calls_the_anthropic_daily_learner(self):
        """*** THE OVERSTATEMENT GUARD. ***

        A grep of the new module proves nothing about the runtime path. This
        asserts the SCHEDULER stopped calling the provider-dependent learner and
        calls the deterministic one instead.
        """
        from pathlib import Path

        import scout.narrative.agent as agent

        src = Path(agent.__file__).read_text("utf-8")
        assert "run_deterministic_daily_learn(db, strategy)" in src
        assert (
            "await daily_learn(" not in src
        ), "scheduler still invokes the Anthropic-dependent daily_learn"

    def test_no_unconditional_success_timestamp_or_event(self):
        """The two writes that made 34 days of failure look like success."""
        from pathlib import Path

        import scout.narrative.agent as agent

        src = Path(agent.__file__).read_text("utf-8")
        assert 'set_timestamp("last_daily_learn_at", now)' not in src
        assert 'logger.info("narrative.daily_learn_complete")' not in src
        # and the replacements exist
        assert "last_daily_learn_attempt_at" in src
        assert "last_daily_learn_success_at" in src
        assert "last_daily_learn_failure_at" in src
        assert "narrative.daily_learn_failed" in src


class TestWeeklyRequiredPathIsAnthropicFree:
    """The weekly path was the last required Anthropic caller.

    `weekly_consolidate` is commentary only — it rewrites `lessons_learned`
    prose and calls no `Strategy.set`, controlling none of the 14 parameters.
    So it is DISABLED rather than rebuilt deterministically: there is no
    parameter logic to preserve.
    """

    def test_weekly_commentary_is_disabled_by_default(self):
        from scout.config import Settings

        s = Settings(
            TELEGRAM_BOT_TOKEN="t", TELEGRAM_CHAT_ID="c", ANTHROPIC_API_KEY="k"
        )
        assert s.NARRATIVE_WEEKLY_COMMENTARY_ENABLED is False

    def test_the_scheduler_gates_weekly_behind_the_flag(self):
        """With the flag off, `weekly_consolidate` is unreachable, so no
        Anthropic client can be constructed on the required weekly path."""
        from pathlib import Path

        import scout.narrative.agent as agent

        src = Path(agent.__file__).read_text("utf-8")
        gate = "if not settings.NARRATIVE_WEEKLY_COMMENTARY_ENABLED:"
        assert gate in src
        # the provider call sits in the else-branch, after the gate
        assert src.index(gate) < src.index("await weekly_consolidate(")

    def test_disabled_commentary_is_not_a_learner_failure(self):
        """*** ABSENT COMMENTARY MUST NOT LOOK LIKE BROKEN LEARNING. ***

        The skip emits OPTIONAL_COMMENTARY_DISABLED at info with
        critical_to_learning=False, and must NOT write a failure timestamp.
        """
        from pathlib import Path

        import scout.narrative.agent as agent

        src = Path(agent.__file__).read_text("utf-8")
        block = src[src.index("if not settings.NARRATIVE_WEEKLY_COMMENTARY_ENABLED:") :]
        block = block[: block.index("else:")]
        assert "OPTIONAL_COMMENTARY_DISABLED" in block
        assert "critical_to_learning=False" in block
        assert "last_weekly_learn_failure_at" not in block
        assert "narrative.weekly_learn_skipped" in block

    def test_no_unconditional_weekly_success_timestamp_or_event(self):
        """Same false-success defect the daily path had."""
        from pathlib import Path

        import scout.narrative.agent as agent

        src = Path(agent.__file__).read_text("utf-8")
        assert 'set_timestamp("last_weekly_learn_at", now)' not in src
        assert 'logger.info("narrative.weekly_learn_complete")' not in src
        for required in (
            "last_weekly_learn_attempt_at",
            "last_weekly_learn_success_at",
            "last_weekly_learn_failure_at",
        ):
            assert required in src

    def test_commentary_failure_does_not_claim_learner_failure(self):
        """If commentary is ever re-enabled and fails, it is tagged
        OPTIONAL_COMMENTARY_FAILED with critical_to_learning=False — it must not
        be reported as deterministic learning failing."""
        from pathlib import Path

        import scout.narrative.agent as agent

        src = Path(agent.__file__).read_text("utf-8")
        assert "OPTIONAL_COMMENTARY_FAILED" in src
        idx = src.index("OPTIONAL_COMMENTARY_FAILED")
        assert "critical_to_learning=False" in src[idx : idx + 200]


class TestLongHoldCannotEnterSignalDispatch:
    def test_long_hold_is_not_a_dispatchable_signal_type(self):
        """`long_hold` is the residual 30% label after partial take-profit, not a
        signal. Its absence from the registry is intentional, not a gap."""
        from scout.trading.params import DEFAULT_SIGNAL_TYPES

        assert "long_hold" not in DEFAULT_SIGNAL_TYPES
