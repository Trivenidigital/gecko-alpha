"""Typed learner outcomes, mutation isolation, and provider-health state.

Why this module exists
----------------------
The narrative learner failed on every scheduled cycle for ~34 days
(last successful ``learn_logs`` row: cycle 51, 2026-07-02) and nothing surfaced
it. Four independent defects combined to hide a hard provider failure:

1. the caller stamped ``last_daily_learn_at`` and logged
   ``narrative.daily_learn_complete`` unconditionally, so a failed cycle wrote
   state that *looks* like a successful one;
2. ``log.exception`` rendered no traceback (JSONRenderer without
   ``format_exc_info``), so the error event carried zero diagnostic content;
3. the learner returned a bare ``None`` on failure, indistinguishable from the
   legitimate "insufficient sample" early return;
4. there was no provider-health state, so a single billing outage produced only
   undifferentiated per-cycle errors.

The captured root cause was ``BadRequestError 400 — "Your credit balance is too
low to access the Anthropic API"``. No code change fixes that; it is an owner
billing action. What this module fixes is the 34 days of silence.

Anti-scope: this module changes no strategy parameter and performs no provider
model substitution.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


class LearnOutcome(str, Enum):
    """Explicit result of a learn cycle. Replaces bare ``None``.

    ``None`` was ambiguous between "nothing to do" and "crashed", which is
    precisely how a hard provider failure passed for a quiet no-op.
    """

    SUCCESS = "SUCCESS"
    SKIPPED_INSUFFICIENT_SAMPLE = "SKIPPED_INSUFFICIENT_SAMPLE"
    FAILED_PROVIDER_BILLING = "FAILED_PROVIDER_BILLING"
    FAILED_PROVIDER = "FAILED_PROVIDER"
    FAILED_PARSING = "FAILED_PARSING"
    FAILED_VALIDATION = "FAILED_VALIDATION"
    FAILED_INTERNAL = "FAILED_INTERNAL"
    DRY_RUN_SUCCESS = "DRY_RUN_SUCCESS"
    DRY_RUN_FAILED = "DRY_RUN_FAILED"

    @property
    def is_success(self) -> bool:
        """True ONLY for a genuine production success.

        ``DRY_RUN_SUCCESS`` is deliberately excluded: a diagnostic run must
        never advance production success state.
        """
        return self is LearnOutcome.SUCCESS

    @property
    def is_failure(self) -> bool:
        return self.name.startswith("FAILED") or self is LearnOutcome.DRY_RUN_FAILED


class ProviderHealth(str, Enum):
    """Bounded provider condition, shared across every Anthropic consumer."""

    AVAILABLE = "AVAILABLE"
    AUTHENTICATION_FAILED = "AUTHENTICATION_FAILED"
    BILLING_BLOCKED = "BILLING_BLOCKED"
    RATE_LIMITED = "RATE_LIMITED"
    MODEL_REJECTED = "MODEL_REJECTED"
    NETWORK_FAILURE = "NETWORK_FAILURE"
    UNKNOWN = "UNKNOWN"


# Secret shapes that must never reach a log line. Applied to every message this
# module emits, because provider errors sometimes echo request context back.
_REDACTIONS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"sk-ant-[A-Za-z0-9_\-]+"), "sk-ant-<redacted>"),
    (re.compile(r"(?i)(authorization|x-api-key)\s*[:=]\s*\S+"), r"\1: <redacted>"),
    (re.compile(r"(?i)\"api_key\"\s*:\s*\"[^\"]*\""), '"api_key": "<redacted>"'),
)

_MAX_MESSAGE_CHARS = 500


def redact(text: str | None) -> str | None:
    """Strip credential shapes and bound length.

    Applied even to provider-authored strings: a 400 body is not guaranteed to
    be free of echoed request context.
    """
    if text is None:
        return None
    out = str(text)
    for pattern, replacement in _REDACTIONS:
        out = pattern.sub(replacement, out)
    return out[:_MAX_MESSAGE_CHARS]


def classify_provider_error(exc: BaseException) -> tuple[LearnOutcome, ProviderHealth]:
    """Map a provider exception to a typed outcome + health condition.

    Billing is separated from authentication deliberately. Both surface as HTTP
    400/401-family errors from the Anthropic SDK, but they need different
    operator actions — "buy credits" vs "rotate the key" — and conflating them
    sends the operator to the wrong place.
    """
    name = type(exc).__name__
    message = str(exc)
    lowered = message.lower()
    status = getattr(exc, "status_code", None)

    # Billing first: a credit-exhausted account returns 400 invalid_request,
    # which would otherwise be swallowed by the generic provider branch.
    if "credit balance is too low" in lowered or "plans & billing" in lowered:
        return LearnOutcome.FAILED_PROVIDER_BILLING, ProviderHealth.BILLING_BLOCKED
    if status == 401 or "authentication" in lowered or "invalid x-api-key" in lowered:
        return LearnOutcome.FAILED_PROVIDER, ProviderHealth.AUTHENTICATION_FAILED
    if status == 429 or "rate limit" in lowered:
        return LearnOutcome.FAILED_PROVIDER, ProviderHealth.RATE_LIMITED
    if "model" in lowered and (
        "not found" in lowered or "not_found" in lowered or "does not exist" in lowered
    ):
        return LearnOutcome.FAILED_PROVIDER, ProviderHealth.MODEL_REJECTED
    if "APIConnectionError" in name or "Timeout" in name or "timeout" in lowered:
        return LearnOutcome.FAILED_PROVIDER, ProviderHealth.NETWORK_FAILURE
    return LearnOutcome.FAILED_PROVIDER, ProviderHealth.UNKNOWN


@dataclass
class LearnResult:
    """Everything a caller needs to log truthfully, with no secrets."""

    outcome: LearnOutcome
    cycle_type: str
    model_identifier: str | None = None
    prediction_count: int | None = None
    elapsed_ms: int | None = None
    correlation_id: str | None = None
    failing_step: str | None = None
    exception_type: str | None = None
    exception_message: str | None = None
    status_code: int | None = None
    provider_request_id: str | None = None
    provider_health: ProviderHealth | None = None
    proposed_adjustments: list[dict] = field(default_factory=list)
    applied_count: int = 0
    dry_run: bool = False

    def log_fields(self) -> dict[str, Any]:
        """Secret-safe structured fields. Never includes the prompt."""
        return {
            "outcome": self.outcome.value,
            "cycle_type": self.cycle_type,
            "model_identifier": self.model_identifier,
            "prediction_count": self.prediction_count,
            "elapsed_ms": self.elapsed_ms,
            "correlation_id": self.correlation_id,
            "failing_step": self.failing_step,
            "exception_type": self.exception_type,
            "exception_message": redact(self.exception_message),
            "status_code": self.status_code,
            "provider_request_id": self.provider_request_id,
            "provider_health": (
                self.provider_health.value if self.provider_health else None
            ),
            "proposed_adjustment_count": len(self.proposed_adjustments),
            "applied_count": self.applied_count,
            "dry_run": self.dry_run,
        }

    @classmethod
    def from_exception(
        cls,
        exc: BaseException,
        *,
        cycle_type: str,
        failing_step: str,
        model_identifier: str | None = None,
        prediction_count: int | None = None,
        elapsed_ms: int | None = None,
        correlation_id: str | None = None,
        dry_run: bool = False,
    ) -> "LearnResult":
        outcome, health = classify_provider_error(exc)
        if dry_run:
            outcome = LearnOutcome.DRY_RUN_FAILED
        return cls(
            outcome=outcome,
            cycle_type=cycle_type,
            model_identifier=model_identifier,
            prediction_count=prediction_count,
            elapsed_ms=elapsed_ms,
            correlation_id=correlation_id,
            failing_step=failing_step,
            exception_type=type(exc).__name__,
            exception_message=str(exc),
            status_code=getattr(exc, "status_code", None),
            provider_request_id=getattr(exc, "request_id", None),
            provider_health=health,
            dry_run=dry_run,
        )


class DryRunViolation(RuntimeError):
    """A diagnostic cycle attempted a mutation.

    Raised rather than ignored: silently swallowing the write would let a
    diagnostic path drift into mutating production without any test noticing.
    """


class ReadOnlyStrategy:
    """Writer-boundary isolation for diagnostic runs.

    Reads pass through to the real Strategy so the diagnostic exercises the
    genuine production input; EVERY mutation raises. This is the enforcement
    mechanism — a ``dry_run`` flag checked inside each mutation function is the
    second layer, but this one cannot be bypassed by forgetting a check in a
    function added later.
    """

    __slots__ = ("_inner", "violations")

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.violations: list[str] = []

    # -- reads pass through -------------------------------------------------
    def get(self, key: str) -> Any:
        return self._inner.get(key)

    def get_all(self) -> Any:
        return self._inner.get_all()

    # -- every mutation is refused -----------------------------------------
    def _refuse(self, op: str) -> None:
        self.violations.append(op)
        raise DryRunViolation(
            f"diagnostic (dry-run) learn cycle attempted {op}; "
            "diagnostic mode must not mutate strategy state"
        )

    async def set(self, *_a: Any, **_kw: Any) -> None:
        self._refuse("Strategy.set")

    async def lock(self, *_a: Any, **_kw: Any) -> None:
        self._refuse("Strategy.lock")

    async def unlock(self, *_a: Any, **_kw: Any) -> None:
        self._refuse("Strategy.unlock")

    async def set_timestamp(self, *_a: Any, **_kw: Any) -> None:
        self._refuse("Strategy.set_timestamp")

    def __getattr__(self, item: str) -> Any:
        # Anything not explicitly allowed above is refused rather than
        # forwarded — fail closed, so a future Strategy method is not silently
        # reachable from diagnostic mode.
        raise DryRunViolation(
            f"diagnostic learn cycle touched unlisted Strategy attribute {item!r}"
        )


@dataclass
class ProviderHealthState:
    """Deduplicated provider condition with first/last seen and affected modules.

    Exists so a billing outage produces ONE operational alert rather than the
    thousands of indistinguishable per-cycle errors that made this invisible.
    """

    condition: ProviderHealth = ProviderHealth.AVAILABLE
    first_seen_at: str | None = None
    last_seen_at: str | None = None
    affected_modules: set[str] = field(default_factory=set)
    alert_emitted: bool = False

    def record_failure(
        self, condition: ProviderHealth, module: str, *, now: datetime | None = None
    ) -> bool:
        """Record a failure. Returns True if this is a NEW alertable condition."""
        stamp = (now or datetime.now(timezone.utc)).isoformat()
        changed = condition is not self.condition
        if changed:
            self.condition = condition
            self.first_seen_at = stamp
            self.affected_modules = set()
            self.alert_emitted = False
        self.last_seen_at = stamp
        self.affected_modules.add(module)
        should_alert = not self.alert_emitted
        if should_alert:
            self.alert_emitted = True
        return should_alert

    def record_success(self, *, now: datetime | None = None) -> bool:
        """Clear the condition after a successful probe. Returns True if cleared."""
        if self.condition is ProviderHealth.AVAILABLE:
            return False
        self.condition = ProviderHealth.AVAILABLE
        self.first_seen_at = None
        self.last_seen_at = (now or datetime.now(timezone.utc)).isoformat()
        self.affected_modules = set()
        self.alert_emitted = False
        return True

    def snapshot(self) -> dict[str, Any]:
        return {
            "condition": self.condition.value,
            "first_seen_at": self.first_seen_at,
            "last_seen_at": self.last_seen_at,
            "affected_modules": sorted(self.affected_modules),
            "alert_emitted": self.alert_emitted,
        }


PROVIDER_HEALTH = ProviderHealthState()
