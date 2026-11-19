"""Hold the state-machine registry to the code wherever the code can be read.

Same idea as tests/test_alert_registry_coverage.py and
tests/test_schema_version_uniqueness.py. What these guards catch:

- a status value added to (or removed from) a CHECK constraint, an Enum, a
  Literal, the close-status literals in scout/trading, or the dashboard's
  copies, without the registry following — and the other way round;
- any change to the Solana lane's transition table;
- a function a transition points at being renamed or removed;
- docs/state_machines.md going stale.

What they do NOT catch: a transition changing inside a function that keeps
its name, a new write site for an existing status, or a meaning or hazard
note going out of date. ``render.checked_by_tests`` states this per machine
and ``test_render_claims_match_the_tests`` keeps that statement honest.

When one fails after an intentional change: update the registry entry, then
run ``python -m scout.statemachines render --write``.
"""

from __future__ import annotations

import ast
import re
import sqlite3
from pathlib import Path
from typing import get_args

import pytest

from scout import statemachines as sm
from scout.db import Database
from scout.statemachines import render as render_mod
from scout.statemachines.audit import audit
from scout.statemachines.model import START

REPO_ROOT = Path(__file__).resolve().parent.parent


def _functions_defined_in(path: Path) -> set[str]:
    """Names of every function, method and class defined in the file."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    }


def _string_constants(paths: list[Path]) -> set[str]:
    found: set[str] = set()
    for path in paths:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                found.add(node.value)
    return found


async def _fresh_db(tmp_path: Path) -> Path:
    path = tmp_path / "registry.db"
    db = Database(path)
    await db.initialize()
    await db.close()
    return path


# ------------------------------------------------------------- declarations


@pytest.mark.parametrize("machine", sm.MACHINES, ids=lambda m: m.name)
def test_declaration_is_well_formed(machine):
    assert machine.problems() == []


def test_machine_names_are_unique():
    assert len(set(sm.names())) == len(sm.MACHINES)


def test_self_check_catches_malformed_declarations():
    """problems() is what `check` and the test above rely on; prove it bites."""
    from scout.statemachines.model import Machine, State, Transition

    bad = Machine(
        name="bad",
        title="Bad",
        entity="t.c",
        storage="column",
        table="t",
        column="c",
        summary="",
        vocabulary="none",
        enforcement="declared",
        scope=None,
        states=(
            State("a", "A", "a", initial=True),
            State("b", "B", "b", terminal=True),
            State("c", "C", "c", written=False, predicate="1 = 1"),
            State("d", "D", "d"),
        ),
        transitions=(
            Transition(START, "a", "x.py::f", "made", "insert"),
            Transition("a", "b", "x.py::f", "moved"),
            Transition("a", "b", "x.py::f", "moved again"),
            Transition("b", "a", "x.py", "back", "bogus"),
            Transition("a", "c", "x.py::f", "to unwritten"),
            Transition("a", "zzz", "x.py::f", "nowhere"),
        ),
    )
    text = "\n".join(bad.problems())
    for expected in (
        "duplicate transitions",
        "terminal state 'b' has outgoing transitions",
        "unknown guard kind 'bogus'",
        "is not path::function",
        "state 'c' is marked unwritten but is reached",
        "state 'c' has a predicate on a non-derived machine",
        "state 'd' is marked written but nothing reaches it",
        "transition to unknown state 'zzz'",
    ):
        assert expected in text, f"problems() missed: {expected}\n{text}"


def test_same_state_rewrites_are_declarable():
    """Code that rewrites the state a row is already in is a real transition
    (tg_listener restart, operator relabel); the helper must not drop it."""
    from scout.statemachines.registry import _t

    assert [(t.src, t.dst) for t in _t("a", "a", "x.py::f", "again")] == [("a", "a")]
    mesh = _t(("a", "b"), ("a", "b"), "x.py::f", "mesh")
    assert ("a", "a") not in {(t.src, t.dst) for t in mesh}
    full = _t(("a", "b"), ("a", "b"), "x.py::f", "mesh", include_self=True)
    assert len(full) == 4
    assert sm.is_legal("tg_listener", "running", "running")
    assert sm.is_legal("operator_action", "acted", "acted")


@pytest.mark.parametrize("machine", sm.MACHINES, ids=lambda m: m.name)
def test_every_transition_points_at_a_real_function(machine):
    cache: dict[str, set[str]] = {}
    missing = []
    for t in machine.transitions:
        rel, func = t.via.split("::")
        path = REPO_ROOT / rel
        assert path.is_file(), f"{machine.name}: {rel} does not exist"
        if rel not in cache:
            cache[rel] = _functions_defined_in(path)
        if func not in cache[rel]:
            missing.append(t.via)
    assert not missing, (
        f"{machine.name}: transitions name functions or classes that no "
        f"longer exist: {sorted(set(missing))} — update the registry to the "
        "function that now performs the write"
    )


# The machines whose state list a test below compares with the code, beyond
# the CHECK-constraint comparison. docs/state_machines.md repeats this claim
# per machine, so the two must name the same machines.
_STATE_LISTS_COMPARED_HERE = {
    "paper_trade",  # test_paper_close_statuses_in_code_are_all_registered
    "signal_lane",  # test_dashboard_lane_states_equal_the_signal_lane_machine
    "tg_alert_outcome",  # test_alert_outcome_copies_equal_the_..._machine
    "tg_resolution",  # test_enum_backed_vocabularies_equal_the_registry
    "evm_execution",  # same
    "tg_listener",  # same
}
_TRANSITIONS_COMPARED_HERE = {
    "solana_execution",  # test_solana_registry_mirrors_the_lanes_own_table
}


def test_render_claims_match_the_tests():
    """The reference tells the reader which parts of each entry a test pins.
    That sentence must not promise more than this file checks."""
    assert set(render_mod.STATE_LIST_CHECKS) == _STATE_LISTS_COMPARED_HERE
    assert set(render_mod.TRANSITION_CHECKS) == _TRANSITIONS_COMPARED_HERE
    for name in _STATE_LISTS_COMPARED_HERE | _TRANSITIONS_COMPARED_HERE:
        sm.get(name)  # raises if a claim names a machine that is gone
    unchecked = [
        m.name
        for m in sm.MACHINES
        if "state list not checked" in render_mod.checked_by_tests(m)
    ]
    assert unchecked == [
        m.name
        for m in sm.MACHINES
        if m.vocabulary != "db_check" and m.name not in _STATE_LISTS_COMPARED_HERE
    ]


# ------------------------------------------------- database vocabularies


def _check_members(table_sql: str, column: str) -> set[str] | None:
    match = re.search(
        rf"\b{column}\b[^,]*?CHECK\s*\(\s*{column}\s+IN\s*\(([^)]*)\)",
        table_sql,
        re.S | re.I,
    )
    if match is None:
        return None
    return set(re.findall(r"'([^']*)'", match.group(1)))


async def test_db_check_constraints_equal_the_registry(tmp_path):
    path = await _fresh_db(tmp_path)
    conn = sqlite3.connect(path)
    try:
        for m in sm.MACHINES:
            if m.vocabulary != "db_check":
                continue
            row = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
                (m.table,),
            ).fetchone()
            assert row is not None, f"{m.name}: table {m.table} is missing"
            members = _check_members(row[0], m.column)
            assert members is not None, (
                f"{m.name}: registry says {m.table}.{m.column} has a CHECK "
                "constraint, but none was found"
            )
            declared = set(m.state_names) | set(m.foreign_values)
            assert members == declared, (
                f"{m.name}: CHECK on {m.table}.{m.column} and the registry "
                f"disagree. Only in the database: {sorted(members - declared)}; "
                f"only in the registry: {sorted(declared - members)}"
            )
    finally:
        conn.close()


async def test_unconstrained_columns_really_have_no_check(tmp_path):
    """If someone adds a CHECK, the registry's 'not enforced' note is stale."""
    path = await _fresh_db(tmp_path)
    conn = sqlite3.connect(path)
    try:
        for m in sm.MACHINES:
            if m.storage != "column" or m.vocabulary == "db_check":
                continue
            row = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
                (m.table,),
            ).fetchone()
            assert row is not None, f"{m.name}: table {m.table} is missing"
            assert _check_members(row[0], m.column) is None, (
                f"{m.name}: {m.table}.{m.column} now has a CHECK constraint — "
                "set vocabulary='db_check' in the registry"
            )
    finally:
        conn.close()


async def test_every_stored_machine_is_readable_on_a_fresh_database(tmp_path):
    """Tables, columns, derived predicates and invariant SQL all exist and run,
    and a freshly migrated database is in declared states only."""
    path = await _fresh_db(tmp_path)
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        report = audit(conn)
    finally:
        conn.close()
    for entry in report["machines"]:
        machine = sm.get(entry["machine"])
        if machine.storage in ("memory", "file"):
            assert entry["status"] == "not_stored_in_db"
            continue
        assert entry["status"] == "ok", entry
        for inv in entry["invariants"]:
            assert inv["status"] == "ok", (entry["machine"], inv)
    assert report["machines_with_findings"] == []


# ------------------------------------------------------ code vocabularies


def test_solana_registry_mirrors_the_lanes_own_table():
    from scout.live import solana_lane as lane

    m = sm.get("solana_execution")
    assert set(m.state_names) == set(lane.ALL_STATES)
    assert set(m.terminal_states) == set(lane.TERMINAL_STATES)
    declared = {s: set(m.next_states(s)) for s in m.state_names}
    assert declared == {s: set(d) for s, d in lane.LEGAL_TRANSITIONS.items()}
    assert m.next_states(START) == (lane.STATE_QUOTE_CREATED,)
    # And the registry's verdicts agree with the lane's guard on every pair.
    for src in lane.ALL_STATES:
        for dst in lane.ALL_STATES:
            try:
                lane.assert_legal_transition(src, dst)
                legal = True
            except lane.IllegalStateTransition:
                legal = False
            assert m.is_legal(src, dst) is legal, (src, dst)


def test_enum_backed_vocabularies_equal_the_registry():
    from scout.live.evm.execution import ExecutionState
    from scout.social.telegram.listener import ListenerState
    from scout.social.telegram.models import ResolutionState

    assert {e.value for e in ResolutionState} == set(
        sm.get("tg_resolution").state_names
    )
    assert {e.value for e in ExecutionState} == set(sm.get("evm_execution").state_names)
    assert set(get_args(ListenerState)) == set(sm.get("tg_listener").state_names)


_CLOSE_LITERAL = re.compile(r"^closed_[a-z0-9_]+$")
#: closed_* strings in these packages that are column / field names, not
#: status values.
_NOT_A_STATUS = {
    "closed_at",
    "closed_accounts",
    "closed_account_count",
    "closed_accounts_permitted",
}


def test_paper_close_statuses_in_code_are_all_registered():
    """A new exit rule cannot ship a close status the registry has not heard of."""
    files = sorted((REPO_ROOT / "scout" / "trading").glob("*.py"))
    in_code = {
        c for c in _string_constants(files) if _CLOSE_LITERAL.match(c)
    } - _NOT_A_STATUS
    registered = set(sm.get("paper_trade").state_names)
    assert in_code <= registered, (
        "close statuses written in scout/trading but missing from the "
        f"paper_trade machine: {sorted(in_code - registered)}"
    )
    never_spelled = {s for s in registered if s != "open"} - in_code
    assert not never_spelled, (
        "paper_trade declares close statuses that scout/trading never "
        f"spells: {sorted(never_spelled)}"
    )


def test_live_status_literals_in_code_are_all_registered():
    files = sorted((REPO_ROOT / "scout" / "live").rglob("*.py"))
    constants = _string_constants(files)
    in_code = {c for c in constants if _CLOSE_LITERAL.match(c)} - _NOT_A_STATUS
    registered = set(sm.get("live_trade").state_names) | set(
        sm.get("shadow_trade").state_names
    )
    assert in_code <= registered, (
        "close statuses written in scout/live but missing from the live_trade "
        f"/ shadow_trade machines: {sorted(in_code - registered)}"
    )


def test_trade_status_literal_is_covered_by_the_registry():
    from scout.trading.models import TradeStatus

    assert set(get_args(TradeStatus)) == set(sm.get("paper_trade").state_names)


def test_paper_anchor_sentinels_are_declared_as_foreign_values():
    constants = _string_constants(
        [
            REPO_ROOT / "scout" / "live" / "kraken_pilot.py",
            REPO_ROOT / "scout" / "live" / "solana_lane.py",
        ]
    )
    for sentinel in sm.get("paper_trade").foreign_values:
        assert sentinel in constants, f"{sentinel} is no longer written by scout/live"


# --------------------------------------------- dashboard / frontend copies


def test_dashboard_lane_states_equal_the_signal_lane_machine():
    # These two read source text with a regular expression. If one fails with
    # an empty set, the file was reformatted and the pattern needs updating;
    # if it fails with a non-empty difference, a state was added or removed.
    registered = set(sm.get("signal_lane").state_names)

    source = (REPO_ROOT / "dashboard" / "lane_status.py").read_text(encoding="utf-8")
    found = set(re.findall(r'state, code = "(\w+)"', source))
    assert "unknown" in found, (
        "dashboard/lane_status.py no longer derives 'unknown' in the form this "
        "test reads; the signal_lane caveat about it may be stale"
    )
    derived = found - {"unknown"}
    assert derived == registered, (
        f"dashboard/lane_status.py derives {sorted(derived)}; the signal_lane "
        f"machine declares {sorted(registered)}"
    )

    js = (REPO_ROOT / "dashboard" / "frontend" / "signalLaneStatus.js").read_text(
        encoding="utf-8"
    )
    frontend = set(re.findall(r"\['(\w+)', \['\w+", js)) - {"unknown"}
    assert frontend == registered, (
        f"dashboard/frontend/signalLaneStatus.js lists {sorted(frontend)}; the "
        f"signal_lane machine declares {sorted(registered)}"
    )


def test_alert_outcome_copies_equal_the_tg_alert_outcome_machine():
    registered = set(sm.get("tg_alert_outcome").state_names)

    backend = (REPO_ROOT / "dashboard" / "telegram_outcomes.py").read_text(
        encoding="utf-8"
    )
    block = re.search(r"_OUTCOMES = \((.*?)\)", backend, re.S)
    assert block is not None, (
        "dashboard/telegram_outcomes.py no longer has an _OUTCOMES tuple in "
        "the form this test reads; update the pattern"
    )
    listed = set(re.findall(r'"(\w+)"', block.group(1)))
    assert listed == registered, (
        f"dashboard/telegram_outcomes.py lists {sorted(listed)}; the "
        f"tg_alert_outcome machine declares {sorted(registered)}"
    )

    panel = (
        REPO_ROOT / "dashboard" / "frontend" / "components" / "TelegramOutcomePanel.jsx"
    ).read_text(encoding="utf-8")
    block = re.search(r"const OUTCOMES = \[(.*?)\n\]", panel, re.S)
    assert block is not None, (
        "TelegramOutcomePanel.jsx no longer has an OUTCOMES array in the form "
        "this test reads; update the pattern"
    )
    shown = set(re.findall(r"\['(\w+)',", block.group(1))) - {"other_recorded_outcomes"}
    assert shown == registered, (
        f"TelegramOutcomePanel.jsx shows {sorted(shown)}; the tg_alert_outcome "
        f"machine declares {sorted(registered)}"
    )


# ----------------------------------------------------------------- the API


def test_lookup_and_guard_api():
    assert sm.is_legal("paper_trade", "open", "closed_tp")
    assert sm.is_legal("paper_trade", None, "open")
    assert not sm.is_legal("paper_trade", "closed_tp", "open")
    assert not sm.is_legal("paper_trade", "open", "not_a_state")
    assert "needs_manual_review" in sm.next_states("live_trade", "open")
    assert sm.next_states("paper_trade", "closed_sl") == ()

    sm.assert_legal("live_trade", "needs_manual_review", "open")
    with pytest.raises(sm.IllegalTransition, match="terminal"):
        sm.assert_legal("paper_trade", "closed_tp", "open")
    with pytest.raises(sm.IllegalTransition, match="not a state"):
        sm.assert_legal("paper_trade", "bogus", "open")
    with pytest.raises(sm.UnknownMachine):
        sm.get("nope")

    info = sm.describe("live_trade", "needs_manual_review")
    assert info["needs_attention"] is True
    assert info["reached_from"] == ["open"]
    assert set(info["next"]) == {"open", "rejected"}


def test_registry_is_json_serialisable_and_complete():
    import json

    data = json.loads(json.dumps(sm.to_dict()))
    assert [m["name"] for m in data["machines"]] == list(sm.names())
    for m in data["machines"]:
        assert m["states"] and m["transitions"]
        assert m["checked_by_tests"]
        for s in m["states"]:
            assert s["label"] and s["meaning"]


# ----------------------------------------------------- nothing is rewired


def test_no_runtime_module_imports_the_registry():
    """The registry is descriptive. If a pipeline, alerting, execution or
    dashboard module starts importing it, that is a behavior change that
    needs its own review — update this test deliberately when it happens."""
    offenders = []
    roots = [REPO_ROOT / "scout", REPO_ROOT / "dashboard", REPO_ROOT / "scripts"]
    # The auditor is the one consumer, and it only reads.
    allowed = {REPO_ROOT / "scripts" / "audit_state_machines.py"}
    for root in roots:
        for path in root.rglob("*.py"):
            if "statemachines" in path.parts or path in allowed:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                names: list[str] = []
                if isinstance(node, ast.Import):
                    names = [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module:
                    names = [node.module] + [
                        f"{node.module}.{a.name}" for a in node.names
                    ]
                if any(n.startswith("scout.statemachines") for n in names):
                    offenders.append(str(path.relative_to(REPO_ROOT)))
    assert offenders == []


# --------------------------------------------------------- generated doc


def test_generated_reference_is_current():
    path = REPO_ROOT / render_mod.DOC_PATH
    assert path.is_file(), (
        f"{render_mod.DOC_PATH} is missing; run "
        "`python -m scout.statemachines render --write`"
    )
    assert path.read_text(encoding="utf-8") == render_mod.render(), (
        f"{render_mod.DOC_PATH} is out of date; run "
        "`python -m scout.statemachines render --write`"
    )
