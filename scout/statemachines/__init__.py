"""State-machine registry: the app's main lifecycles, each declared once.

Why it exists
-------------
The app's state machines were implicit: status strings spelled at the write
sites, states derived from combinations of nullable columns, and "closed" or
"suspended" re-derived differently by each consumer. This package is the one
place that says, for each registered machine, what the states are, what they
mean, which moves happen, and which function performs each move. It does not
cover every lifecycle in the app: the ones surveyed but not declared are
listed in tasks/findings_implicit_state_machines_2026_11_19.md.

How far to trust it
-------------------
Tests compare the state lists with the code wherever the code has a single
spelling to compare with (a CHECK constraint, an Enum, a Literal), and the
transitions for ``solana_execution`` only. Everything else — the remaining
transitions, the meanings, the guard kinds, the hazards — is reviewed prose.
``render.checked_by_tests(machine)`` says which applies to a given machine.

How to use it
-------------
    from scout import statemachines as sm

    sm.get("live_trade").next_states("open")
    sm.is_legal("paper_trade", "open", "closed_tp")
    sm.describe("live_trade", "needs_manual_review")["meaning"]

    python -m scout.statemachines list
    python -m scout.statemachines show paper_trade
    python -m scout.statemachines next live_trade open
    python -m scout.statemachines json

It is descriptive. No pipeline, alerting or execution module imports it, so
it cannot change what the app does. ``assert_legal`` is offered for new code
that wants a guard; adopting it at an existing write site is a behavior
change and needs its own review — and for every machine except
``solana_execution`` the declared transitions are observations, not a
verified complete list, so a guard built on them could refuse a move the
code legitimately makes.
"""

from __future__ import annotations

from typing import Any

from scout.statemachines.model import (
    START,
    Invariant,
    Machine,
    State,
    Transition,
)
from scout.statemachines.registry import MACHINES

__all__ = [
    "START",
    "MACHINES",
    "Machine",
    "State",
    "Transition",
    "Invariant",
    "IllegalTransition",
    "UnknownMachine",
    "names",
    "get",
    "next_states",
    "is_legal",
    "assert_legal",
    "describe",
    "to_dict",
]


class UnknownMachine(KeyError):
    """No machine is registered under that name."""


class IllegalTransition(RuntimeError):
    """A move the registry does not declare for that machine."""


_BY_NAME: dict[str, Machine] = {m.name: m for m in MACHINES}


def names() -> tuple[str, ...]:
    return tuple(_BY_NAME)


def get(name: str) -> Machine:
    try:
        return _BY_NAME[name]
    except KeyError:
        raise UnknownMachine(
            f"no state machine named {name!r}; known: {', '.join(_BY_NAME)}"
        ) from None


def next_states(machine: str, state: str) -> tuple[str, ...]:
    """Declared destinations from ``state`` (pass ``START`` for creation)."""
    return get(machine).next_states(state)


def is_legal(machine: str, src: str | None, dst: str) -> bool:
    return get(machine).is_legal(src, dst)


def assert_legal(machine: str, src: str | None, dst: str) -> None:
    """Raise ``IllegalTransition`` unless ``src -> dst`` is declared."""
    m = get(machine)
    if m.is_legal(src, dst):
        return
    origin = START if src is None else src
    try:
        allowed = m.next_states(origin)
    except KeyError:
        raise IllegalTransition(
            f"{origin!r} is not a state of {machine}; states are "
            f"{', '.join(m.state_names)}"
        ) from None
    raise IllegalTransition(
        f"{machine}: {origin} -> {dst} is not a declared transition; from "
        f"{origin} the declared destinations are "
        f"{', '.join(allowed) if allowed else '(none — terminal)'}"
    )


def describe(machine: str, state: str) -> dict[str, Any]:
    """Everything known about one state, as plain data."""
    m = get(machine)
    s = m.state(state)
    return {
        "machine": m.name,
        "state": s.name,
        "label": s.label,
        "meaning": s.meaning,
        "initial": s.initial,
        "terminal": s.terminal,
        "needs_attention": s.attention,
        "written_by_code": s.written,
        "next": list(m.next_states(s.name)),
        "reached_from": sorted(
            {t.src for t in m.transitions if t.dst == s.name}, key=str
        ),
    }


def _checked_by_tests(m: Machine) -> str:
    # Imported here: render imports the registry, as this module does.
    from scout.statemachines.render import checked_by_tests

    return checked_by_tests(m)


def to_dict() -> dict[str, Any]:
    """The whole registry as JSON-serialisable data (stable key order)."""
    return {
        "machines": [
            {
                "name": m.name,
                "title": m.title,
                "entity": m.entity,
                "storage": m.storage,
                "table": m.table,
                "column": m.column,
                "scope": m.scope,
                "null_state": m.null_state,
                "summary": m.summary,
                "vocabulary": m.vocabulary,
                "enforcement": m.enforcement,
                "checked_by_tests": _checked_by_tests(m),
                "states": [
                    {
                        "name": s.name,
                        "label": s.label,
                        "meaning": s.meaning,
                        "initial": s.initial,
                        "terminal": s.terminal,
                        "needs_attention": s.attention,
                        "written_by_code": s.written,
                        "predicate": s.predicate,
                    }
                    for s in m.states
                ],
                "transitions": [
                    {
                        "from": t.src,
                        "to": t.dst,
                        "via": t.via,
                        "trigger": t.trigger,
                        "guard": t.guard,
                    }
                    for t in m.transitions
                ],
                "invariants": [
                    {
                        "name": i.name,
                        "description": i.description,
                        "sql": i.sql,
                    }
                    for i in m.invariants
                ],
                "foreign_values": list(m.foreign_values),
                "caveats": list(m.caveats),
            }
            for m in MACHINES
        ]
    }
