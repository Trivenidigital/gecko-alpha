"""Structural guards for the derived first-seen substrate.

The substrate is only correct because of two structural facts, neither of which
is visible at any single call site:

1. ``emit_event`` is the ONLY thing that inserts into ``signal_events``. The
   substrate is folded there, in the same transaction. A future direct INSERT
   elsewhere would be invisible to every migrated consumer -- the row would
   exist, lead-time attribution would silently omit it, and nothing would
   error. That is the silent-failure class this substrate was built to remove,
   so reintroducing it via a second writer must fail CI instead.

2. No consumer derives first-seen from ``signal_events`` any more. The whole
   point of option F is that retention stops being the implicit historical
   boundary; one un-migrated consumer keeps that coupling alive for itself.

Both guards scan source text, so each has its own falsifier below: a scanner
that matches nothing passes vacuously and would protect nothing.
"""

from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_TREES = ("scout", "dashboard", "scripts")

# Built by concatenation so this file's own guard text cannot satisfy the
# scanner when it walks a tree that happens to include tests.
_INSERT_PAT = "INSERT INTO " + "signal_events"
_DERIVE_PAT = "MIN(created_at)" + " FROM " + "signal_events"


def _sources() -> list[Path]:
    out: list[Path] = []
    for tree in _TREES:
        d = _ROOT / tree
        if d.is_dir():
            out.extend(p for p in d.rglob("*.py") if "__pycache__" not in p.parts)
    return out


def _scan(pattern: str, texts: dict[Path, str]) -> list[Path]:
    return sorted(p for p, t in texts.items() if pattern in t)


def _read_all() -> dict[Path, str]:
    return {p: p.read_text(encoding="utf-8", errors="replace") for p in _sources()}


def test_the_scanner_actually_sees_a_planted_writer():
    """Falsifier for guard 1. Pins the detector, not just the tree."""
    planted = {
        Path("fake/rogue.py"): f'await conn.execute("""{_INSERT_PAT} (token_id)""")',
        Path("fake/clean.py"): "await conn.execute('SELECT 1')",
    }
    assert _scan(_INSERT_PAT, planted) == [Path("fake/rogue.py")]


def test_the_derive_scanner_actually_sees_a_planted_consumer():
    """Falsifier for guard 2."""
    planted = {
        Path("fake/old.py"): f"SELECT {_DERIVE_PAT} WHERE token_id = ?",
        Path("fake/new.py"): "SELECT MIN(first_seen_at) FROM signal_first_seen",
    }
    assert _scan(_DERIVE_PAT, planted) == [Path("fake/old.py")]


def test_the_scanner_is_reading_a_non_empty_tree():
    """A scanner pointed at nothing passes every assertion below it."""
    srcs = _sources()
    assert len(srcs) > 50, f"source scan collected only {len(srcs)} files"


def test_emit_event_is_the_only_writer_of_signal_events():
    hits = _scan(_INSERT_PAT, _read_all())
    rel = [h.relative_to(_ROOT).as_posix() for h in hits]
    assert rel == ["scout/chains/events.py"], (
        f"signal_events gained a writer outside emit_event: {rel}. Rows inserted "
        "there never reach signal_first_seen, so every migrated consumer silently "
        "under-reports them. Route the write through emit_event, or fold the "
        "substrate in the same transaction as the new INSERT."
    )


def test_no_consumer_still_derives_first_seen_from_signal_events():
    hits = _scan(_DERIVE_PAT, _read_all())
    rel = [h.relative_to(_ROOT).as_posix() for h in hits]
    assert rel == [], (
        f"these still derive first-seen from signal_events: {rel}. That re-couples "
        "them to retention -- shortening it would silently move their derived "
        "minimum forward. Read signal_first_seen instead."
    )
