"""Canonical asset identity for chain-detection truth.

**Detection truth requires stable asset identity. Prefix similarity is not
identity.** (Operator ruling, 2026-08-23.)

The predicate this replaces matched a cohort member against the derived
first-seen substrate four ways, and let the earliest of ANY of them determine
truth::

    token_id = :coin_id                          -- exact canonical
 OR LOWER(token_id) = LOWER(:symbol)              -- symbol equality
 OR LOWER(token_id) LIKE LOWER(:symbol || '%')    -- forward prefix
 OR LOWER(:coin_id) LIKE LOWER(token_id || '%')   -- reverse prefix

The two prefix arms are not identity tests, and the read-only impact study run
against production on 2026-08-23 found them deciding truth for 18 of 424
resolved cohort members (4.2%). The concrete cases are unambiguous errors, not
edge cases:

===========================  ==========================  ====================
cohort member                matched substrate token     relationship
===========================  ==========================  ====================
``real-world-apparel``       ``re``                      none whatsoever
``floki-ceo``                ``floki``                   different asset
``blind-boxes`` (BLES)       ``bless-2``                 different asset
``terra-luna-2``             ``terra-luna``              LUNA vs LUNC
``vanar-chain-2``            ``vanar-chain``             +6.07 days of lead
``stat``                     ``status``                  different asset
``starpower`` (STAR)         ``stargate-finance``        different project
``openledger-2``             ``opengradient``            different project
===========================  ==========================  ====================

``re`` is the clearest statement of the problem: a two-character token_id is a
prefix of ``real-world-apparel``, so it matched, and because consumers take a
MIN it won.

WHY THIS GETS WORSE ON ITS OWN
------------------------------
``signal_events`` is retention-bounded; the derived substrate is not, and never
prunes. So the pool of tokens a prefix can match grows without limit while the
event table's does not, and MIN always prefers the oldest. The impact study
could NOT yet observe that amplification -- the substrate was backfilled in one
shot and is only ~14 days old, so almost every row shares the same timestamp
floor and prefix winners are not yet disproportionately old. The mechanism is
established by the identity errors above; the aging amplification is a
prediction that becomes measurable as the substrate outlives the 14-day window.

IDENTITY PRECEDENCE
-------------------
1. exact chain + contract/address, where available
2. exact canonical CoinGecko/token identifier
3. explicit provenance-backed alias -> canonical asset
4. otherwise ``identity_unresolved``

Symbol equality sits at tier 3 and is admitted ONLY when it resolves to exactly
one substrate token. An ambiguous symbol is not an identity assertion, so it
earns no detection credit.

DOCUMENTED DEVIATION: bare symbol equality is a pragmatic stand-in for tier 3,
not a true provenance-backed alias mapping -- no such table exists yet. It is
admitted because the impact study measured symbol equality independently
deciding **zero** winners in production (every symbol-matched winner was also an
exact canonical-id match), so admitting it preserves existing detections while
costing nothing measurable. If an alias table is ever built, this tier should
read from it and bare symbol equality should drop to `identity_unresolved`.

Prefix matching survives as DIAGNOSTIC output only. It must never determine
``detected_by_chains``, ``chains_detected_at``, ``chains_lead_minutes``,
early-detection win claims, or persisted performance evidence.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Semantic version stamped on every row this module decides. Historical rows
# carry LEGACY_SEMANTICS instead; their values are never recomputed in place.
CANONICAL_SEMANTICS = "canonical_v1"
LEGACY_SEMANTICS = "legacy_prefix"

TIER_CONTRACT = "contract"
TIER_CANONICAL_ID = "canonical_id"
TIER_ALIAS_UNIQUE = "alias_unique"
TIER_UNRESOLVED = "identity_unresolved"

# Ordered by precedence; index is the tier's rank.
TIER_PRECEDENCE = (TIER_CONTRACT, TIER_CANONICAL_ID, TIER_ALIAS_UNIQUE)


@dataclass
class Resolution:
    """The identity-resolved first-seen, plus what prefix WOULD have claimed."""

    first_seen_at: str | None = None
    tier: str = TIER_UNRESOLVED
    token_id: str | None = None
    #: Diagnostic only. Never assign these to truth columns.
    prefix_first_seen_at: str | None = None
    prefix_token_id: str | None = None
    alias_candidates: list[str] = field(default_factory=list)

    @property
    def detected(self) -> bool:
        return self.first_seen_at is not None

    @property
    def prefix_would_have_credited_more(self) -> bool:
        """True when the discarded prefix match was earlier than the truth.

        This is the quantity worth logging: it is the fabricated lead the old
        semantics were handing out.
        """
        if self.prefix_first_seen_at is None:
            return False
        if self.first_seen_at is None:
            return True
        return self.prefix_first_seen_at < self.first_seen_at


def _is_contract_shaped(value: str) -> bool:
    """EVM 0x-address or a dex: composite key.

    Canonical identifiers from CoinGecko are slugs, so this only ever fires for
    chain-native ids. Kept explicit because tier 1 must be reachable the moment
    a surface carries an address, rather than silently collapsing into tier 2.
    """
    v = value.lower()
    return v.startswith("dex:") or (v.startswith("0x") and len(v) == 42)


def classify_candidate(token_id: str, coin_id: str, symbol: str | None) -> str | None:
    """The identity tier this substrate row establishes, or None for non-identity.

    Prefix relationships deliberately return None: they are not identity, so
    they cannot appear in the precedence order at all.
    """
    if not token_id or not coin_id:
        return None
    sym = (symbol or "").strip().lower()
    tok = token_id.strip().lower()
    coin = coin_id.strip().lower()

    if tok == coin:
        # `tok`, not `token_id`: the equality that got us here was on the
        # normalised value, so the shape test must use it too or a padded /
        # upper-case address is mislabelled tier 2.
        return TIER_CONTRACT if _is_contract_shaped(tok) else TIER_CANONICAL_ID
    if sym and tok == sym:
        return TIER_ALIAS_UNIQUE
    return None


def is_prefix_only(token_id: str, coin_id: str, symbol: str | None) -> bool:
    """A match the OLD predicate admitted purely on prefix similarity."""
    if not token_id or not coin_id:
        return False
    if classify_candidate(token_id, coin_id, symbol) is not None:
        return False
    sym = (symbol or "").strip().lower()
    tok = token_id.strip().lower()
    coin = coin_id.strip().lower()
    if sym and tok.startswith(sym):
        return True
    return bool(tok) and coin.startswith(tok)


def resolve(candidates, coin_id: str, symbol: str | None) -> Resolution:
    """Pick the identity-resolved first-seen from substrate candidates.

    ``candidates`` is an iterable of ``(token_id, first_seen_at)`` already
    filtered to the consumer's time bound.

    Resolution is by TIER FIRST, then earliest within that tier. That ordering
    is the point: an exact identity match must beat any older fuzzy candidate,
    so age can never promote a weaker identity claim over a stronger one.
    """
    by_tier: dict[str, list[tuple[str, str]]] = {}
    prefix_hits: list[tuple[str, str]] = []

    for token_id, first_seen_at in candidates:
        if token_id is None or first_seen_at is None:
            continue
        tier = classify_candidate(token_id, coin_id, symbol)
        if tier is None:
            if is_prefix_only(token_id, coin_id, symbol):
                prefix_hits.append((first_seen_at, token_id))
            continue
        by_tier.setdefault(tier, []).append((first_seen_at, token_id))

    prefix_hits.sort()
    res = Resolution(
        prefix_first_seen_at=prefix_hits[0][0] if prefix_hits else None,
        prefix_token_id=prefix_hits[0][1] if prefix_hits else None,
    )

    for tier in TIER_PRECEDENCE:
        hits = by_tier.get(tier)
        if not hits:
            continue
        # NORMALISED. Comparing raw token_ids here inverts the guard: tier-3
        # membership requires `token_id.strip().lower() == symbol.strip().lower()`,
        # so raw values can differ ONLY by case or whitespace -- i.e. the SAME
        # asset. `PEPE` and `pepe` would have read as ambiguity and silently lost
        # a detection the old predicate made, which is the opposite failure to
        # the one this module exists to fix.
        distinct = {t.strip().lower() for _, t in hits}
        if tier == TIER_ALIAS_UNIQUE and len(distinct) > 1:
            # UNREACHABLE while tier 3 is bare symbol equality: every alias
            # candidate normalises to the symbol itself, so `distinct` is always
            # a single element. `test_alias_ambiguity_is_unreachable_by_construction`
            # asserts that property rather than pretending to exercise this
            # branch.
            #
            # Kept because it becomes live the moment tier 3 is re-sourced from
            # a real provenance-backed alias table, where two DIFFERENT assets
            # genuinely can claim one symbol. Deleting it would leave that
            # future change silently unguarded.
            res.alias_candidates = sorted(distinct)
            res.tier = TIER_UNRESOLVED
            return res
        hits.sort()
        res.first_seen_at, res.token_id = hits[0][0], hits[0][1]
        res.tier = tier
        return res

    return res


# Kept at module scope so the trackers cannot drift apart on the wording of the
# candidate predicate the way the short/long-symbol branches did.
_IDENTITY_ARMS = "token_id = ? OR LOWER(token_id) = LOWER(?)"
_PREFIX_ARMS = (
    "LOWER(token_id) LIKE LOWER(? || '%') OR LOWER(?) LIKE LOWER(token_id || '%')"
)


#: The two time bounds the consumers use. They are NOT interchangeable and are
#: deliberately preserved as-is by this refactor.
#:
#: * ``datetime_plus_5m`` -- gainers/trending: ``datetime(first_seen_at) <
#:   datetime(?, '+5 minutes')``, a tolerance window with both sides normalised.
#: * ``raw_lt`` -- losers: a bare ``first_seen_at < ?``. This compares in the
#:   same BINARY order the stored value was minimised under, so aggregation and
#:   filter agree by construction. Wrapping it in datetime() to "tidy up" would
#:   be a silent behaviour change, not a cleanup.
BOUND_DATETIME_PLUS_5M = "datetime_plus_5m"
BOUND_RAW_LT = "raw_lt"

_BOUND_SQL = {
    BOUND_DATETIME_PLUS_5M: "datetime(first_seen_at) < datetime(?, '+5 minutes')",
    BOUND_RAW_LT: "first_seen_at < ?",
}


async def resolve_chain_first_seen(
    conn,
    coin_id: str,
    symbol: str,
    cutoff_str: str,
    *,
    prefix_diagnostic: bool = True,
    bound: str = BOUND_DATETIME_PLUS_5M,
) -> Resolution:
    """Identity-resolved first-seen for one cohort member.

    The candidate query still admits prefix matches when ``prefix_diagnostic``
    is set, but ONLY so the discarded match can be measured -- ``resolve()``
    never lets a prefix decide truth. That is what makes the fabricated lead the
    old semantics handed out observable instead of merely asserted.

    Symbol length no longer changes the TRUTH path. The previous short/long
    split was itself a defect: it left one function deriving first-seen from two
    different historical boundaries depending on how many characters a ticker
    had. Length now only narrows the diagnostic, because a 2-3 character prefix
    matches a large share of the substrate and is worth neither the rows nor the
    noise.
    """
    where = _IDENTITY_ARMS
    params: list = [coin_id, symbol]
    if prefix_diagnostic:
        where = f"{where} OR {_PREFIX_ARMS}"
        params += [symbol, coin_id]
    params.append(cutoff_str)

    try:
        bound_sql = _BOUND_SQL[bound]
    except KeyError:
        raise ValueError(
            f"unknown bound {bound!r}; expected one of {sorted(_BOUND_SQL)}"
        ) from None

    cur = await conn.execute(
        f"""SELECT token_id, first_seen_at FROM signal_first_seen
            WHERE ({where}) AND {bound_sql}""",
        tuple(params),
    )
    rows = await cur.fetchall()
    return resolve([(r[0], r[1]) for r in rows], coin_id, symbol)
