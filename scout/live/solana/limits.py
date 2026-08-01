"""The lane's execution envelope, in one object.

Every limit the Solana lane enforces lives here: capital, concurrency,
allowlists, slippage, price impact, fees, and freshness. Before this module
they were scattered across ``_quote_and_check``, ``_check_balance``, an inline
10% headroom constant and ``tx_inspector``'s ceilings — which meant no single
place answered "what is this lane allowed to do", and adding a limit meant
finding the right function rather than adding a check.

Shape borrowed from ``tx_inspector.VerificationReport``, deliberately
-------------------------------------------------------------------
A named check with a detail string, ANDed into a ``passed``, and unknown is a
FAILURE. That shape has already proved itself on the inspector: the evidence
file records what was PROVED rather than only what broke, so a reviewer can
see that a limit was evaluated at all. A limit that silently does not apply is
indistinguishable in a log from a limit that passed, and the difference is the
whole point.

Fail-closed means an input the engine cannot read produces a failed check, not
a skipped one. A missing balance, an unparseable price impact, a block height
that could not be fetched — each is "we could not show this is within the
envelope", which is the same answer as "it is not".

Why the engine is stage-oriented
--------------------------------
The limits cannot all be evaluated at one moment: the per-trade band needs a
quote, the fee ceilings need built bytes, freshness needs to be re-asked after
the operator has been sitting at the prompt. So the engine exposes one method
per stage, each returning a ``LimitsReport`` naming its stage. It is still one
object with one source of numbers — ``declared_limits()`` renders the entire
envelope without evaluating anything, which is what ``status`` and the
approval screen show.

*** BOUNDED_AUTONOMOUS INHERITS THIS ENVELOPE EXACTLY. ***
The engine is constructed from ``Settings`` and never sees the mode. There is
no branch here, no per-mode multiplier and no autonomous-only ceiling: the
only thing that differs between supervised and autonomous execution is which
authorization policy answers. If a limit ever needs to know the mode, that is
a change to this rule and not a detail — a test asserts the reports are
identical across modes.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable, Sequence

import structlog

from scout.config import Settings
from scout.live.solana.constants import (
    ALLOWED_PROGRAM_IDS,
    LAMPORTS_PER_SOL,
    USDC_DECIMALS,
)
from scout.live.solana.exceptions import SolanaLimitBreached

log = structlog.get_logger(__name__)

# Stage names. Each is one evaluation point in `place`, and each becomes an
# evidence step, so they are constants rather than string literals at the call
# sites — an evidence reader greps these.
STAGE_QUOTE = "quote"
STAGE_TRANSACTION = "transaction"
STAGE_BALANCE = "balance"
STAGE_FRESHNESS = "freshness"


def _dec(value: Any) -> Decimal:
    return Decimal(str(value))


def _fmt(value: Decimal | None) -> str:
    return "n/a" if value is None else format(value, "f")


def _usdc(raw: int | None) -> Decimal | None:
    return None if raw is None else _dec(raw) / _dec(10**USDC_DECIMALS)


def _sol(lamports: int | None) -> Decimal | None:
    return None if lamports is None else _dec(lamports) / _dec(LAMPORTS_PER_SOL)


@dataclass(frozen=True)
class LimitCheck:
    """One named limit and whether this trade is inside it."""

    name: str
    passed: bool
    detail: str


@dataclass(frozen=True)
class LimitsReport:
    """The outcome of one stage's checks. ``passed`` is the AND of all of them."""

    stage: str
    passed: bool
    checks: tuple[LimitCheck, ...]

    @property
    def failures(self) -> tuple[LimitCheck, ...]:
        return tuple(c for c in self.checks if not c.passed)

    def raise_if_failed(self) -> None:
        """Raise ``SolanaLimitBreached`` unless every check passed."""
        if self.passed:
            return
        summary = "; ".join(f"{c.name}: {c.detail}" for c in self.failures)
        raise SolanaLimitBreached(
            f"{self.stage} limits breached ({len(self.failures)} of "
            f"{len(self.checks)} checks): {summary}"
        )

    def as_evidence(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "passed": self.passed,
            "checks": [
                {"name": c.name, "passed": c.passed, "detail": c.detail}
                for c in self.checks
            ],
            "failed_checks": [c.name for c in self.failures],
        }


class _Builder:
    """Accumulates checks for one stage. Internal to the engine."""

    def __init__(self, stage: str) -> None:
        self._stage = stage
        self._checks: list[LimitCheck] = []

    def add(self, name: str, passed: bool, detail: str) -> None:
        self._checks.append(LimitCheck(name=name, passed=bool(passed), detail=detail))

    def report(self) -> LimitsReport:
        checks = tuple(self._checks)
        return LimitsReport(
            stage=self._stage,
            passed=all(c.passed for c in checks),
            checks=checks,
        )


@dataclass(frozen=True)
class FeeCeilings:
    """The three lamport ceilings, read from Settings in ONE place.

    ``tx_inspector`` enforces the same three against the same Settings fields
    and keeps doing so: it is the gate that stands between remotely-built bytes
    and the signing key, and it has to be safe to use on its own. This is not a
    second implementation of that rule but a second ENFORCEMENT of it, so that
    one report carries the whole envelope. ``test_the_engine_and_the_inspector
    _agree_on_fee_ceilings`` pins them together.
    """

    priority_fee_lamports: int
    jito_tip_lamports: int
    total_fee_lamports: int
    max_ata_creates: int

    @classmethod
    def from_settings(cls, settings: Settings) -> "FeeCeilings":
        return cls(
            priority_fee_lamports=int(settings.SOLANA_PILOT_MAX_PRIORITY_FEE_LAMPORTS),
            jito_tip_lamports=int(settings.SOLANA_PILOT_MAX_JITO_TIP_LAMPORTS),
            total_fee_lamports=int(settings.SOLANA_PILOT_MAX_TOTAL_FEE_LAMPORTS),
            max_ata_creates=int(settings.SOLANA_PILOT_MAX_ATA_CREATES),
        )


@dataclass(frozen=True)
class LaneExposure:
    """What the lane already has on, read from the ledger.

    Passed in rather than queried here so the engine stays synchronous and
    database-free: a limits object that opens its own connection cannot be
    evaluated in a test without one, and the numbers belong to the caller's
    transaction anyway.

    ``notional_usd_today`` is AUTHORIZED notional, not realised — it counts
    what the lane committed to, because a daily cap that only counted settled
    trades would let a burst of in-flight ones through.
    """

    open_positions: int = 0
    active_executions: int = 0
    notional_usd_today: Decimal = Decimal("0")

    def as_evidence(self) -> dict[str, Any]:
        return {
            "open_positions": self.open_positions,
            "active_executions": self.active_executions,
            "notional_usd_today": _fmt(self.notional_usd_today),
        }


class LimitsEngine:
    """Every limit the lane enforces, evaluated stage by stage.

    Constructed from ``Settings`` alone. It holds no connection, no session and
    no mode — see the module docstring on why the mode is deliberately absent.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self.fee_ceilings = FeeCeilings.from_settings(settings)

    # ---------------- declaration ----------------
    def declared_limits(self) -> dict[str, Any]:
        """The whole envelope, rendered without evaluating anything.

        What ``status`` prints and what the approval screen shows. An operator
        authorizing a trade should be able to see the bounds it was judged
        against without reconstructing them from a config file.
        """
        s = self._settings
        return {
            "per_trade_usd": [
                _fmt(_dec(s.SOLANA_PILOT_MIN_ORDER_USD)),
                _fmt(_dec(s.SOLANA_PILOT_MAX_ORDER_USD)),
            ],
            "daily_notional_usd": _fmt(_dec(s.SOLANA_MAX_DAILY_NOTIONAL_USD)),
            "max_open_positions": int(s.SOLANA_MAX_OPEN_POSITIONS),
            "max_concurrent_executions": int(s.SOLANA_MAX_CONCURRENT_EXECUTIONS),
            "max_slippage_bps": int(s.SOLANA_PILOT_SLIPPAGE_BPS),
            "max_price_impact_pct": _fmt(_dec(s.SOLANA_PILOT_MAX_PRICE_IMPACT_PCT)),
            "max_priority_fee_lamports": self.fee_ceilings.priority_fee_lamports,
            "max_jito_tip_lamports": self.fee_ceilings.jito_tip_lamports,
            "max_total_fee_lamports": self.fee_ceilings.total_fee_lamports,
            "max_ata_creates": self.fee_ceilings.max_ata_creates,
            "balance_headroom_pct": _fmt(_dec(s.SOLANA_BALANCE_HEADROOM_PCT)),
            "max_quote_age_sec": _fmt(_dec(s.SOLANA_MAX_QUOTE_AGE_SEC)),
            "blockhash_safety_margin_blocks": int(
                s.SOLANA_PILOT_BLOCKHASH_SAFETY_MARGIN_BLOCKS
            ),
            "allowed_input_mints": list(s.SOLANA_ALLOWED_INPUT_MINTS),
            "allowed_output_mints": list(s.SOLANA_ALLOWED_OUTPUT_MINTS),
            "allowed_route_labels": list(s.SOLANA_ALLOWED_ROUTE_LABELS)
            or "unrestricted",
            "allowed_program_ids": sorted(ALLOWED_PROGRAM_IDS),
        }

    # ---------------- stage: quote ----------------
    def check_quote(
        self,
        quote: Any,
        *,
        amount_lamports: int,
        price_impact_pct: Decimal,
        exposure: LaneExposure,
    ) -> LimitsReport:
        """Capital, concurrency, allowlists and the quote's own terms.

        ``price_impact_pct`` is passed in already converted to PERCENT rather
        than re-parsed here: Jupiter reports the field as a decimal FRACTION,
        and a second conversion site is a second chance to read 2% as 0.02%.
        The caller's converter is the single one, and it returns a 100%
        sentinel for a field it could not parse — which fails this check
        closed.
        """
        s = self._settings
        b = _Builder(STAGE_QUOTE)

        b.add(
            "swap_mode_is_exact_in",
            quote.swap_mode == "ExactIn",
            # otherAmountThreshold means "minimum output" under ExactIn and
            # "maximum input" under ExactOut. Every downstream guarantee in
            # this lane reads it as the former.
            f"swapMode={quote.swap_mode!r}"
            + ("" if quote.swap_mode == "ExactIn" else " — not a minimum-output bound"),
        )
        b.add(
            "quote_matches_requested_amount",
            quote.in_amount == amount_lamports,
            f"inAmount {quote.in_amount} vs requested {amount_lamports} lamports",
        )

        allowed_in = tuple(s.SOLANA_ALLOWED_INPUT_MINTS)
        allowed_out = tuple(s.SOLANA_ALLOWED_OUTPUT_MINTS)
        b.add(
            "input_mint_allowed",
            quote.input_mint in allowed_in,
            f"{quote.input_mint} against {len(allowed_in)} allowed input mint(s)",
        )
        b.add(
            "output_mint_allowed",
            quote.output_mint in allowed_out,
            f"{quote.output_mint} against {len(allowed_out)} allowed output mint(s)",
        )

        b.add(*self._route_check(quote.route_plan))

        approved_bps = int(s.SOLANA_PILOT_SLIPPAGE_BPS)
        b.add(
            "slippage_within_ceiling",
            quote.slippage_bps <= approved_bps,
            f"slippageBps {quote.slippage_bps} against ceiling {approved_bps}",
        )

        max_impact = _dec(s.SOLANA_PILOT_MAX_PRICE_IMPACT_PCT)
        b.add(
            "price_impact_within_ceiling",
            price_impact_pct <= max_impact,
            f"price impact {_fmt(price_impact_pct)}% against ceiling "
            f"{_fmt(max_impact)}%",
        )

        # The per-trade band is enforced on the QUOTE's USDC output, not on the
        # SOL input: USDC is the dollar-denominated leg, so what a $5-$10
        # envelope actually bounds is what comes back. A band on the SOL side
        # would need a price feed of its own and would drift out of the
        # envelope every time SOL moved.
        out_usd = _usdc(quote.out_amount)
        min_usd = _dec(s.SOLANA_PILOT_MIN_ORDER_USD)
        max_usd = _dec(s.SOLANA_PILOT_MAX_ORDER_USD)
        b.add(
            "per_trade_notional_within_band",
            out_usd is not None and min_usd <= out_usd <= max_usd,
            f"quoted {_fmt(out_usd)} USDC against band "
            f"[{_fmt(min_usd)}, {_fmt(max_usd)}] USD",
        )

        daily_cap = _dec(s.SOLANA_MAX_DAILY_NOTIONAL_USD)
        projected = exposure.notional_usd_today + (out_usd or Decimal("0"))
        b.add(
            "daily_notional_within_cap",
            out_usd is not None and projected <= daily_cap,
            f"{_fmt(exposure.notional_usd_today)} already authorized today "
            f"+ {_fmt(out_usd)} this trade = {_fmt(projected)} against cap "
            f"{_fmt(daily_cap)} USD",
        )

        max_open = int(s.SOLANA_MAX_OPEN_POSITIONS)
        b.add(
            "open_positions_within_limit",
            exposure.open_positions < max_open,
            f"{exposure.open_positions} open against limit {max_open} "
            "(this trade would make one more)",
        )

        max_active = int(s.SOLANA_MAX_CONCURRENT_EXECUTIONS)
        b.add(
            "concurrent_executions_within_limit",
            exposure.active_executions < max_active,
            f"{exposure.active_executions} execution(s) in flight against limit "
            f"{max_active}",
        )

        return b.report()

    def _route_check(self, route_plan: Sequence[Any]) -> tuple[str, bool, str]:
        """Route allowlist. Empty configuration means UNRESTRICTED, and says so.

        Jupiter routes through dozens of AMMs and rotates which ones it picks,
        so a closed default list would refuse essentially every real route. The
        allowlist therefore ships empty and the check reports "unrestricted" —
        an evidence file must never imply a restriction that is not configured.
        """
        allowed = {
            label.strip() for label in self._settings.SOLANA_ALLOWED_ROUTE_LABELS
        }
        labels = [str(step.label or "") for step in route_plan]
        if not allowed:
            return (
                "route_labels_allowed",
                True,
                f"unrestricted (SOLANA_ALLOWED_ROUTE_LABELS is empty); route "
                f"used {labels or ['(no steps reported)']}",
            )
        if not labels:
            # An empty route under a configured allowlist is not "nothing to
            # check" — it is a route we could not see, and an allowlist that
            # passes what it cannot read is not an allowlist.
            return (
                "route_labels_allowed",
                False,
                "the quote reported no route steps, so the route could not be "
                "checked against the allowlist",
            )
        rejected = sorted({label for label in labels if label not in allowed})
        return (
            "route_labels_allowed",
            not rejected,
            f"route {labels} against {len(allowed)} allowed label(s)"
            + (f"; NOT allowed: {rejected}" if rejected else ""),
        )

    # ---------------- stage: transaction ----------------
    def check_transaction(self, report: Any) -> LimitsReport:
        """The fee ceilings, applied to what the BYTES actually carry.

        Every amount comes from ``tx_inspector``'s re-derivation of the
        compiled transaction, never from what was requested of Jupiter — the
        request is not a promise about what came back, and that gap is the
        whole reason the inspector exists.
        """
        c = self.fee_ceilings
        b = _Builder(STAGE_TRANSACTION)

        b.add(
            "priority_fee_within_ceiling",
            report.priority_fee_lamports <= c.priority_fee_lamports,
            f"{report.priority_fee_lamports} against ceiling "
            f"{c.priority_fee_lamports} lamports",
        )
        b.add(
            "jito_tip_within_ceiling",
            report.jito_tip_lamports <= c.jito_tip_lamports,
            f"{report.jito_tip_lamports} against ceiling {c.jito_tip_lamports} "
            "lamports",
        )
        b.add(
            "total_fee_within_ceiling",
            report.total_fee_lamports <= c.total_fee_lamports,
            # Base signature fee + priority fee + Jito tip + ATA rent. It
            # bounds every lamport that leaves the wallet other than the swap
            # itself, so a build that respects each component and stacks them
            # still fails here.
            f"{report.total_fee_lamports} against ceiling {c.total_fee_lamports} "
            f"lamports ({_fmt(_sol(report.total_fee_lamports))} SOL)",
        )
        b.add(
            "ata_creates_within_limit",
            report.ata_create_count <= c.max_ata_creates,
            f"{report.ata_create_count} account create(s) against limit "
            f"{c.max_ata_creates} ({report.ata_rent_lamports} lamports of rent)",
        )
        unknown = sorted(set(report.program_ids) - ALLOWED_PROGRAM_IDS)
        b.add(
            "programs_allowlisted",
            not unknown,
            f"{len(report.program_ids)} program(s) against a closed allowlist"
            + (f"; NOT allowed: {unknown}" if unknown else ""),
        )
        return b.report()

    # ---------------- stage: balance ----------------
    def check_balance(
        self,
        *,
        sol_lamports: int | None,
        amount_lamports: int,
        total_fee_lamports: int,
    ) -> LimitsReport:
        """Does the wallet cover the swap, everything the bytes charge, and headroom?

        The ATA rent is inside ``total_fee_lamports`` and is NOT added again:
        the inspector counts it once, from the actual number of account-create
        instructions, which is also why a wallet that already holds the output
        mint is not charged for an account it does not need.
        """
        headroom_pct = _dec(self._settings.SOLANA_BALANCE_HEADROOM_PCT)
        required = amount_lamports + total_fee_lamports
        # Headroom over the WHOLE requirement, so a priority-fee market that
        # moves between the build and the block cannot turn a just-affordable
        # swap into an on-chain failure after the operator has authorized it.
        required_with_headroom = int(_dec(required) * (1 + headroom_pct / 100))
        b = _Builder(STAGE_BALANCE)
        b.add(
            "sol_covers_swap_fees_and_headroom",
            sol_lamports is not None and sol_lamports >= required_with_headroom,
            (
                f"balance {_fmt(_sol(sol_lamports))} SOL against required "
                f"{_fmt(_sol(required_with_headroom))} SOL "
                f"(swap {_fmt(_sol(amount_lamports))} + {total_fee_lamports} lamports "
                f"of fees and rent + {_fmt(headroom_pct)}% headroom)"
                if sol_lamports is not None
                else "SOL balance could not be read, so cover cannot be shown"
            ),
        )
        return b.report()

    def required_lamports(
        self, *, amount_lamports: int, total_fee_lamports: int
    ) -> tuple[int, int]:
        """``(required, required_with_headroom)`` — for the evidence record."""
        headroom_pct = _dec(self._settings.SOLANA_BALANCE_HEADROOM_PCT)
        required = amount_lamports + total_fee_lamports
        return required, int(_dec(required) * (1 + headroom_pct / 100))

    # ---------------- stage: freshness ----------------
    def check_freshness(
        self,
        *,
        quote_age_sec: float | None,
        current_block_height: int | None,
        last_valid_block_height: int | None,
    ) -> LimitsReport:
        """Re-asked AFTER authorization, because the prompt is unbounded time.

        Both limits guard the same failure: the operator authorized a specific
        set of numbers, and by the time they finished reading, those numbers
        may describe a trade that no longer exists. A stale quote's price has
        moved; an expired blockhash cannot land at all.

        Every unreadable input fails. "We could not show this is still fresh"
        and "it is not fresh" get the same answer, because the alternative is
        spending an authorization on a build whose freshness is unknown.
        """
        s = self._settings
        b = _Builder(STAGE_FRESHNESS)

        max_age = _dec(s.SOLANA_MAX_QUOTE_AGE_SEC)
        b.add(
            "quote_age_within_limit",
            quote_age_sec is not None and _dec(quote_age_sec) <= max_age,
            (
                f"quote is {quote_age_sec:.1f}s old against a {_fmt(max_age)}s limit"
                if quote_age_sec is not None
                else "the quote's age could not be established"
            ),
        )

        margin = int(s.SOLANA_PILOT_BLOCKHASH_SAFETY_MARGIN_BLOCKS)
        if current_block_height is None or last_valid_block_height is None:
            b.add(
                "blockhash_within_safety_margin",
                False,
                "the current block height could not be read, so the build's "
                "freshness cannot be shown",
            )
        else:
            deadline = last_valid_block_height - margin
            b.add(
                "blockhash_within_safety_margin",
                current_block_height <= deadline,
                f"height {current_block_height} against safe deadline {deadline} "
                f"(lastValidBlockHeight {last_valid_block_height} minus a "
                f"{margin}-block margin)",
            )
        return b.report()


def notional_usd_today(
    rows: Iterable[tuple[Any, Any]],
    *,
    unreadable_size_usd: Decimal,
    now: datetime | None = None,
) -> Decimal:
    """Sum today's authorized notional from ``(size_usd, created_at)`` rows.

    Two kinds of unreadable data, one rule: **bad data must never widen the
    cap.** A cap that loosens itself on corruption is not a cap.

    A row whose TIMESTAMP will not parse counts toward today, because dropping
    it would raise the amount the lane may still spend.

    A row whose SIZE will not parse counts as ``unreadable_size_usd`` — the
    caller passes the per-trade maximum, the largest that row could legitimately
    have been. Treating it as zero (which this function used to do, against its
    own docstring) is the exact bug: two $40 authorizations total $80, but
    corrupt the second row and the day reads $40, silently creating $40 of
    headroom that was already spent. Refusing outright would be the other
    extreme — one corrupt row would wedge the lane until someone edited the
    database — so the conservative substitution keeps the cap meaningful and
    the lane recoverable.

    Today is UTC, matching the clock every writer in this lane stamps with.
    """
    reference = (now or datetime.now(timezone.utc)).date()
    total = Decimal("0")
    for size_usd, created_at in rows:
        try:
            stamped = datetime.fromisoformat(str(created_at))
        except (TypeError, ValueError):
            stamped = None
        if stamped is not None:
            if stamped.tzinfo is None:
                stamped = stamped.replace(tzinfo=timezone.utc)
            if stamped.astimezone(timezone.utc).date() != reference:
                continue
        try:
            total += _dec(size_usd or 0)
        except (InvalidOperation, ValueError):
            total += unreadable_size_usd
            log.warning(
                "solana_limits_unreadable_notional",
                size_usd=str(size_usd),
                counted_as=format(unreadable_size_usd, "f"),
                reason="unreadable size counted at the per-trade maximum so a "
                "corrupt row cannot widen the daily cap",
            )
    return total
