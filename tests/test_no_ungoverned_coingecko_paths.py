"""Structural tripwire: no CoinGecko HTTP callsite may escape the budget governor.

The 2026-08-21 exhaustion happened because nothing counted monthly credits. The
first fix instrumented `_get_with_backoff` and called it "the single choke
point". That was false. Review named three ungoverned paths; the structural
sweep found EIGHT -- `narrative/observer`, `narrative/predictor`,
`narrative/evaluator` (TWO: the batch fetch and a separate `/simple/price`
fallback), `counter/detail`, plus `briefing/collector`, `secondwave/detector`
and `social/telegram/resolver`, which no enumeration had listed.

A budget is only real if it is impossible to spend outside it. Behavioural tests
cannot prove that -- they only cover the paths someone remembered to exercise.
This asserts the property STRUCTURALLY, so a NEW direct CoinGecko call fails
here rather than silently eating the allowance until the plan runs dry again.

Two routes are legitimate:
  1. `_get_with_backoff` -- the governed primitive (counts + enforces).
  2. `governed_cg_call`  -- for callers owning a retry loop that cannot adopt
     that signature; borrows the same accounting and enforcement.

Anything else is a leak.
"""

import ast
import pathlib

import pytest

SCOUT = pathlib.Path(__file__).resolve().parent.parent / "scout"

NL = chr(10)

# Substrings identifying a CoinGecko-bound request. `cg_api.base_url` is the
# repo's single source of truth for the CG host, so any module building a CG URL
# goes through it; the literal hosts guard against someone hardcoding one.
_CG_URL_MARKERS = ("cg_api.base_url", "api.coingecko.com", "pro-api.coingecko.com")

_GOVERNED_MARKERS = ("_get_with_backoff", "governed_cg_call")

_EXEMPT = {"cg_api.py"}  # defines the hosts; issues nothing


def _python_files():
    for path in SCOUT.rglob("*.py"):
        if path.name in _EXEMPT:
            continue
        yield path


def _fn_source(node, src_lines):
    """Source text of one function, for marker matching."""
    start = getattr(node, "lineno", 1) - 1
    end = getattr(node, "end_lineno", start + 1)
    return NL.join(src_lines[start:end])


def _issues_http(node) -> bool:
    """True if this node calls session.get / session.post anywhere inside it."""
    for sub in ast.walk(node):
        if isinstance(sub, ast.Attribute) and sub.attr in ("get", "post"):
            base = sub.value
            name = getattr(base, "id", None) or getattr(base, "attr", None)
            if name and "session" in str(name).lower():
                return True
    return False


def ungoverned_cg_functions(src: str) -> set:
    """FUNCTIONS that issue CoinGecko HTTP without a governed primitive.

    Both conditions are FUNCTION-scoped, and both matter:

    * CG-URL scoped, because `briefing/collector.py` issues HTTP to CoinGlass,
      alternative.me and DefiLlama alongside one CoinGecko call. A module-level
      URL test flags those five non-CoinGecko fetchers as leaks -- false
      positives that would train someone to suppress the guard.
    * GOVERNED-marker scoped, because a module-level marker test asks only "does
      this file mention `_get_with_backoff` anywhere", so a module holding one
      correctly governed request plus one new raw CoinGecko request PASSES --
      which is the most likely way a leak actually gets introduced.
    """
    tree = ast.parse(src)
    lines = src.splitlines()
    bad = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        fn_src = _fn_source(node, lines)
        if not any(m in fn_src for m in _CG_URL_MARKERS):
            continue
        if not _issues_http(node):
            continue
        if not any(m in fn_src for m in _GOVERNED_MARKERS):
            bad.add(node.name)
    return bad


def test_every_coingecko_http_callsite_is_governed():
    """Any FUNCTION issuing CoinGecko HTTP must reference a governed primitive.

    This is what makes the envelopes real rather than advisory: enforcement
    lives at the request primitive, so a lane that never consults the budget
    still cannot spend.
    """
    leaks = []
    for path in _python_files():
        src = path.read_text(encoding="utf-8")
        if not any(m in src for m in _CG_URL_MARKERS):
            continue
        try:
            bad = ungoverned_cg_functions(src)
        except SyntaxError:  # pragma: no cover
            continue
        for fn in sorted(bad):
            leaks.append(f"{path.relative_to(SCOUT.parent)}::{fn}")

    assert not leaks, (
        "ungoverned CoinGecko HTTP callsite(s) -- these spend monthly credits "
        "that no envelope accounts for, which is exactly how the plan hit 100% "
        f"on 2026-08-21: {sorted(leaks)}. Route through _get_with_backoff, or "
        "use governed_cg_call if the caller owns its retry loop."
    )


# ---------------------------------------------------------------------------
# The guard must be able to fail -- three discriminating mutants
# ---------------------------------------------------------------------------


def test_tripwire_detects_a_plain_leak():
    """A bug in the detector would make this pass unconditionally.

    A guard that cannot fail is worse than no guard, because it is trusted.
    """
    leaky = NL.join(
        [
            "from scout import cg_api",
            "async def go(session, tier):",
            "    url = f'{cg_api.base_url(tier)}/coins/markets'",
            "    async with session.get(url) as resp:",
            "        return await resp.json()",
        ]
    )
    assert ungoverned_cg_functions(leaky) == {"go"}


def test_tripwire_kills_a_MIXED_module():
    """THE decisive mutant: one governed call + one raw call in the SAME module.

    The module-scoped version of this guard PASSED here, because the file
    mentioned a governed primitive somewhere. That is the realistic leak: nobody
    adds a brand-new ungoverned CoinGecko module -- they add one more request to
    a file that already has a governed one.
    """
    mixed = NL.join(
        [
            "from scout import cg_api",
            "from scout.ingestion.coingecko import _get_with_backoff",
            "from scout.coingecko_budget import BUCKET_DISCOVERY",
            "async def governed(session, settings, tier):",
            "    return await _get_with_backoff(",
            "        session, f'{cg_api.base_url(tier)}/coins/markets',",
            "        bucket=BUCKET_DISCOVERY, settings=settings,",
            "    )",
            "async def sneaky(session, tier):",
            "    url = f'{cg_api.base_url(tier)}/coins/categories'",
            "    async with session.get(url) as resp:",
            "        return await resp.json()",
        ]
    )
    bad = ungoverned_cg_functions(mixed)
    assert bad == {"sneaky"}, (
        "a raw CoinGecko request must be caught even when a governed one shares "
        f"the module; got {bad}"
    )


def test_non_coingecko_http_in_a_cg_module_is_not_a_leak():
    """No false positives.

    briefing/collector.py fetches CoinGlass, alternative.me and DefiLlama beside
    one CoinGecko call. Flagging those would train someone to suppress the guard,
    which is how a tripwire stops protecting anything.
    """
    mixed = NL.join(
        [
            "from scout import cg_api",
            "async def other_provider(session):",
            "    async with session.get('https://open-api.coinglass.com/x') as r:",
            "        return await r.json()",
            "async def cg_one(session, tier):",
            "    url = f'{cg_api.base_url(tier)}/global'",
            "    async with session.get(url) as resp:",
            "        return await resp.json()",
        ]
    )
    assert ungoverned_cg_functions(mixed) == {"cg_one"}


# ---------------------------------------------------------------------------
# Named regression set -- every path found by the 2026-08-21 audit
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "module",
    [
        "scout/ingestion/coingecko.py",
        "scout/ingestion/held_position_prices.py",
        "scout/trending/tracker.py",
        "scout/narrative/observer.py",
        "scout/narrative/predictor.py",
        "scout/narrative/evaluator.py",
        "scout/counter/detail.py",
        # Found only by the structural sweep, not by any enumeration:
        "scout/briefing/collector.py",
        "scout/secondwave/detector.py",
        "scout/social/telegram/resolver.py",
    ],
)
def test_known_coingecko_callers_stay_governed(module):
    """The general property is above; this is the regression list.

    If one of these is refactored back into a raw request, the sweep and this
    both fail -- and the named module makes the failure self-explaining.
    """
    src = (SCOUT.parent / module).read_text(encoding="utf-8")
    assert (
        ungoverned_cg_functions(src) == set()
    ), f"{module} has an ungoverned CoinGecko callsite again"
