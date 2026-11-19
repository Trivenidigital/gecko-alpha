"""``python -m scout.statemachines`` — query the registry from a shell.

    list                      one line per machine
    show <machine>            states, transitions and hazards of one machine
    next <machine> <state>    where an entity in <state> may go
    json                      the whole registry as JSON (for tools and agents)
    check                     structural self-check of the declarations
    render [--write|--check]  print, write or verify docs/state_machines.md
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from scout import statemachines as sm
from scout.statemachines import render as render_mod
from scout.statemachines.model import START

REPO_ROOT = Path(__file__).resolve().parents[2]


def _list() -> int:
    width = max(len(n) for n in sm.names())
    for m in sm.MACHINES:
        print(f"{m.name:<{width}}  {m.enforcement:<8}  {m.entity}")
    return 0


def _show(name: str) -> int:
    m = sm.get(name)
    print(f"{m.title}  [{m.name}]")
    print(f"  held in     : {m.entity}")
    print(f"  state list  : {m.vocabulary}")
    print(f"  transitions : {m.enforcement}")
    print(f"  tests check : {render_mod.checked_by_tests(m)}")
    if m.scope:
        print(f"  rows covered: {m.scope}")
    print(f"  {m.summary}")
    print("\nstates:")
    for s in m.states:
        flags = [
            f
            for f, on in (
                ("initial", s.initial),
                ("terminal", s.terminal),
                ("needs attention", s.attention),
                ("never written", not s.written),
            )
            if on
        ]
        suffix = f"  ({', '.join(flags)})" if flags else ""
        print(f"  {s.name}{suffix}\n      {s.meaning}")
    print("\ntransitions:")
    for t in m.transitions:
        src = "(new)" if t.src == START else t.src
        print(f"  {src} -> {t.dst}   [{t.guard}]  {t.via}\n      {t.trigger}")
    if m.caveats:
        print("\nknown hazards:")
        for c in m.caveats:
            print(f"  - {c}")
    return 0


def _next(name: str, state: str) -> int:
    m = sm.get(name)
    origin = START if state in ("new", START) else state
    destinations = m.next_states(origin)
    if not destinations:
        kind = "terminal" if m.state(origin).terminal else "no declared moves"
        print(f"(none — {kind})")
        return 0
    for dst in destinations:
        vias = sorted(
            {t.via for t in m.transitions if t.src == origin and t.dst == dst}
        )
        print(f"{dst}   via {', '.join(vias)}")
    return 0


def _check() -> int:
    bad = 0
    for m in sm.MACHINES:
        for problem in m.problems():
            bad += 1
            print(f"{m.name}: {problem}")
    print(f"{len(sm.MACHINES)} machines, {bad} problems")
    return 1 if bad else 0


def _render(write: bool, check: bool) -> int:
    text = render_mod.render()
    path = REPO_ROOT / render_mod.DOC_PATH
    if write:
        path.write_text(text, encoding="utf-8")
        print(f"wrote {render_mod.DOC_PATH}")
        return 0
    if check:
        current = path.read_text(encoding="utf-8") if path.exists() else ""
        if current != text:
            print(
                f"{render_mod.DOC_PATH} is out of date; run "
                "`python -m scout.statemachines render --write`"
            )
            return 1
        print(f"{render_mod.DOC_PATH} is current")
        return 0
    sys.stdout.write(text)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m scout.statemachines", description=__doc__.split("\n")[0]
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    show = sub.add_parser("show")
    show.add_argument("machine")
    nxt = sub.add_parser("next")
    nxt.add_argument("machine")
    nxt.add_argument("state")
    sub.add_parser("json")
    sub.add_parser("check")
    rend = sub.add_parser("render")
    mode = rend.add_mutually_exclusive_group()
    mode.add_argument("--write", action="store_true")
    mode.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)

    try:
        if args.cmd == "list":
            return _list()
        if args.cmd == "show":
            return _show(args.machine)
        if args.cmd == "next":
            return _next(args.machine, args.state)
        if args.cmd == "json":
            json.dump(sm.to_dict(), sys.stdout, indent=2)
            sys.stdout.write("\n")
            return 0
        if args.cmd == "check":
            return _check()
        return _render(args.write, args.check)
    except KeyError as exc:  # UnknownMachine / unknown state
        # KeyError's str() is the repr of its argument; print the message.
        message = exc.args[0] if exc.args else str(exc)
        print(message, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
