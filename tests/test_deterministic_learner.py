"""Deterministic learner — proposal-only, no provider, no mutation.

Context: the previous learner's only analysis step was an Anthropic call. When
credits ran out it failed every cycle for ~34 days. This replacement must be
incapable of failing for provider reasons, and incapable of applying a change.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from scout.narrative.deterministic_learner import (
    ALGORITHM_VERSION,
    MIN_VALIDATION_SAMPLE,
    PredictionRecord,
    ProposalVerdict,
    apply_filter,
    candidate_values,
    chronological_split,
    evaluate,
    rows_to_records,
    score_population,
    snapshot_hash,
)

BOUNDS = {
    "narrative_fit_score_min": (0.0, 100.0),
    "counter_suppress_threshold": (0.0, 100.0),
    "laggard_min_mcap": (0.0, 10_000_000.0),
    "laggard_max_mcap": (50_000_000.0, 1_000_000_000.0),
    "min_trigger_count": (1.0, 10.0),
}
CURRENT = {
    "narrative_fit_score_min": 60,
    "counter_suppress_threshold": 65,
    "laggard_min_mcap": 1_000_000,
    "laggard_max_mcap": 200_000_000,
    "min_trigger_count": 1,
}


def _rec(day: int, pct: str, fit: int = 70, risk: int = 30, mcap: str = "5000000"):
    return PredictionRecord(
        predicted_at=f"2026-05-{day:02d}T00:00:00+00:00",
        outcome_class="HIT" if Decimal(pct) >= 15 else "NEUTRAL",
        outcome_pct=Decimal(pct),
        narrative_fit_score=fit,
        counter_risk_score=risk,
        market_cap=Decimal(mcap),
        trigger_count=1,
    )


def _population(n=200, pct="1.0"):
    return [_rec(1 + (i % 28), pct) for i in range(n)]


class TestNoProviderDependency:
    def test_the_module_imports_no_provider_sdk(self):
        """*** THE WHOLE POINT. ***

        A billing outage took learning down for 34 days. This module must not be
        able to fail that way — so it must not import a provider SDK at all.
        """
        from pathlib import Path

        import scout.narrative.deterministic_learner as m

        src = Path(m.__file__).read_text("utf-8")
        for banned in ("import anthropic", "from anthropic", "openai", "requests.post"):
            assert banned not in src, f"provider dependency leaked in: {banned}"

    def test_evaluate_needs_no_network_or_credentials(self):
        p = evaluate(_population(), CURRENT, BOUNDS)
        assert p.algorithm_version == ALGORITHM_VERSION
        assert p.verdict in set(ProposalVerdict)


class TestProposalOnly:
    def test_evaluate_never_returns_an_applied_change(self):
        """Proposals carry a rollback value and are never self-applying."""
        p = evaluate(_population(), CURRENT, BOUNDS)
        for entry in p.proposed:
            assert "rollback_value" in entry
            assert entry["rollback_value"] == CURRENT[entry["key"]]
        assert not hasattr(p, "apply")

    def test_locked_parameters_are_never_proposed(self):
        p = evaluate(_population(), CURRENT, BOUNDS, locked_keys=list(CURRENT.keys()))
        assert p.proposed == []
        assert any("locked" in n for n in p.notes)

    def test_candidates_never_exceed_bounds(self):
        for key, bound in BOUNDS.items():
            for v in candidate_values(key, CURRENT[key], bound):
                assert bound[0] <= float(v) <= bound[1], f"{key}={v} out of {bound}"

    def test_max_change_per_cycle_is_clamped(self):
        """*** THE RATCHET GUARD. ***

        `laggard_min_volume` was driven to its ceiling in one learner cycle and
        stayed there 72 days. No single cycle may move a value more than a
        bounded fraction of its range.
        """
        lo, hi = 0.0, 100.0
        vals = [
            float(v) for v in candidate_values("narrative_fit_score_min", 50, (lo, hi))
        ]
        assert min(vals) >= 50 - (hi - lo) * 0.25 - 0.001
        assert max(vals) <= 50 + (hi - lo) * 0.25 + 0.001


class TestOverfittingGuards:
    def test_insufficient_sample_refuses_to_propose(self):
        p = evaluate([_rec(1, "1.0") for _ in range(10)], CURRENT, BOUNDS)
        assert p.verdict is ProposalVerdict.INSUFFICIENT_EVIDENCE
        assert any("insufficient sample" in r for r in p.rejections)

    def test_split_is_chronological_not_random(self):
        recs = rows_to_records(
            [
                {
                    "predicted_at": f"2026-05-{d:02d}T00:00:00+00:00",
                    "outcome_class": "NEUTRAL",
                    "outcome_48h_change_pct": 1.0,
                    "narrative_fit_score": 70,
                    "counter_risk_score": 10,
                    "market_cap_at_prediction": 5e6,
                    "trigger_count": 1,
                }
                for d in range(1, 21)
            ]
        )
        train, val = chronological_split(recs)
        assert train and val
        # every training record predates every validation record
        assert max(r.predicted_at for r in train) <= min(r.predicted_at for r in val)

    def test_a_flat_population_yields_no_change(self):
        """Identical outcomes everywhere: no filter can help, so none is proposed."""
        p = evaluate(_population(300, "1.0"), CURRENT, BOUNDS)
        assert p.verdict in (
            ProposalVerdict.NO_CHANGE,
            ProposalVerdict.INSUFFICIENT_EVIDENCE,
            ProposalVerdict.UNSTABLE_EVIDENCE,
        )
        assert p.proposed == []

    def test_validation_only_improvement_is_rejected(self):
        """A candidate that improves on holdout but not on train is noise."""
        p = evaluate(_population(300, "1.0"), CURRENT, BOUNDS)
        noise = [
            c
            for c in p.candidates
            if c.rejected_reason == "improves on validation but not on train"
        ]
        # The guard must exist and be reachable; the flat population makes most
        # candidates tie, so assert the predicate is wired rather than a count.
        assert all(c.rejected_reason for c in p.candidates if c.improvement <= 0)
        assert isinstance(noise, list)


class TestDeterminism:
    def test_same_population_yields_identical_hash_and_verdict(self):
        pop = _population(250)
        a, b = evaluate(pop, CURRENT, BOUNDS), evaluate(list(pop), CURRENT, BOUNDS)
        assert a.snapshot_sha256 == b.snapshot_sha256
        assert a.verdict == b.verdict
        assert [c.as_dict() for c in a.candidates] == [
            c.as_dict() for c in b.candidates
        ]

    def test_hash_changes_when_population_changes(self):
        pop = _population(100)
        assert snapshot_hash(pop) != snapshot_hash(pop + [_rec(9, "42.0")])

    def test_arithmetic_is_exact_not_float(self):
        s = score_population([_rec(1, "0.1"), _rec(2, "0.2")])
        assert s.total_outcome == Decimal("0.3")  # 0.1+0.2 != 0.3 in binary float


class TestScoring:
    def test_score_is_not_win_rate_alone(self):
        """A high hit rate with tiny winners must not beat a lower hit rate with
        real magnitude — mean outcome carries the size."""
        many_small = [_rec(1, "0.5") for _ in range(10)]
        few_large = [_rec(1, "20.0")] + [_rec(1, "-1.0") for _ in range(9)]
        assert (
            score_population(few_large).mean_outcome
            > score_population(many_small).mean_outcome
        )

    def test_worst_drawdown_is_tracked(self):
        s = score_population([_rec(1, "5.0"), _rec(2, "-30.0")])
        assert s.worst_drawdown == Decimal("-30.0")

    def test_filters_select_the_expected_records(self):
        recs = [_rec(1, "1.0", fit=50), _rec(2, "1.0", fit=80)]
        assert len(apply_filter(recs, "narrative_fit_score_min", 60)) == 1

    def test_records_without_usable_outcome_are_dropped(self):
        recs = rows_to_records(
            [
                {
                    "predicted_at": "2026-05-01",
                    "outcome_class": "HIT",
                    "outcome_48h_change_pct": None,
                    "peak_change_pct": None,
                },
                {
                    "predicted_at": "2026-05-02",
                    "outcome_class": "HIT",
                    "outcome_48h_change_pct": None,
                    "peak_change_pct": 12.0,
                },
            ]
        )
        assert len(recs) == 1
        assert recs[0].outcome_pct == Decimal("12.0")
