"""Structural tripwire: no CoinGecko HTTP path may escape the budget governor.

The 2026-08-21 exhaustion happened because nothing counted monthly credits. The
first fix instrumented `_get_with_backoff` and called it "the single choke
point". That was false: review found `narrative/observer`, `narrative/predictor`
and `counter/detail` issuing CoinGecko requests directly, and this sweep found
two more the review had not listed -- `narrative/evaluator.fetch_prices_batch`
and its separate `/simple/price` fallback loop. Five ungoverned paths meant the
monthly model was a fiction while every local number looked reasonable.

A budget is only real if it is impossible to spend outside it. Behavioural tests
cannot prove that -- they only cover the paths someone remembered to exercise.
This test asserts the property STRUCTURALLY, over the whole tree, so a NEW
direct CoinGecko call added later fails here rather than silently eating the
allowance until the plan runs dry again.

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

# Substrings that identify a CoinGecko-bound request. `cg_api.base_url` is the
# repo's single source of truth for the CG host (scout/cg_api.py), so any module
# building a CG URL goes through it; the literal hosts are belt-and-braces
# against someone hardcoding one, which cg_api's own docstring forbids.
_CG_URL_MARKERS = ("cg_api.base_url", "api.coingecko.com", "pro-api.coingecko.com")

_GOVERNED_MARKERS = ("_get_with_backoff", "governed_cg_call")

# Files allowed to name a CG host without issuing requests.
_EXEMPT = {
    "cg_api.py",  # defines the hosts
}


def _python_files():
    for path in SCOUT.rglob("*.py"):
        if path.name in _EXEMPT:
            continue
        yield path


def _issues_http(tree: ast.AST) -> bool:
    """True if the module calls session.get / session.post anywhere."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in ("get", "post"):
            base = node.value
            name = getattr(base, "id", None) or getattr(base, "attr", None)
            if name and "session" in str(name).lower():
                return True
    return False


def test_every_coingecko_http_path_is_governed():
    """Any module that builds a CG URL and issues HTTP must route through the governor.

    This is the property that makes the 65k/30k/5k envelopes real rather than
    advisory: enforcement lives at the request primitive, so a lane that never
    consults the budget still cannot spend.
    """
    leaks = []
    for path in _python_files():
        src = path.read_text(encoding="utf-8")
        if not any(m in src for m in _CG_URL_MARKERS):
            continue
        try:
            tree = ast.parse(src)
        except SyntaxError:  # pragma: no cover
            continue
        if not _issues_http(tree):
            continue  # builds a URL but hands it to someone else
        if not any(m in src for m in _GOVERNED_MARKERS):
            leaks.append(str(path.relative_to(SCOUT.parent)))

    assert not leaks, (
        "ungoverned CoinGecko HTTP path(s) — these spend monthly credits that "
        "no envelope accounts for, which is exactly how the plan hit 100% on "
        f"2026-08-21: {sorted(leaks)}. Route through _get_with_backoff, or use "
        "governed_cg_call if the caller owns its retry loop."
    )


def test_the_tripwire_can_actually_detect_a_leak(tmp_path):
    """The guard must fail on a planted leak.

    Without this, a bug in the detector (a marker typo, an AST walk that never
    matches) would make the tripwire pass unconditionally — a guard that cannot
    fail is worse than no guard, because it is trusted.
    """
    leak = tmp_path / "scout_fake"
    leak.mkdir()
    (leak / "leaky.py").write_text(
        "import aiohttp\n"
        "from scout import cg_api\n"
        "async def go(session, tier):\n"
        "    url = f'{cg_api.base_url(tier)}/coins/markets'\n"
        "    async with session.get(url) as resp:\n"
        "        return await resp.json()\n",
        encoding="utf-8",
    )
    src = (leak / "leaky.py").read_text(encoding="utf-8")
    assert any(m in src for m in _CG_URL_MARKERS)
    assert _issues_http(ast.parse(src))
    assert not any(m in src for m in _GOVERNED_MARKERS), (
        "planted leak must be classified as ungoverned"
    )


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
    ],
)
def test_known_coingecko_callers_stay_governed(module):
    """Pin the specific modules found during the 2026-08-21 audit.

    The sweep above is the general property; this is the regression list. If one
    of these is later refactored back into a raw request, the sweep and this
    both fail — and the named module makes the failure self-explaining.
    """
    src = (SCOUT.parent / module).read_text(encoding="utf-8")
    assert any(m in src for m in _GOVERNED_MARKERS), (
        f"{module} lost its budget governance"
    )
