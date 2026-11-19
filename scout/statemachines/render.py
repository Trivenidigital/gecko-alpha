"""Render the registry as docs/state_machines.md.

The document is generated, never hand-edited:
``python -m scout.statemachines render --write``. A test compares the file
with this output, so the document cannot drift from the declarations. Whether
the declarations match the code is a separate question; each machine's
"Checked by tests" line says how much of it a test holds in place.
"""

from __future__ import annotations

from scout.statemachines.model import START, Machine
from scout.statemachines.registry import MACHINES

DOC_PATH = "docs/state_machines.md"

_VOCAB = {
    "db_check": "SQLite CHECK constraint",
    "enum": "Python Enum",
    "module_constants": "constants in one module",
    "none": "not enforced (string literals at the write sites)",
}
_ENFORCE = {
    "enforced": "enforced — an illegal move raises",
    "guarded": "guarded — writes check the prior state",
    "declared": "declared here only — nothing checks at runtime",
}
_GUARD = {
    "table": "transition table",
    "cas": "compare-and-swap in SQL",
    "txn": "re-read and written in one locked transaction",
    "python": "check in Python before the write",
    "insert": "creation",
    "none": "none",
}

#: What tests/test_statemachine_registry.py compares with the code, beyond
#: the CHECK-constraint comparison every ``db_check`` machine gets. Keep in
#: step with that file; test_render_claims_match_the_tests enforces the keys.
STATE_LIST_CHECKS = {
    "paper_trade": (
        "close statuses compared with the string literals in scout/trading"
    ),
    "signal_lane": (
        "state list compared with dashboard/lane_status.py and "
        "dashboard/frontend/signalLaneStatus.js"
    ),
    "tg_alert_outcome": (
        "state list also compared with dashboard/telegram_outcomes.py and "
        "TelegramOutcomePanel.jsx"
    ),
    "tg_resolution": "state list compared with the ResolutionState Enum",
    "evm_execution": "state list compared with the ExecutionState Enum",
    "tg_listener": "state list compared with the ListenerState Literal",
}
TRANSITION_CHECKS = {
    "solana_execution": "transitions compared with the lane's own table",
}


def _vocab_label(m: Machine) -> str:
    if m.storage == "derived":
        return "derived from several columns (no stored state name)"
    return _VOCAB[m.vocabulary]


def checked_by_tests(m: Machine) -> str:
    """One honest sentence on how much of this entry a test pins."""
    parts: list[str] = []
    if m.vocabulary == "db_check":
        parts.append("state list compared with the CHECK constraint")
    if m.name in STATE_LIST_CHECKS:
        parts.append(STATE_LIST_CHECKS[m.name])
    if not parts:
        parts.append("state list not checked against the code")
    if m.name in TRANSITION_CHECKS:
        parts.append(TRANSITION_CHECKS[m.name])
    else:
        parts.append("transitions not checked (only that each named function exists)")
    return "; ".join(parts)


def _cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def _mermaid(m: Machine) -> list[str]:
    lines = ["```mermaid", "stateDiagram-v2"]
    edges: list[tuple[str, str]] = []
    for t in m.transitions:
        edge = ("[*]" if t.src == START else t.src, t.dst)
        if edge not in edges:
            edges.append(edge)
    for src, dst in edges:
        lines.append(f"    {src} --> {dst}")
    for s in m.states:
        if s.terminal and s.written:
            lines.append(f"    {s.name} --> [*]")
    lines.append("```")
    return lines


def _machine(m: Machine) -> list[str]:
    out = [f"## {m.title}", ""]
    out.append(f"`{m.name}` · held in `{m.entity}`")
    out.append("")
    out.append(m.summary)
    out.append("")
    out.append(f"- **State list:** {_vocab_label(m)}")
    out.append(f"- **Transitions:** {_ENFORCE[m.enforcement]}")
    out.append(f"- **Checked by tests:** {checked_by_tests(m)}")
    if m.scope:
        out.append(f"- **Rows it covers:** `{m.scope}`")
    if m.null_state:
        out.append(f"- **NULL means:** `{m.null_state}`")
    if m.foreign_values:
        values = ", ".join(f"`{v}`" for v in m.foreign_values)
        out.append(f"- **Other values in the column (not states):** {values}")
    out.append("")
    out.extend(_mermaid(m))
    out.append("")

    out.append("| State | Shown as | Meaning | Notes |")
    out.append("|---|---|---|---|")
    for s in m.states:
        notes = []
        if s.initial:
            notes.append("initial")
        if s.terminal:
            notes.append("terminal")
        if s.attention:
            notes.append("needs attention")
        if not s.written:
            notes.append("never written by code")
        out.append(
            f"| `{s.name}` | {_cell(s.label)} | {_cell(s.meaning)} | "
            f"{', '.join(notes)} |"
        )
    out.append("")

    out.append("| From | To | When | Performed by | Guard |")
    out.append("|---|---|---|---|---|")
    for t in m.transitions:
        src = "(new)" if t.src == START else f"`{t.src}`"
        out.append(
            f"| {src} | `{t.dst}` | {_cell(t.trigger)} | `{t.via}` | "
            f"{_GUARD[t.guard]} |"
        )
    out.append("")

    derived = [s for s in m.states if s.predicate]
    if derived:
        out.append("How each state is recognised in the table:")
        out.append("")
        for s in derived:
            out.append(f"- `{s.name}`: `{s.predicate}`")
        out.append("")
    if m.invariants:
        out.append("Must never be true (checked by the auditor):")
        out.append("")
        for inv in m.invariants:
            out.append(f"- **{inv.name}** — {inv.description}")
        out.append("")
    if m.caveats:
        out.append("Known hazards (read from the code at registration):")
        out.append("")
        for c in m.caveats:
            out.append(f"- {c}")
        out.append("")
    return out


def render() -> str:
    out = [
        "# State machines",
        "",
        "<!-- GENERATED by `python -m scout.statemachines render --write` from",
        "     scout/statemachines/registry.py. Do not edit by hand; a test",
        "     fails when this file and the registry disagree. -->",
        "",
        f"The {len(MACHINES)} lifecycles of gecko-alpha that have been written",
        "down so far: what the states are, what each one means, which moves",
        "happen and which function performs them. Use it to read a status you",
        "see on the dashboard or in Telegram, and to find the code that",
        "changes it. Lifecycles surveyed but not declared here are listed in",
        "tasks/findings_implicit_state_machines_2026_11_19.md.",
        "",
        "**How far to trust it.** This file always matches the registry. The",
        "registry was written from the code and reviewed, but only part of it",
        "is held in place by tests — each machine's *Checked by tests* line",
        "says which part. Meanings, guards and hazards are prose: read the",
        "function a row points at before relying on a detail.",
        "",
        "The registry describes the app; it does not change it. To query it",
        "from a shell or an agent:",
        "",
        "```",
        "python -m scout.statemachines list",
        "python -m scout.statemachines show live_trade",
        "python -m scout.statemachines next live_trade open",
        "python -m scout.statemachines json",
        "python scripts/audit_state_machines.py --db scout.db",
        "```",
        "",
        "| Machine | Held in | State list | Transitions |",
        "|---|---|---|---|",
    ]
    for m in MACHINES:
        anchor = (
            m.title.lower()
            .replace(" ", "-")
            .replace("/", "")
            .replace("(", "")
            .replace(")", "")
            .replace(",", "")
            .replace("—", "")
        )
        out.append(
            f"| [{m.title}](#{anchor}) | `{_cell(m.entity)}` | "
            f"{_vocab_label(m)} | {m.enforcement} |"
        )
    out.append("")
    for m in MACHINES:
        out.extend(_machine(m))
    return "\n".join(out).rstrip() + "\n"
