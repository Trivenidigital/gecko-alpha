"""Structural guards for the derived first-seen substrate.

The substrate is only correct because of two structural facts, neither of which
is visible at any single call site:

1. ``emit_event`` is the ONLY thing that inserts into ``signal_events``. A
   future direct INSERT elsewhere would be invisible to every migrated
   consumer -- the row would exist, lead-time attribution would silently omit
   it, and nothing would error.

2. No consumer derives first-seen from ``signal_events`` any more. The point of
   option F is that retention stops being the implicit historical boundary; one
   un-migrated consumer keeps that coupling alive for itself.

WHY THIS FILE WAS REWRITTEN
---------------------------
The first version scanned for the literal substring
``MIN(created_at) FROM signal_events``. It passed while SIX live derivations
sat in the scanned trees, and it is what let the trending tracker's
short-symbol branch ship un-migrated. Text matching cannot see any of the
real phrasings:

    f"SELECT MIN({timestamp_col}) FROM {table_name} "   <- table is a variable
    "MIN(created_at) AS t FROM signal_events"           <- alias breaks adjacency
    f"SELECT MIN({ts}) FROM {table} {alias}"            <- ts is "e.created_at"

Its "falsifiers" planted the scanner's own literal and asserted it was found,
which pins the ``in`` operator rather than coverage -- a tautology that reads
like evidence.

The load-bearing guard here is therefore AST-based and keys on the thing the
dynamic form cannot hide: the exact string literal ``"signal_events"`` has to
be written somewhere to reach ``_check_detector``. Exact-match on an AST
constant also cannot be fooled by a comment or docstring mentioning the table.
"""

import ast
import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_TREES = ("scout", "dashboard", "scripts")
_TABLE = "signal_" + "events"

_INSERT_PAT = "INSERT INTO " + _TABLE

# Catches aliased columns and table aliases that adjacency missed:
#   MIN(created_at) AS t FROM signal_events
#   MIN(e.created_at) FROM signal_events e
_DERIVE_RE = re.compile(
    r"MIN\s*\(\s*[\w.]*created_at\s*\)" r"(?:\s+AS\s+\w+)?" r"\s+FROM\s+" + _TABLE,
    re.I,
)

# Every file permitted to name the table as a string literal, with the reason.
# Anything NOT here is a new coupling and must be an explicit decision.
_LITERAL_ALLOWED = {
    # Lower-bounded lookback window: asks "first seen WITHIN this window", so
    # retention is not its implicit historical boundary -- provided retention
    # stays >= the window, which test_prospective_lookback_floor pins.
    "scout/conviction/prospective.py": "lower-bounded lookback window",
    # §12a freshness registry: names the table to monitor it, not to read
    # first-seen from it.
    "dashboard/db.py": "table-freshness registry entry",
    # Read-only audit; boolean EXISTS bounded by a tolerance window.
    "scripts/audit_missed_gainers.py": "read-only window-bounded audit",
    # Offline provenance backfill. Its LIVE-database query reads
    # signal_first_seen; it names signal_events only for the FROZEN /root
    # snapshots, which predate the signal_first_seen table (migration
    # 20260823) and whose contents can never change -- so the retention
    # coupling this guard exists to prevent cannot arise there.
    "scripts/backfill_chain_identity_recompute.py": "frozen snapshots only",
}


def _sources() -> list[Path]:
    out: list[Path] = []
    for tree in _TREES:
        d = _ROOT / tree
        if d.is_dir():
            out.extend(p for p in d.rglob("*.py") if "__pycache__" not in p.parts)
    return out


def _read_all() -> dict[Path, str]:
    return {p: p.read_text(encoding="utf-8", errors="replace") for p in _sources()}


def _scan(pattern: str, texts: dict[Path, str]) -> list[Path]:
    return sorted(p for p, t in texts.items() if pattern in t)


def _scan_re(rx: "re.Pattern[str]", texts: dict[Path, str]) -> list[Path]:
    return sorted(p for p, t in texts.items() if rx.search(t))


def _table_literal_sites(texts: dict[Path, str]) -> dict[str, list[int]]:
    """Files naming the table as an EXACT string literal, with line numbers.

    Exact match is what makes this immune to prose: a docstring or comment
    mentioning signal_events is never a constant equal to it.
    """
    found: dict[str, list[int]] = {}
    for path, text in texts.items():
        try:
            tree = ast.parse(text)
        except SyntaxError:
            continue
        lines = [
            node.lineno
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and node.value == _TABLE
        ]
        if lines:
            try:
                key = path.relative_to(_ROOT).as_posix()
            except ValueError:  # synthetic path in a falsifier
                key = path.as_posix()
            found[key] = sorted(lines)
    return found


# ---------------------------------------------------------------------------
# Falsifiers. Each plants a form the PREVIOUS guard could not see.
# ---------------------------------------------------------------------------


def test_the_ast_scanner_sees_a_literal_passed_as_an_argument():
    """THE case that shipped: a dynamic query built from a passed-in table name.

    No text pattern can match `f"FROM {table_name}"`, but the caller still has
    to write the table name as a literal somewhere.
    """
    src = 'await _check_detector(db, "signal_' + 'events", "token_id", x)\n'
    tmp = {Path("fake/dynamic.py"): src}
    assert _table_literal_sites(tmp) == {"fake/dynamic.py": [1]}


def test_the_ast_scanner_ignores_prose_mentions():
    """A comment or docstring naming the table must not trip the guard."""
    src = '"""Reads from signal_' + 'events history."""\n# signal_' + "events\nx = 1\n"
    assert _table_literal_sites({Path("fake/prose.py"): src}) == {}


def test_the_regex_sees_aliased_and_dotted_derivations():
    """Both forms the adjacency match missed."""
    planted = {
        Path("fake/plain.py"): "SELECT MIN(created_at) FROM " + _TABLE,
        Path("fake/aliased.py"): "SELECT MIN(created_at) AS t FROM " + _TABLE,
        Path("fake/dotted.py"): "SELECT MIN(e.created_at) FROM " + _TABLE + " e",
        Path("fake/clean.py"): "SELECT MIN(first_seen_at) FROM signal_first_seen",
    }
    assert _scan_re(_DERIVE_RE, planted) == [
        Path("fake/aliased.py"),
        Path("fake/dotted.py"),
        Path("fake/plain.py"),
    ]


def test_the_scanner_is_reading_a_non_empty_tree():
    """A scanner pointed at nothing passes every assertion below it."""
    assert len(_sources()) > 50, f"source scan collected only {len(_sources())} files"


# ---------------------------------------------------------------------------
# The guards
# ---------------------------------------------------------------------------


def test_emit_event_is_the_only_writer_of_signal_events():
    rel = [h.relative_to(_ROOT).as_posix() for h in _scan(_INSERT_PAT, _read_all())]
    assert rel == ["scout/chains/events.py"], (
        f"{_TABLE} gained a writer outside emit_event: {rel}. Rows inserted "
        "there never reach signal_first_seen, so every migrated consumer "
        "silently under-reports them."
    )


def test_no_module_names_the_events_table_without_an_explicit_reason():
    """The guard that would have caught the un-migrated short-symbol branch.

    Naming the table is how a consumer re-couples itself to retention, whether
    the SQL is a literal or built at runtime. New names must be a decision, not
    an accident.
    """
    sites = _table_literal_sites(_read_all())
    unexpected = {f: ls for f, ls in sites.items() if f not in _LITERAL_ALLOWED}
    assert not unexpected, (
        f"these name {_TABLE} as a string literal without an entry in "
        f"_LITERAL_ALLOWED: {unexpected}. If it derives a first-seen, migrate "
        "it to signal_first_seen; if it is window-bounded and genuinely "
        "exempt, add it with the reason."
    )


def test_the_allowlist_is_not_silently_stale():
    """An entry that no longer matches implies a review that no longer applies.

    Judged against BOTH detectors, because both tests consult this one
    allowlist and they do not see the same files. `_table_literal_sites` wants
    an ast.Constant exactly equal to the table name; `_DERIVE_RE` matches the
    name embedded in a longer SQL string. A file caught only by the regex --
    the ordinary shape, `"... MIN(created_at) FROM signal_events ..."` -- could
    therefore never be allowlisted: adding it silenced the derive guard and
    immediately failed this one as stale. Checking one detector while gating
    on two is the bug; the allowlist is stale only when NEITHER finds the file.
    """
    texts = _read_all()
    seen = set(_table_literal_sites(texts)) | {
        h.relative_to(_ROOT).as_posix() for h in _scan_re(_DERIVE_RE, texts)
    }
    stale = set(_LITERAL_ALLOWED) - seen
    assert (
        not stale
    ), f"allowlist names files that no longer reference {_TABLE}: {stale}"


def test_no_consumer_still_derives_first_seen_from_signal_events():
    allowed = set(_LITERAL_ALLOWED)
    rel = [
        h.relative_to(_ROOT).as_posix()
        for h in _scan_re(_DERIVE_RE, _read_all())
        if h.relative_to(_ROOT).as_posix() not in allowed
    ]
    assert rel == [], (
        f"these still derive first-seen from {_TABLE}: {rel}. That re-couples "
        "them to retention -- shortening it would silently move their derived "
        "minimum forward. Read signal_first_seen instead."
    )
