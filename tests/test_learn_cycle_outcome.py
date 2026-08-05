"""One outcome vocabulary for both learn cadences, and a failure path that talks.

Two defects are pinned here.

**Ambiguous outcomes.** The daily and weekly schedulers each had their own
result shape, and the daily one was read by asking ``is it None?``, ``is_success?``,
``is_skipped?`` — with the failure case reached by elimination. An outcome
derived by elimination is indistinguishable from an outcome nobody set, which is
how ~34 days of hard provider failures read as quiet no-ops.

**Silent failures.** The deterministic learner's failure path logged
``logger.exception`` with no fields at all, and ``structlog``'s JSONRenderer is
configured without ``format_exc_info`` — so the error event carried literally no
diagnostic content. These tests require the full secret-safe field set on the
failure path, and require it to be *returned*, not raised.
"""

from __future__ import annotations

import json

import aiosqlite
import pytest

from scout.narrative.deterministic_learner import (
    _VERDICT_TO_CYCLE_OUTCOME,
    LearnProposal,
    ProposalVerdict,
    evaluate,
    run_deterministic_daily_learn,
)
from scout.narrative.learn_outcome import (
    CYCLE_REPORTING,
    LearnCycleOutcome,
    redact,
    safe_traceback,
)

# The seven the delivery specified. OPTIONAL_COMMENTARY_SUCCESS is a documented
# addition, asserted separately so this list stays a faithful record of the ask.
REQUIRED_MEMBERS = {
    "DETERMINISTIC_NO_CHANGE",
    "DETERMINISTIC_PROPOSAL",
    "INSUFFICIENT_EVIDENCE",
    "UNSTABLE_EVIDENCE",
    "FAILED",
    "OPTIONAL_COMMENTARY_DISABLED",
    "OPTIONAL_COMMENTARY_FAILED",
}

# Every field the failure path must emit.
REQUIRED_FAILURE_FIELDS = {
    "outcome",
    "exception_type",
    "exception_message",
    "failing_step",
    "algorithm_version",
    "snapshot_hash",
    "prediction_count",
    "elapsed_ms",
    "correlation_id",
}


class _Db:
    def __init__(self, conn):
        self._conn = conn


class _Strategy:
    def __init__(self):
        self.sets: list[tuple] = []
        self.timestamps: list[tuple] = []

    async def set(self, *a, **kw):
        self.sets.append((a, kw))

    async def set_timestamp(self, *a, **kw):
        self.timestamps.append((a, kw))


async def _seed(tmp_path, n=400):
    path = str(tmp_path / "c.db")
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


class TestOneVocabularyForBothCadences:
    def test_the_required_members_all_exist(self):
        assert REQUIRED_MEMBERS <= {m.name for m in LearnCycleOutcome}

    def test_the_only_extra_member_is_the_documented_commentary_success(self):
        """Guards against the enum quietly growing.

        A scheduler vocabulary that accretes members stops being a vocabulary.
        """
        extra = {m.name for m in LearnCycleOutcome} - REQUIRED_MEMBERS
        assert extra == {"OPTIONAL_COMMENTARY_SUCCESS"}

    def test_every_internal_verdict_maps_explicitly(self):
        """Totality. A new verdict with no mapping fails HERE, not in prod."""
        assert set(_VERDICT_TO_CYCLE_OUTCOME) == set(ProposalVerdict)

    def test_the_mapping_is_injective(self):
        """Two verdicts collapsing onto one outcome would hide a distinction the
        split was made to expose."""
        outcomes = list(_VERDICT_TO_CYCLE_OUTCOME.values())
        assert len(outcomes) == len(set(outcomes))

    def test_every_outcome_has_reporting(self):
        assert set(CYCLE_REPORTING) == set(LearnCycleOutcome)

    def test_every_reported_level_is_a_real_logger_method(self):
        """The agent dispatches with `getattr(logger, reporting.level)`.

        A typo in the table would raise AttributeError at the moment of
        reporting — i.e. only on the outcome that carries the typo, which for
        `FAILED` means the failure log itself is what breaks.
        """
        import structlog

        log = structlog.get_logger(__name__)
        for outcome, reporting in CYCLE_REPORTING.items():
            assert callable(getattr(log, reporting.level, None)), (
                f"{outcome.name} reports at level {reporting.level!r}, "
                "which is not a logger method"
            )

    def test_only_genuine_failures_are_logged_at_error(self):
        """Error level is a paging signal. A disabled optional feature and a
        learner that concluded "no change" must not reach it."""
        at_error = {o for o, r in CYCLE_REPORTING.items() if r.level == "error"}
        assert at_error == {
            LearnCycleOutcome.FAILED,
            LearnCycleOutcome.OPTIONAL_COMMENTARY_FAILED,
        }

    def test_lookup_raises_rather_than_defaulting(self):
        """`.get(x, default)` would let an unmapped verdict inherit a plausible
        outcome. The lookup must be a hard failure."""

        class _Rogue(LearnProposal):
            pass

        p = _Rogue(verdict=ProposalVerdict.NO_CHANGE)
        p.verdict = "NOT_A_VERDICT"  # type: ignore[assignment]
        with pytest.raises(KeyError):
            _ = p.cycle_outcome

    def test_commentary_outcomes_are_never_learning_successes(self):
        for member in LearnCycleOutcome:
            if member.name.startswith("OPTIONAL_COMMENTARY"):
                assert member.is_learning_success is False
                assert member.is_critical_to_learning is False

    def test_no_change_counts_as_a_learning_success(self):
        """The learner ran and concluded correctly. Treating NO_CHANGE as a
        non-success would make a healthy learner look chronically broken."""
        assert LearnCycleOutcome.DETERMINISTIC_NO_CHANGE.is_learning_success
        assert LearnCycleOutcome.DETERMINISTIC_PROPOSAL.is_learning_success
        assert LearnCycleOutcome.FAILED.is_learning_success is False
        assert LearnCycleOutcome.INSUFFICIENT_EVIDENCE.is_learning_success is False
        assert LearnCycleOutcome.UNSTABLE_EVIDENCE.is_learning_success is False


class TestTheAgentInfersNothing:
    def test_the_daily_block_has_no_none_or_truthiness_outcome_test(self):
        """The exact shapes that made failure the else-branch."""
        from pathlib import Path

        import scout.narrative.agent as agent

        src = Path(agent.__file__).read_text("utf-8")
        start = src.index("LEARN daily (gated by hour + 23h gap)")
        block = src[start : src.index("# Paper trading daily digest", start)]
        for banned in (
            "learn_result is not None",
            "learn_result.is_success",
            "learn_result.is_skipped",
            "FAILED_INTERNAL",
        ):
            assert banned not in block, f"outcome still inferred via {banned!r}"
        # the outcome is read from the result and dispatched through the table
        assert "learn_result.cycle_outcome" in block
        assert "CYCLE_REPORTING[" in block

    def test_a_prune_error_is_not_reported_as_a_learn_failure(self):
        """Pruning ran inside the learn try-block, so a prune error logged a
        learn failure — the same category error one layer down."""
        from pathlib import Path

        import scout.narrative.agent as agent

        src = Path(agent.__file__).read_text("utf-8")
        assert "narrative.daily_prune_error" in src
        start = src.index("LEARN daily (gated by hour + 23h gap)")
        block = src[start : src.index("# Paper trading daily digest", start)]
        # the learn call and its reporting complete BEFORE the prune try-block
        assert block.index("CYCLE_REPORTING[") < block.index("prune_old_snapshots(")


class TestUnstableIsNotInsufficient:
    def test_they_are_distinct_members(self):
        assert (
            LearnCycleOutcome.UNSTABLE_EVIDENCE
            is not LearnCycleOutcome.INSUFFICIENT_EVIDENCE
        )
        assert (
            _VERDICT_TO_CYCLE_OUTCOME[ProposalVerdict.UNSTABLE_EVIDENCE]
            is LearnCycleOutcome.UNSTABLE_EVIDENCE
        )

    def test_an_empty_search_space_is_insufficient_not_no_change(self):
        """Every searchable key locked: nothing was evaluated, so reporting
        NO_CHANGE ("no candidate beat the current configuration") would be a
        claim about a search that never ran."""
        from test_deterministic_learner import BOUNDS, CURRENT, _population

        p = evaluate(
            _population(300, "1.0"), CURRENT, BOUNDS, locked_keys=list(CURRENT)
        )
        assert p.verdict is ProposalVerdict.INSUFFICIENT_EVIDENCE
        assert p.cycle_outcome is LearnCycleOutcome.INSUFFICIENT_EVIDENCE
        assert p.candidates == []


class TestTheFailurePathTalks:
    """*** THE LOAD-BEARING FAILURE TESTS. ***

    Failure injected at each real stage of the runtime path.
    """

    @pytest.mark.parametrize(
        "step,patch_target",
        [
            ("load_records", "scout.narrative.deterministic_learner.load_records"),
            ("evaluate", "scout.narrative.deterministic_learner.evaluate"),
        ],
    )
    async def test_it_returns_a_failed_result_with_every_required_field(
        self, tmp_path, monkeypatch, step, patch_target
    ):
        boom = RuntimeError("synthetic failure at " + step)

        def _raise(*a, **kw):
            raise boom

        monkeypatch.setattr(patch_target, _raise)
        conn = await _seed(tmp_path)
        try:
            result = await run_deterministic_daily_learn(_Db(conn), _Strategy())
        finally:
            await conn.close()

        assert result is not None, "the runtime path must never return None"
        assert result.verdict is ProposalVerdict.FAILED
        assert result.cycle_outcome is LearnCycleOutcome.FAILED

        fields = result.log_fields()
        missing = REQUIRED_FAILURE_FIELDS - set(fields)
        assert not missing, f"failure event omits {sorted(missing)}"
        assert fields["outcome"] == "FAILED"
        assert fields["exception_type"] == "RuntimeError"
        assert step in fields["exception_message"]
        assert fields["failing_step"] == step
        assert fields["algorithm_version"] == "deterministic-v1"
        assert fields["correlation_id"]
        assert isinstance(fields["elapsed_ms"], int)
        assert fields["traceback"] and "RuntimeError" in fields["traceback"]

    async def test_it_never_raises_even_with_no_connection(self):
        class _NoConn:
            _conn = None

        result = await run_deterministic_daily_learn(_NoConn(), _Strategy())
        assert result.cycle_outcome is LearnCycleOutcome.FAILED
        assert result.failing_step == "acquire_connection"
        # nothing to hash or count, but the keys are still present
        assert result.log_fields()["snapshot_hash"] is None
        assert result.log_fields()["prediction_count"] == 0

    async def test_a_load_failure_still_reports_the_population_it_reached(
        self, tmp_path, monkeypatch
    ):
        """`prediction_count` and `snapshot_hash` carry whatever was actually
        loaded, so a failure downstream of loading is still attributable."""
        conn = await _seed(tmp_path)
        monkeypatch.setattr(
            "scout.narrative.deterministic_learner.evaluate",
            lambda *a, **kw: (_ for _ in ()).throw(ValueError("late boom")),
        )
        try:
            result = await run_deterministic_daily_learn(_Db(conn), _Strategy())
        finally:
            await conn.close()
        assert result.failing_step == "evaluate"
        assert result.log_fields()["prediction_count"] == 400
        assert result.log_fields()["snapshot_hash"]

    async def test_the_failure_is_persisted_as_a_learn_log_row(
        self, tmp_path, monkeypatch
    ):
        """A cycle that failed must leave a row saying so. Otherwise
        `learn_logs` silently skips the failure and the gap looks like a
        scheduler that never fired."""
        conn = await _seed(tmp_path)
        monkeypatch.setattr(
            "scout.narrative.deterministic_learner.evaluate",
            lambda *a, **kw: (_ for _ in ()).throw(ValueError("late boom")),
        )
        try:
            await run_deterministic_daily_learn(_Db(conn), _Strategy())
            cur = await conn.execute(
                "SELECT cycle_type, reflection_text FROM learn_logs"
            )
            rows = await cur.fetchall()
        finally:
            await conn.close()
        assert len(rows) == 1
        payload = json.loads(rows[0][1])
        assert payload["evaluation"]["verdict"] == "FAILED"
        assert payload["runtime"]["failing_step"] == "evaluate"

    async def test_a_failed_cycle_mutates_no_parameter(self, tmp_path, monkeypatch):
        conn = await _seed(tmp_path)
        strategy = _Strategy()
        monkeypatch.setattr(
            "scout.narrative.deterministic_learner.evaluate",
            lambda *a, **kw: (_ for _ in ()).throw(ValueError("boom")),
        )
        try:
            await run_deterministic_daily_learn(_Db(conn), strategy)
            cur = await conn.execute("SELECT key, value, locked FROM agent_strategy")
            after = await cur.fetchall()
        finally:
            await conn.close()
        assert strategy.sets == []
        assert {tuple(r) for r in after} == {
            ("counter_suppress_threshold", "65", 0),
            ("laggard_max_mcap", "200000000", 0),
            ("min_trigger_count", "1", 0),
        }


class TestFailureTelemetryIsSecretSafe:
    def test_an_api_key_in_the_message_is_redacted(self):
        exc = RuntimeError("boom with sk-ant-api03-DEADBEEFdeadbeef0123 inside")
        p = LearnProposal.failed(exc, failing_step="evaluate")
        emitted = p.log_fields()["exception_message"]
        assert "sk-ant-api03-DEADBEEFdeadbeef0123" not in emitted
        assert "sk-ant-<redacted>" in emitted

    def test_an_api_key_in_the_traceback_is_redacted(self):
        try:
            raise RuntimeError("x-api-key: sk-ant-api03-SECRETSECRETSECRET")
        except RuntimeError as exc:
            rendered = safe_traceback(exc)
        assert rendered is not None
        assert "SECRETSECRETSECRET" not in rendered

    def test_an_authorization_header_is_redacted(self):
        """`Bearer <token>` is the standard shape, and the original pattern
        stopped at the scheme — redacting the word "Bearer" and logging the
        token."""
        assert "hunter2" not in (redact("Authorization: Bearer hunter2") or "")
        assert "hunter2" not in (redact("x-api-key = Bearer hunter2") or "")

    def test_redaction_does_not_eat_following_lines(self):
        """Bounded to the line: a greedy pattern would swallow the rest of a
        multi-line traceback and destroy the diagnostic content."""
        out = redact('Authorization: Bearer hunter2\nFile "agent.py", line 12')
        assert "agent.py" in out

    def test_the_message_is_length_bounded(self):
        p = LearnProposal.failed(RuntimeError("x" * 5000), failing_step="evaluate")
        assert len(p.log_fields()["exception_message"]) <= 500

    def test_the_traceback_is_length_bounded(self):
        def _deep(n):
            if n == 0:
                raise RuntimeError("bottom")
            _deep(n - 1)

        try:
            _deep(60)
        except RuntimeError as exc:
            rendered = safe_traceback(exc)
        assert rendered is not None and len(rendered) <= 4100

    def test_no_prompt_or_provider_payload_field_exists(self):
        """The deterministic learner handles neither, and the field set must not
        acquire a place to put them."""
        p = LearnProposal.failed(RuntimeError("boom"), failing_step="evaluate")
        banned = {"prompt", "messages", "request_body", "response_body", "api_key"}
        assert banned & set(p.log_fields()) == set()
        assert banned & set(p.runtime_envelope()) == set()


class TestDeterminismSurvivesTheRuntimeEnvelope:
    async def test_the_evaluation_subtree_is_identical_across_runs(self, tmp_path):
        """`correlation_id` and `elapsed_ms` vary by design; the evaluation
        record must not, or `snapshot_sha256` stops meaning anything."""
        conn = await _seed(tmp_path)
        try:
            first = await run_deterministic_daily_learn(_Db(conn), _Strategy())
            second = await run_deterministic_daily_learn(_Db(conn), _Strategy())
        finally:
            await conn.close()
        assert first.as_record() == second.as_record()
        assert first.correlation_id != second.correlation_id
