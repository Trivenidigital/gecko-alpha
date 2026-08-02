"""No autonomous code path can reach the supervised Solana reverse swap.

Structural, not behavioural, and asserted over the AST rather than by grepping
strings: the lane's own docstrings and refusal messages name the very symbols a
substring search would look for, so a text search matches the DOCUMENTATION and
passes on a module that genuinely imports it.

Companion to ``tests/live/test_kraken_exit_has_no_autonomous_path.py``, which
makes the same guarantee for the Kraken exit. The reverse swap is the more
dangerous of the two to leave reachable: it SELLS a position, it is the only
command in the tree that can close a Solana ledger row, and the module it lives
in already holds a funded mainnet signing path.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_SCOUT = _ROOT / "scout"
_LANE_MODULE = "scout.live.solana_lane"
_LANE_FILE = _SCOUT / "live" / "solana_lane.py"

# The operator-only entry points. Nothing outside the module and its tests may
# name them.
_REVERSE_ENTRY_POINTS = {"reverse_place", "reverse_resolve", "reverse_status"}

# Modules the reverse swap must not be able to reach, and why each one:
#
#   live_evaluator  the autonomous closer. One import is the difference between
#                   a supervised command and a scheduled one.
#   kraken_pilot /  the other venue's lane. The two hold different assets at
#   kraken_adapter  different venues and neither's envelope bounds the other's;
#                   a shared import is how one lane's change starts moving the
#                   other's money.
#   scout.live.engine / loops
#                   the scheduled execution machinery. Named in full rather
#                   than as the substring "engine", which would be satisfied
#                   accidentally by `LimitsEngine` and so would assert nothing.
_FORBIDDEN_IMPORTS = (
    "live_evaluator",
    "kraken_pilot",
    "kraken_adapter",
    "scout.live.engine",
    "scout.live.loops",
    "shadow_evaluator",
)

# The Hermes Solana verifier is SECONDARY POST-TRANSACTION EVIDENCE and nothing
# else. It must not appear in the execution path at all — not as an import, not
# as a subprocess, not as a path string — because a helper that is present is a
# helper somebody will reach for during an incident.
_HERMES_MARKERS = ("gecko-solana-verify", "hermes")


def _python_sources() -> list[Path]:
    return sorted(_SCOUT.rglob("*.py"))


def _imports(tree: ast.AST) -> set[str]:
    """Every module name imported anywhere, including inside functions — a
    deferred import is still an import path."""
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                found.add(node.module)
                found.update(f"{node.module}.{a.name}" for a in node.names)
    return found


def _lane_tree() -> ast.AST:
    return ast.parse(_LANE_FILE.read_text("utf-8"))


def test_nothing_in_scout_imports_the_solana_lane():
    """The lane is an operator CLI. A single import from the pipeline is the
    difference between a supervised command and a scheduled one."""
    offenders = []
    for path in _python_sources():
        if path == _LANE_FILE:
            continue
        if any(
            "solana_lane" in name
            for name in _imports(ast.parse(path.read_text("utf-8")))
        ):
            offenders.append(str(path.relative_to(_ROOT)))
    assert offenders == [], (
        f"{offenders} import {_LANE_MODULE}; the supervised reverse swap must "
        "have no in-pipeline caller"
    )


def test_nothing_in_scout_names_a_reverse_entry_point():
    """Belt to the import braces: no attribute access either, so a module that
    obtained the runner some other way still cannot be calling these."""
    offenders: list[tuple[str, str]] = []
    for path in _python_sources():
        if path == _LANE_FILE:
            continue
        for node in ast.walk(ast.parse(path.read_text("utf-8"))):
            name = None
            if isinstance(node, ast.Attribute):
                name = node.attr
            elif isinstance(node, ast.Name):
                name = node.id
            if name in _REVERSE_ENTRY_POINTS:
                offenders.append((str(path.relative_to(_ROOT)), name))
    assert offenders == [], f"reverse entry points named outside the lane: {offenders}"


@pytest.mark.parametrize("forbidden", _FORBIDDEN_IMPORTS)
def test_the_solana_lane_imports_nothing_that_could_drive_it(forbidden):
    offenders = {name for name in _imports(_lane_tree()) if forbidden in name}
    assert offenders == set(), (
        f"solana_lane.py imports {offenders}; the reverse swap must not share a "
        f"module with {forbidden}"
    )


def test_the_reverse_entry_points_are_only_reachable_through_the_cli():
    """Each is called exactly once inside the module: ``main``'s dispatch."""
    tree = _lane_tree()
    main = next(
        node
        for node in tree.body
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef))
        and node.name == "main"
    )
    inside_main = {
        node.func.attr
        for node in ast.walk(main)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert _REVERSE_ENTRY_POINTS <= inside_main, (
        "the reverse entry points must be dispatched from main: missing "
        f"{_REVERSE_ENTRY_POINTS - inside_main}"
    )

    calls = [
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in _REVERSE_ENTRY_POINTS
    ]
    assert len(calls) == len(_REVERSE_ENTRY_POINTS), (
        f"reverse entry points are called {len(calls)} times inside the module; "
        "expected exactly once each, from main's dispatch"
    )


def test_the_reverse_entry_points_are_defined_where_they_are_claimed_to_be():
    """A guard on the guard: if the methods were renamed or moved, every
    assertion above would pass vacuously."""
    defined = {
        node.name
        for node in ast.walk(_lane_tree())
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef))
    }
    assert _REVERSE_ENTRY_POINTS <= defined


def test_the_lane_does_not_shell_out_to_the_hermes_verifier():
    """Secondary post-transaction evidence only, and therefore ABSENT here.

    Route selection, construction, authorization, signing, broadcast, ambiguity
    resolution and authoritative reconciliation are all decided from the
    cluster and the transaction's own bytes. A verifier that were reachable
    from this module would be one an operator could be tempted to believe
    during an incident, which is exactly when its answer carries no authority.
    """
    source = _LANE_FILE.read_text("utf-8").lower()
    for marker in _HERMES_MARKERS:
        assert marker not in source, (
            f"solana_lane.py mentions {marker!r}; the Hermes Solana skill is "
            "secondary post-transaction evidence and must not appear in the "
            "execution path"
        )
    for name in ("subprocess", "os.system", "shutil.which"):
        assert name not in _imports(_lane_tree()), (
            f"solana_lane.py imports {name}; the lane executes no external " "programs"
        )


@pytest.mark.parametrize(
    "pattern", ["*.service", "*.timer", "*.sh", "*.cron", "crontab*"]
)
def test_no_scheduler_artifact_invokes_the_reverse_swap(pattern):
    """Nothing in the repo schedules it. A cron line or a systemd unit naming
    it would make an operator-only command autonomous."""
    offenders = [
        str(path.relative_to(_ROOT))
        for path in _ROOT.rglob(pattern)
        if ".git" not in path.parts
        and "node_modules" not in path.parts
        and "solana_lane reverse" in path.read_text("utf-8", errors="ignore")
    ]
    assert offenders == [], f"{offenders} schedule the supervised reverse swap"


def test_the_exit_cap_exemption_cannot_be_stamped_autonomous():
    """*** The exemption is minted once, and never from a literal. ***

    ``OperatorExitBinding`` is what lifts the daily ENTRY cap off an exit. The
    field the limits engine reads to decide whether to honour it is
    ``authorized_by``, so the guarantee "BOUNDED_AUTONOMOUS cannot use the
    exemption" reduces to one property of the tree: that field is never a
    constant, and is only ever the method of whatever ``authorization_policy_
    for`` returns for this run's mode. A string literal there — or a second
    construction site somewhere with its own idea of the value — is how the
    autonomous mode would acquire an exemption nobody granted it.

    Structural rather than behavioural on purpose: a test that drove the
    autonomous mode and watched it be refused would keep passing if a second,
    unrefused construction site appeared beside the first.
    """
    sites: list[tuple[str, ast.Call]] = []
    for path in _python_sources():
        for node in ast.walk(ast.parse(path.read_text("utf-8"))):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "OperatorExitBinding"
            ):
                sites.append((str(path.relative_to(_ROOT)), node))
    assert [name for name, _ in sites] == [
        str(_LANE_FILE.relative_to(_ROOT))
    ], f"OperatorExitBinding is constructed at {[n for n, _ in sites]}"

    keywords = {kw.arg: kw.value for kw in sites[0][1].keywords}
    authorized_by = keywords["authorized_by"]
    assert (
        isinstance(authorized_by, ast.Attribute) and authorized_by.attr == "method"
    ), (
        "authorized_by must be the authorization policy's own method, not a "
        f"{type(authorized_by).__name__}"
    )
    assert (
        isinstance(authorized_by.value, ast.Call)
        and isinstance(authorized_by.value.func, ast.Name)
        and authorized_by.value.func.id == "authorization_policy_for"
    ), "authorized_by must come from authorization_policy_for(mode)"


def test_the_kraken_exit_implementation_is_untouched_by_this_change():
    """The Kraken lane is out of scope, and 'out of scope' is checkable.

    Asserted as a property of the tree rather than of the diff: the Kraken
    pilot must not name anything the reverse swap introduced, which is the
    shape a "small shared helper" refactor would take.
    """
    introduced = _REVERSE_ENTRY_POINTS | {
        "build_reverse_snapshot",
        "reverse_intent_digest",
        "reverse_authorization_token",
        "route_fingerprint",
        "SwapDirection",
        "DIRECTION_REVERSE",
        "DIRECTION_FORWARD",
    }
    for module in ("kraken_pilot.py", "kraken_adapter.py"):
        tree = ast.parse((_SCOUT / "live" / module).read_text("utf-8"))
        named = {
            node.attr if isinstance(node, ast.Attribute) else node.id
            for node in ast.walk(tree)
            if isinstance(node, (ast.Attribute, ast.Name))
        }
        assert named & introduced == set(), f"{module} names {named & introduced}"
        assert not any(
            "solana" in n for n in _imports(tree)
        ), f"{module} imports a Solana module"
