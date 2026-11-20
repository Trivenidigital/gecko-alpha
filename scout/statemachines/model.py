"""Declarative model for the state-machine registry.

Pure stdlib and import-free of the rest of ``scout`` on purpose: the registry
describes the app's state machines, it must never become a dependency cycle
through them, and scripts / the dashboard / an agent must be able to load it
without opening a database or importing a trading module.
"""

from __future__ import annotations

from dataclasses import dataclass, field

#: Pseudo-state for "the entity does not exist yet". A transition whose
#: ``src`` is ``START`` is a creation.
START = "(new)"

#: Where the SET OF STATES is enforced today.
VOCABULARY_KINDS = (
    "db_check",  # a SQLite CHECK constraint lists the values
    "enum",  # a Python Enum is the single spelling
    "module_constants",  # named constants / a tuple in one module
    "none",  # bare string literals at the write sites
)

#: How strongly TRANSITIONS are enforced today.
ENFORCEMENT_KINDS = (
    "enforced",  # a transition table is consulted and an illegal move raises
    "guarded",  # writes check the prior state (compare-and-swap or filter)
    "declared",  # nothing checks; this registry is the only declaration
)

STORAGE_KINDS = ("column", "derived", "memory", "file")

GUARD_KINDS = (
    "table",  # checked against a transition table before the write
    "cas",  # the SQL statement itself filters on the prior state
    "txn",  # prior state re-read and written inside one locked transaction
    "python",  # an `if` in the same function on a value read earlier
    "insert",  # creation; uniqueness/INSERT semantics are the guard
    "none",  # unconditional write
)


@dataclass(frozen=True)
class State:
    """One state, with the plain-language meaning an operator needs."""

    name: str
    label: str
    meaning: str
    initial: bool = False
    terminal: bool = False
    #: An operator is expected to do something when an entity sits here.
    attention: bool = False
    #: False = part of the declared vocabulary but no code path writes it.
    written: bool = True
    #: For ``storage="derived"`` machines: a SQL boolean expression over
    #: ``Machine.table`` that is true exactly when a row is in this state.
    predicate: str | None = None


@dataclass(frozen=True)
class Transition:
    src: str
    dst: str
    #: ``path/to/module.py::function`` that performs the write. Checked by
    #: tests/test_statemachine_registry.py to still exist.
    via: str
    trigger: str
    guard: str = "none"


@dataclass(frozen=True)
class Invariant:
    """A combination that is representable in storage but must never occur.

    ``sql`` is a read-only query returning one integer: the number of rows
    violating the invariant. ``tables`` lists what it reads so the auditor can
    skip it cleanly on a database that predates one of them.
    """

    name: str
    description: str
    sql: str
    tables: tuple[str, ...]


@dataclass(frozen=True)
class Machine:
    name: str
    title: str
    #: What holds the state, e.g. ``paper_trades.status``.
    entity: str
    storage: str
    summary: str
    states: tuple[State, ...]
    transitions: tuple[Transition, ...]
    vocabulary: str
    enforcement: str
    #: Set for column / derived machines so the auditor can read them.
    table: str | None = None
    column: str | None = None
    invariants: tuple[Invariant, ...] = ()
    #: Hazards verified in the code at registration time. Facts, not wishes.
    caveats: tuple[str, ...] = ()
    #: Values that legitimately appear in ``column`` but are not lifecycle
    #: states of this machine (e.g. sentinel rows written by another lane).
    foreign_values: tuple[str, ...] = field(default=())
    #: For column machines where SQL NULL is itself a state (e.g. "not yet
    #: evaluated"): the name of the state NULL stands for.
    null_state: str | None = None
    #: SQL boolean expression over ``table`` selecting the rows this machine
    #: is about, when the table also holds rows that are not this entity
    #: (sentinel rows, a second window sharing the columns).
    scope: str | None = None

    # ------------------------------------------------------------------ views
    @property
    def state_names(self) -> tuple[str, ...]:
        return tuple(s.name for s in self.states)

    @property
    def initial_states(self) -> tuple[str, ...]:
        return tuple(s.name for s in self.states if s.initial)

    @property
    def terminal_states(self) -> tuple[str, ...]:
        return tuple(s.name for s in self.states if s.terminal)

    def state(self, name: str) -> State:
        for s in self.states:
            if s.name == name:
                return s
        raise KeyError(f"{name!r} is not a state of {self.name}")

    def next_states(self, name: str) -> tuple[str, ...]:
        """Declared destinations from ``name`` (``START`` for creation)."""
        if name != START:
            self.state(name)  # raises on an unknown state
        seen: list[str] = []
        for t in self.transitions:
            if t.src == name and t.dst not in seen:
                seen.append(t.dst)
        return tuple(seen)

    def is_legal(self, src: str | None, dst: str) -> bool:
        """True when ``src -> dst`` is a declared transition.

        ``src=None`` (or ``START``) asks whether an entity may be created in
        ``dst``. Unknown states are never legal.
        """
        origin = START if src is None else src
        if dst not in self.state_names:
            return False
        if origin != START and origin not in self.state_names:
            return False
        return any(t.src == origin and t.dst == dst for t in self.transitions)

    def problems(self) -> list[str]:
        """Structural defects in this declaration (empty = well-formed)."""
        out: list[str] = []
        names = self.state_names
        if len(set(names)) != len(names):
            out.append("duplicate state names")
        if self.vocabulary not in VOCABULARY_KINDS:
            out.append(f"unknown vocabulary kind {self.vocabulary!r}")
        if self.enforcement not in ENFORCEMENT_KINDS:
            out.append(f"unknown enforcement kind {self.enforcement!r}")
        if self.storage not in STORAGE_KINDS:
            out.append(f"unknown storage kind {self.storage!r}")
        if self.storage == "column" and not (self.table and self.column):
            out.append("column machine needs table and column")
        if self.storage == "derived":
            if not self.table:
                out.append("derived machine needs table")
            for s in self.states:
                if s.predicate is None:
                    out.append(f"derived state {s.name!r} has no predicate")
        if not self.initial_states:
            out.append("no initial state")
        for t in self.transitions:
            if t.src != START and t.src not in names:
                out.append(f"transition from unknown state {t.src!r}")
            if t.dst not in names:
                out.append(f"transition to unknown state {t.dst!r}")
            if t.guard not in GUARD_KINDS:
                out.append(f"unknown guard kind {t.guard!r} on {t.src}->{t.dst}")
            if t.via.count("::") != 1:
                out.append(f"transition via {t.via!r} is not path::function")
        keys = [(t.src, t.dst, t.via) for t in self.transitions]
        if len(set(keys)) != len(keys):
            dupes = sorted({k for k in keys if keys.count(k) > 1})
            out.append(f"duplicate transitions: {dupes}")
        for s in self.states:
            outgoing = [t for t in self.transitions if t.src == s.name]
            incoming = [t for t in self.transitions if t.dst == s.name]
            if s.terminal and outgoing:
                out.append(f"terminal state {s.name!r} has outgoing transitions")
            if s.written and not incoming:
                out.append(f"state {s.name!r} is marked written but nothing reaches it")
            # For an enforced machine the transitions ARE the code's own
            # table of permitted moves, so a permitted-but-never-taken state
            # is legitimate there. Elsewhere transitions are observed writes.
            if not s.written and incoming and self.enforcement != "enforced":
                out.append(f"state {s.name!r} is marked unwritten but is reached")
            if s.initial and not s.written:
                out.append(f"state {s.name!r} is initial but marked unwritten")
            if s.predicate is not None and self.storage != "derived":
                out.append(f"state {s.name!r} has a predicate on a non-derived machine")
            if s.name in (START, "new", "NULL", "(NULL)"):
                out.append(f"state name {s.name!r} is reserved")
            if s.initial and not any(
                t.src == START and t.dst == s.name for t in self.transitions
            ):
                out.append(f"initial state {s.name!r} has no creation transition")
        for name in self.foreign_values:
            if name in names:
                out.append(f"foreign value {name!r} is also a state")
        if self.null_state is not None and self.null_state not in names:
            out.append(f"null_state {self.null_state!r} is not a declared state")
        if self.null_state is not None and self.storage != "column":
            out.append("null_state only applies to column machines")
        if self.scope is not None and self.storage not in ("column", "derived"):
            out.append("scope only applies to machines stored in a table")
        return out
