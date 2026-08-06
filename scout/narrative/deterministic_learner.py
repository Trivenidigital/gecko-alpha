"""Deterministic, non-LLM parameter learner over resolved ``predictions``.

Why this exists
---------------
The previous learner's only analysis step was an Anthropic call. When the
account's credit balance was exhausted it returned
``BadRequestError 400 — "Your credit balance is too low"`` on every cycle, and
learning stopped entirely for ~34 days. The owner's ruling is that paid model
access must not sit on the learning critical path.

This module replaces that dependency with an arithmetic evaluator over the same
qualifying prediction population. It needs no network, no provider and no
credentials, so it cannot fail for billing reasons.

Guarantees
----------
* **Proposal only.** Nothing here writes a parameter. The single public entry
  point returns a :class:`LearnProposal`; applying it is a separate, owner-gated
  decision that this delivery deliberately does not implement.
* **Deterministic.** No RNG, no wall-clock reads, no set iteration order
  dependence. The same snapshot yields byte-identical output, which is what
  makes ``snapshot_sha256`` meaningful as a provenance record.
* **Exact arithmetic.** Percentages are ``Decimal``; no float accumulation.
* **Bounds and locks are inviolable.** Candidates outside ``STRATEGY_BOUNDS``
  are never generated, and locked parameters are never proposed.

Anti-scope: does not apply changes, does not touch quarantine, does not read or
require any provider credential.
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from typing import Any, Callable, Sequence

from scout.narrative.learn_outcome import LearnCycleOutcome, redact, safe_traceback

ALGORITHM_VERSION = "deterministic-v1"

# Fraction of the chronological population reserved for held-out validation.
HOLDOUT_FRACTION = Decimal("0.30")
MIN_TRAIN_SAMPLE = 60
MIN_VALIDATION_SAMPLE = 40
# A candidate must beat baseline by more than this on the holdout to be
# proposed. Set above zero so noise-level differences cannot trigger a change.
MIN_HOLDOUT_IMPROVEMENT = Decimal("0.50")  # percentage points of mean outcome
# Cap on how far a single cycle may move a parameter, as a fraction of its
# bounded range. Prevents one noisy window from slamming a value to an extreme —
# the failure mode that left `laggard_min_volume` pinned at its ceiling.
MAX_CHANGE_FRACTION_PER_CYCLE = Decimal("0.25")
# Sensitivity: neighbours of the winner must not collapse, or the optimum is a
# spike and almost certainly overfitted.
SENSITIVITY_TOLERANCE = Decimal("0.50")


class ProposalVerdict(str, Enum):
    """INTERNAL evaluator verdict. Not the scheduler vocabulary.

    Callers outside this module should read
    :attr:`LearnProposal.cycle_outcome`, which maps these onto the shared
    :class:`~scout.narrative.learn_outcome.LearnCycleOutcome`.

    ``INSUFFICIENT_EVIDENCE`` and ``UNSTABLE_EVIDENCE`` were one conflated
    member. Splitting them matters because they are opposite situations: the
    first is "not enough data to search", the second is "the search produced a
    winner and the stability gates threw it out". Conflated, a rejected-as-
    overfit result reported as an empty sample — and an operator waiting for
    more data would have waited forever.
    """

    # The qualifier is load-bearing and now travels with the value. A bare
    # "NO_CHANGE" reads as a statement about all 14 tunable parameters; it is
    # only ever a statement about the 3 the evaluator can reconstruct from
    # stored prediction columns. `searched_parameters` /
    # `unidentifiable_parameters` in the telemetry give the exact scope.
    NO_CHANGE = "NO_CHANGE_WITHIN_VERIFIED_SEARCH_SPACE"
    PROPOSED_CHANGE_FOR_OWNER_REVIEW = "PROPOSED_CHANGE_FOR_OWNER_REVIEW"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"
    UNSTABLE_EVIDENCE = "UNSTABLE_EVIDENCE"
    # The evaluation did not complete. A returned value, never an escaping
    # exception: an exception at this boundary reaches the scheduler's bare
    # `except`, which has no result object to report and so degrades to a log
    # line with no outcome field — the ambiguity this delivery removes.
    FAILED = "FAILED"


# Total, explicit map. `dict[...]` lookup, never `.get(..., default)`: a new
# verdict member with no mapping must raise KeyError in the test suite, not
# silently inherit a plausible-looking default.
_VERDICT_TO_CYCLE_OUTCOME: dict[ProposalVerdict, LearnCycleOutcome] = {
    ProposalVerdict.NO_CHANGE: LearnCycleOutcome.DETERMINISTIC_NO_CHANGE,
    ProposalVerdict.PROPOSED_CHANGE_FOR_OWNER_REVIEW: (
        LearnCycleOutcome.DETERMINISTIC_PROPOSAL
    ),
    ProposalVerdict.INSUFFICIENT_EVIDENCE: LearnCycleOutcome.INSUFFICIENT_EVIDENCE,
    ProposalVerdict.UNSTABLE_EVIDENCE: LearnCycleOutcome.UNSTABLE_EVIDENCE,
    ProposalVerdict.FAILED: LearnCycleOutcome.FAILED,
}


@dataclass(frozen=True)
class PredictionRecord:
    """One resolved prediction, reduced to the fields the evaluator needs."""

    predicted_at: str
    outcome_class: str
    outcome_pct: Decimal
    narrative_fit_score: int | None
    counter_risk_score: int | None
    market_cap: Decimal | None
    trigger_count: int | None


@dataclass
class CandidateResult:
    key: str
    value: Any
    train_score: Decimal
    validation_score: Decimal
    train_n: int
    validation_n: int
    improvement: Decimal
    rejected_reason: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "value": self.value,
            "train_score": str(self.train_score),
            "validation_score": str(self.validation_score),
            "train_n": self.train_n,
            "validation_n": self.validation_n,
            "improvement": str(self.improvement),
            "rejected_reason": self.rejected_reason,
        }


@dataclass
class LearnProposal:
    verdict: ProposalVerdict
    algorithm_version: str = ALGORITHM_VERSION
    snapshot_sha256: str = ""
    population_size: int = 0
    train_n: int = 0
    validation_n: int = 0
    baseline: dict[str, Any] = field(default_factory=dict)
    candidates: list[CandidateResult] = field(default_factory=list)
    proposed: list[dict[str, Any]] = field(default_factory=list)
    rejections: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    commentary: str | None = None
    commentary_status: str = "NOT_ATTEMPTED"

    # -- search scope ------------------------------------------------------
    # Which parameters the verdict actually covers. Reported alongside every
    # outcome so "no change" is never read as a claim about the whole
    # parameter set.
    searched_parameters: list[str] = field(default_factory=list)
    unidentifiable_parameters: list[str] = field(default_factory=list)

    # -- runtime envelope --------------------------------------------------
    # Deliberately OUTSIDE `as_record()`: these vary run-to-run, and the
    # determinism guarantee ("same snapshot yields byte-identical output") is
    # what makes `snapshot_sha256` meaningful as provenance. They are persisted
    # under a separate `runtime` key by `run_deterministic_daily_learn`.
    correlation_id: str | None = None
    elapsed_ms: int | None = None
    failing_step: str | None = None
    exception_type: str | None = None
    exception_message: str | None = None
    traceback_text: str | None = None

    @property
    def cycle_outcome(self) -> LearnCycleOutcome:
        """The scheduler-facing outcome. Explicit total map, no default."""
        return _VERDICT_TO_CYCLE_OUTCOME[self.verdict]

    def log_fields(self) -> dict[str, Any]:
        """Secret-safe structured fields.

        Every key is present on every path — success, no-evidence and failure —
        with ``None`` where a value genuinely does not exist. A key that appears
        only on some outcomes makes absence ambiguous between "not applicable"
        and "the emitting branch forgot", which is how the original failure path
        got away with logging nothing.

        Contains no credential, no prompt and no provider payload: this learner
        handles none of them, and the two free-text fields it does carry
        (``exception_message``, ``traceback``) are passed through
        :func:`~scout.narrative.learn_outcome.redact` and length-bounded anyway.
        """
        return {
            "outcome": self.cycle_outcome.value,
            "verdict": self.verdict.value,
            "algorithm_version": self.algorithm_version,
            "snapshot_hash": self.snapshot_sha256[:16] or None,
            "prediction_count": self.population_size,
            "train_n": self.train_n,
            "validation_n": self.validation_n,
            "candidates_tested": len(self.candidates),
            "proposed_count": len(self.proposed),
            "rejection_count": len(self.rejections),
            "searched_parameters": len(self.searched_parameters),
            "unidentifiable_parameters": len(self.unidentifiable_parameters),
            "searched_parameter_names": sorted(self.searched_parameters),
            "commentary_status": self.commentary_status,
            "elapsed_ms": self.elapsed_ms,
            "correlation_id": self.correlation_id,
            "failing_step": self.failing_step,
            "exception_type": self.exception_type,
            "exception_message": redact(self.exception_message),
            "traceback": self.traceback_text,
            "dry_run": True,
        }

    def as_record(self) -> dict[str, Any]:
        """Durable provenance record of the EVALUATION. Deterministic.

        Contains only content derived from the snapshot, so two runs over an
        identical population produce identical bytes. Run-varying metadata lives
        in the ``runtime`` envelope written alongside it.
        """
        return {
            "verdict": self.verdict.value,
            "outcome": self.cycle_outcome.value,
            "algorithm_version": self.algorithm_version,
            "snapshot_sha256": self.snapshot_sha256,
            "population_size": self.population_size,
            "train_n": self.train_n,
            "validation_n": self.validation_n,
            "baseline": self.baseline,
            "candidates": [c.as_dict() for c in self.candidates],
            "proposed": self.proposed,
            "rejections": self.rejections,
            "notes": self.notes,
            "searched_parameters": sorted(self.searched_parameters),
            "unidentifiable_parameters": sorted(self.unidentifiable_parameters),
            "commentary_status": self.commentary_status,
            "commentary": self.commentary,
        }

    def runtime_envelope(self) -> dict[str, Any]:
        """Run-varying metadata. Never part of the determinism guarantee."""
        return {
            "correlation_id": self.correlation_id,
            "elapsed_ms": self.elapsed_ms,
            "failing_step": self.failing_step,
            "exception_type": self.exception_type,
            "exception_message": redact(self.exception_message),
            "traceback": self.traceback_text,
        }

    @classmethod
    def failed(
        cls,
        exc: BaseException,
        *,
        failing_step: str,
        correlation_id: str | None = None,
        elapsed_ms: int | None = None,
        population_size: int = 0,
        snapshot_sha256: str = "",
    ) -> "LearnProposal":
        """Build the FAILED result the scheduler logs.

        Carries the same secret-safe field set as every other outcome, so a
        failure is a fully-described event rather than a bare error line.
        """
        return cls(
            verdict=ProposalVerdict.FAILED,
            snapshot_sha256=snapshot_sha256,
            population_size=population_size,
            correlation_id=correlation_id,
            elapsed_ms=elapsed_ms,
            failing_step=failing_step,
            exception_type=type(exc).__name__,
            exception_message=str(exc),
            traceback_text=safe_traceback(exc),
        )


# --------------------------------------------------------------------------
# Population loading
# --------------------------------------------------------------------------

QUALIFYING_SQL = """
    SELECT predicted_at, created_at, outcome_class,
           outcome_48h_change_pct, peak_change_pct,
           narrative_fit_score, counter_risk_score,
           market_cap_at_prediction, trigger_count
      FROM predictions
     WHERE is_control = 0
       AND outcome_class IS NOT NULL
       AND outcome_class != 'UNRESOLVED'
"""


def _dec(value: Any) -> Decimal | None:
    if value is None:
        return None
    # str() first: Decimal(float) inherits binary-float error, which would make
    # the "exact arithmetic" guarantee cosmetic.
    return Decimal(str(value))


def rows_to_records(rows: Sequence[Any]) -> list[PredictionRecord]:
    """Normalise raw rows. Records with no usable outcome are dropped.

    The 48h window is preferred over 6h on the lane's own recorded evidence that
    48h is the correct horizon; ``peak_change_pct`` is the fallback when the 48h
    price was never captured.
    """
    out: list[PredictionRecord] = []
    for r in rows:
        d = dict(r) if not isinstance(r, dict) else r
        pct = _dec(d.get("outcome_48h_change_pct"))
        if pct is None:
            pct = _dec(d.get("peak_change_pct"))
        if pct is None:
            continue
        when = d.get("predicted_at") or d.get("created_at")
        if not when:
            continue
        out.append(
            PredictionRecord(
                predicted_at=str(when),
                outcome_class=str(d.get("outcome_class") or "UNKNOWN"),
                outcome_pct=pct,
                narrative_fit_score=d.get("narrative_fit_score"),
                counter_risk_score=d.get("counter_risk_score"),
                market_cap=_dec(d.get("market_cap_at_prediction")),
                trigger_count=d.get("trigger_count"),
            )
        )
    # Chronological. Ties broken by the full tuple so ordering is total and the
    # snapshot hash is reproducible.
    out.sort(key=lambda r: (r.predicted_at, r.outcome_class, str(r.outcome_pct)))
    return out


def snapshot_hash(records: Sequence[PredictionRecord]) -> str:
    """Content hash of the exact population an evaluation ran against."""
    h = hashlib.sha256()
    for r in records:
        h.update(
            "|".join(
                [
                    r.predicted_at,
                    r.outcome_class,
                    str(r.outcome_pct),
                    str(r.narrative_fit_score),
                    str(r.counter_risk_score),
                    str(r.market_cap),
                    str(r.trigger_count),
                ]
            ).encode("utf-8")
        )
        h.update(b"\x00")
    return h.hexdigest()


def chronological_split(
    records: Sequence[PredictionRecord],
) -> tuple[list[PredictionRecord], list[PredictionRecord]]:
    """Train on the earlier window, validate on the later one.

    Chronological rather than random: a random split leaks future information
    into training through overlapping market regimes, which is exactly how a
    parameter search convinces itself it has found signal.
    """
    n = len(records)
    holdout_n = int((Decimal(n) * HOLDOUT_FRACTION).to_integral_value())
    if holdout_n <= 0:
        return list(records), []
    return list(records[: n - holdout_n]), list(records[n - holdout_n :])


# --------------------------------------------------------------------------
# Scoring
# --------------------------------------------------------------------------


@dataclass
class Score:
    mean_outcome: Decimal
    total_outcome: Decimal
    n: int
    worst_drawdown: Decimal
    hit_rate: Decimal

    @property
    def usable(self) -> bool:
        return self.n > 0


def score_population(records: Sequence[PredictionRecord]) -> Score:
    """Score a filtered population.

    Deliberately NOT win-rate alone: a filter that keeps only tiny winners can
    show a superb hit rate and lose money. Mean outcome carries magnitude,
    ``worst_drawdown`` carries tail risk, and ``n`` carries sufficiency.
    """
    if not records:
        return Score(Decimal(0), Decimal(0), 0, Decimal(0), Decimal(0))
    total = sum((r.outcome_pct for r in records), Decimal(0))
    n = len(records)
    worst = min(r.outcome_pct for r in records)
    hits = sum(1 for r in records if r.outcome_class == "HIT")
    return Score(
        mean_outcome=(total / Decimal(n)),
        total_outcome=total,
        n=n,
        worst_drawdown=worst,
        hit_rate=(Decimal(hits) / Decimal(n) * Decimal(100)),
    )


# Each tunable parameter maps to the predicate that applies it. Only parameters
# whose effect is reconstructable from stored prediction fields are searchable —
# the rest cannot be evaluated offline and are deliberately excluded rather than
# guessed at.
FilterFn = Callable[[PredictionRecord, Any], bool]

SEARCHABLE: dict[str, FilterFn] = {
    "narrative_fit_score_min": lambda r, v: (
        r.narrative_fit_score is not None and r.narrative_fit_score >= v
    ),
    "counter_suppress_threshold": lambda r, v: (
        r.counter_risk_score is None or r.counter_risk_score < v
    ),
    "laggard_min_mcap": lambda r, v: (
        r.market_cap is not None and r.market_cap >= Decimal(str(v))
    ),
    "laggard_max_mcap": lambda r, v: (
        r.market_cap is not None and r.market_cap <= Decimal(str(v))
    ),
    "min_trigger_count": lambda r, v: (
        r.trigger_count is not None and r.trigger_count >= v
    ),
}


def apply_filter(
    records: Sequence[PredictionRecord], key: str, value: Any
) -> list[PredictionRecord]:
    fn = SEARCHABLE[key]
    return [r for r in records if fn(r, value)]


def candidate_values(
    key: str, current: Any, bounds: tuple[float, float], steps: int = 8
) -> list[Any]:
    """Bounded, deterministic candidate grid, clamped by max-change-per-cycle.

    The clamp is what prevents a single window from ratcheting a parameter to an
    extreme. `laggard_min_volume` reaching its ceiling and staying there for 72
    days is the concrete failure this exists to prevent.
    """
    lo_b, hi_b = Decimal(str(bounds[0])), Decimal(str(bounds[1]))
    cur = Decimal(str(current))
    span = (hi_b - lo_b) * MAX_CHANGE_FRACTION_PER_CYCLE
    lo = max(lo_b, cur - span)
    hi = min(hi_b, cur + span)
    if hi <= lo:
        return []
    out: list[Any] = []
    for i in range(steps + 1):
        v = lo + (hi - lo) * Decimal(i) / Decimal(steps)
        v = (
            v.quantize(Decimal("1"))
            if abs(hi - lo) > 10
            else v.quantize(Decimal("0.1"))
        )
        iv = int(v) if v == v.to_integral_value() else float(v)
        if iv not in out:
            out.append(iv)
    return out


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------


def evaluate(
    records: Sequence[PredictionRecord],
    current_params: dict[str, Any],
    bounds: dict[str, tuple[float, float]],
    locked_keys: Sequence[str] = (),
) -> LearnProposal:
    """Produce a dry-run proposal. Applies nothing.

    Returns ``INSUFFICIENT_EVIDENCE`` or ``UNSTABLE_EVIDENCE`` rather than a
    weak proposal whenever the population cannot support a conclusion —
    no-change is always a valid, and usually the correct, answer.
    """
    proposal = LearnProposal(verdict=ProposalVerdict.INSUFFICIENT_EVIDENCE)
    proposal.population_size = len(records)
    proposal.snapshot_sha256 = snapshot_hash(records)

    # Scope of this verdict, computed before any early return so an
    # insufficient-sample result still reports what it WOULD have covered.
    #
    # searched      = has a reconstruction predicate AND a current value AND bounds
    # unidentifiable = in the bounds registry but no predicate over stored
    #                  prediction columns — cannot be evaluated offline, and is
    #                  excluded rather than guessed at
    proposal.searched_parameters = sorted(
        k for k in SEARCHABLE if k in current_params and k in bounds
    )
    proposal.unidentifiable_parameters = sorted(set(bounds) - set(SEARCHABLE))

    train, validation = chronological_split(records)
    proposal.train_n = len(train)
    proposal.validation_n = len(validation)

    if len(train) < MIN_TRAIN_SAMPLE or len(validation) < MIN_VALIDATION_SAMPLE:
        proposal.rejections.append(
            f"insufficient sample: train={len(train)} (min {MIN_TRAIN_SAMPLE}), "
            f"validation={len(validation)} (min {MIN_VALIDATION_SAMPLE})"
        )
        return proposal

    base_train = score_population(train)
    base_val = score_population(validation)
    proposal.baseline = {
        "params": {k: current_params.get(k) for k in SEARCHABLE if k in current_params},
        "train_mean_outcome": str(base_train.mean_outcome),
        "validation_mean_outcome": str(base_val.mean_outcome),
        "validation_n": base_val.n,
        "validation_hit_rate": str(base_val.hit_rate),
        "validation_worst_drawdown": str(base_val.worst_drawdown),
    }

    locked = set(locked_keys)
    best_by_key: dict[str, CandidateResult] = {}

    for key in sorted(SEARCHABLE):
        if key in locked:
            proposal.notes.append(f"{key}: skipped — parameter is locked")
            continue
        if key not in current_params or key not in bounds:
            proposal.notes.append(f"{key}: skipped — no current value or bounds")
            continue

        for value in candidate_values(key, current_params[key], bounds[key]):
            t = score_population(apply_filter(train, key, value))
            v = score_population(apply_filter(validation, key, value))
            if t.n < MIN_TRAIN_SAMPLE // 2 or v.n < MIN_VALIDATION_SAMPLE // 2:
                proposal.candidates.append(
                    CandidateResult(
                        key,
                        value,
                        t.mean_outcome,
                        v.mean_outcome,
                        t.n,
                        v.n,
                        Decimal(0),
                        "sample too small after filtering",
                    )
                )
                continue
            improvement = v.mean_outcome - base_val.mean_outcome
            cand = CandidateResult(
                key, value, t.mean_outcome, v.mean_outcome, t.n, v.n, improvement
            )
            if improvement <= MIN_HOLDOUT_IMPROVEMENT:
                cand.rejected_reason = (
                    f"held-out improvement {improvement} <= "
                    f"{MIN_HOLDOUT_IMPROVEMENT} threshold"
                )
            elif t.mean_outcome <= base_train.mean_outcome:
                # Improves only on the holdout: that is noise, not a signal.
                cand.rejected_reason = "improves on validation but not on train"
            proposal.candidates.append(cand)
            if cand.rejected_reason is None:
                prev = best_by_key.get(key)
                if prev is None or cand.improvement > prev.improvement:
                    best_by_key[key] = cand

    # Sensitivity: a winner whose neighbours collapse is a spike, not an optimum.
    # `stability_rejected` is what separates UNSTABLE_EVIDENCE from
    # INSUFFICIENT_EVIDENCE below: it records that the search DID find a winner
    # and the stability gates threw it out, which is a different operator
    # situation from having nothing to search.
    stability_rejected = False
    for key, cand in sorted(best_by_key.items()):
        neighbours = [
            c
            for c in proposal.candidates
            if c.key == key and c.value != cand.value and c.rejected_reason is None
        ]
        if not neighbours:
            stability_rejected = True
            proposal.rejections.append(
                f"{key}={cand.value}: rejected — no surviving neighbour, "
                "result is a single-point spike (likely overfit)"
            )
            continue
        worst_neighbour = min(n.improvement for n in neighbours)
        if (cand.improvement - worst_neighbour) > SENSITIVITY_TOLERANCE * Decimal(4):
            stability_rejected = True
            proposal.rejections.append(
                f"{key}={cand.value}: rejected — unstable neighbourhood "
                f"(best {cand.improvement} vs worst neighbour {worst_neighbour})"
            )
            continue
        proposal.proposed.append(
            {
                "key": key,
                "current_value": current_params.get(key),
                "proposed_value": cand.value,
                "rollback_value": current_params.get(key),
                "validation_improvement_pp": str(cand.improvement),
                "validation_n": cand.validation_n,
                "train_n": cand.train_n,
            }
        )

    # Explicit, ordered terminal classification. Each branch states which
    # situation it means; none of them infers from a bare truthiness test alone
    # without saying what the test stands for.
    if proposal.proposed:
        proposal.verdict = ProposalVerdict.PROPOSED_CHANGE_FOR_OWNER_REVIEW
    elif stability_rejected:
        # A winner existed and the sensitivity/neighbourhood gates rejected it.
        # NOT the same as "nothing to search" — previously both landed on the
        # conflated member and, when candidates existed, this case even fell
        # through to NO_CHANGE with the note "no candidate beat the current
        # configuration", which was false.
        proposal.verdict = ProposalVerdict.UNSTABLE_EVIDENCE
        proposal.notes.append(
            "a candidate beat the current configuration but failed the "
            "stability gates; treated as overfit, not as a proposal"
        )
    elif proposal.candidates:
        proposal.verdict = ProposalVerdict.NO_CHANGE
        proposal.notes.append(
            "no candidate beat the current configuration on held-out data"
        )
    else:
        # Sample was large enough, but no candidate grid could be built at all
        # (every searchable key locked, or missing a current value / bounds).
        proposal.verdict = ProposalVerdict.INSUFFICIENT_EVIDENCE
        proposal.rejections.append(
            "no evaluable candidate: every searchable parameter was locked or "
            "had no current value and bounds"
        )
    return proposal


async def load_records(conn: Any) -> list[PredictionRecord]:
    """Load the qualifying population using the same predicate as the learner."""
    cursor = await conn.execute(QUALIFYING_SQL)
    rows = await cursor.fetchall()
    return rows_to_records(rows)


def proposal_json(proposal: LearnProposal) -> str:
    """Persisted JSON: deterministic evaluation plus the runtime envelope.

    The two are separate keys so the determinism guarantee stays checkable —
    ``json["evaluation"]`` is byte-identical across runs on an identical
    population; ``json["runtime"]`` is expected to vary.
    """
    return json.dumps(
        {"evaluation": proposal.as_record(), "runtime": proposal.runtime_envelope()},
        indent=2,
        sort_keys=True,
        default=str,
    )


async def _persist_learn_log(conn: Any, proposal: LearnProposal) -> None:
    """Append one ``learn_logs`` provenance row. History, not a mutation."""
    cursor = await conn.execute("SELECT MAX(cycle_number) FROM learn_logs")
    row = await cursor.fetchone()
    cycle_number = (row[0] or 0) + 1
    await conn.execute(
        """INSERT INTO learn_logs
           (cycle_number, cycle_type, reflection_text, changes_made,
            hit_rate_before, hit_rate_after)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (
            cycle_number,
            "daily_deterministic",
            proposal_json(proposal),
            # changes_made records the PROPOSAL, not an application. Applying is
            # an owner-gated decision this delivery does not implement.
            json.dumps(proposal.proposed),
            None,
            None,
        ),
    )
    await conn.commit()


async def run_deterministic_daily_learn(db: Any, strategy: Any) -> LearnProposal:
    """THE RUNTIME LEARNING PATH. Scheduled daily; mutates no parameter.

    This is the function the scheduler calls. It deliberately takes no API key
    and constructs no provider client, so the runtime learning path has no
    provider dependency at all — the property the previous architecture lacked,
    which is why a billing outage stopped learning for ~34 days.

    Writes exactly one row to ``learn_logs`` as a durable provenance record
    (snapshot hash, algorithm version, baseline, candidates, rejections). That
    is a history row, NOT a parameter mutation: no ``Strategy.set`` is called
    and no value or lock state changes.

    **Always returns a LearnProposal. Never returns None, never raises.**
    Every failure becomes a ``FAILED`` proposal carrying ``failing_step``,
    ``exception_type``, a redacted ``exception_message`` and a safe traceback.
    The caller therefore has an outcome to report on every path and never has
    to infer one from a missing return value or a bare log line.
    """
    correlation_id = uuid.uuid4().hex[:16]
    started = time.monotonic()

    def _elapsed() -> int:
        return int((time.monotonic() - started) * 1000)

    # `step` names the stage a failure happened in. Assigned before each stage
    # rather than derived from the traceback so the label survives redaction,
    # truncation and any future logging change.
    step = "acquire_connection"
    records: list[PredictionRecord] = []
    snapshot = ""
    try:
        conn = db._conn
        if conn is None:
            raise RuntimeError("Database not initialized.")

        step = "load_bounds"
        # From the leaf registry, the same object `Strategy.set` and the
        # dashboard endpoint enforce. One definition, no drift.
        from scout.narrative.strategy_bounds import STRATEGY_BOUNDS

        step = "load_records"
        records = await load_records(conn)
        snapshot = snapshot_hash(records)

        step = "load_strategy"
        cursor = await conn.execute("SELECT key, value, locked FROM agent_strategy")
        params: dict[str, Any] = {}
        locked: list[str] = []
        for key, raw, is_locked in await cursor.fetchall():
            try:
                params[key] = json.loads(raw)
            except Exception:
                # A non-JSON stored value is legitimate on older rows; keep the
                # raw string rather than dropping the parameter from the search.
                params[key] = raw
            if is_locked:
                locked.append(key)

        step = "evaluate"
        proposal = evaluate(records, params, STRATEGY_BOUNDS, locked_keys=locked)
        proposal.correlation_id = correlation_id

        step = "persist_learn_log"
        proposal.elapsed_ms = _elapsed()
        await _persist_learn_log(conn, proposal)
        return proposal
    except Exception as exc:
        failure = LearnProposal.failed(
            exc,
            failing_step=step,
            correlation_id=correlation_id,
            elapsed_ms=_elapsed(),
            population_size=len(records),
            snapshot_sha256=snapshot,
        )
        # Best-effort provenance for the failure itself. A learn_logs row that
        # records WHY a cycle failed is worth more than a clean stack, and this
        # write must never replace the original exception in the returned
        # result — hence the swallow, with the reason recorded in-band.
        try:
            conn = getattr(db, "_conn", None)
            if conn is not None:
                await _persist_learn_log(conn, failure)
        except Exception as persist_exc:
            failure.notes.append(
                f"failure record could not be persisted: {type(persist_exc).__name__}"
            )
        return failure
