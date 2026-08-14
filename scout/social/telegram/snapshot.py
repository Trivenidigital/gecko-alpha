"""Durable resolution snapshot for TG shadow Stage A.

The shadow decision has to be reproducible after the in-memory
``ResolvedToken`` is gone — catch-up after a disabled window, and restart
after a crash between the signal INSERT and the ``tg_act_shadow`` write, both
re-decide rows whose resolver facts no longer exist anywhere in the process.
Stage A forbids new API calls, so the facts are captured at the signal INSERT
boundary instead and read back from there. Nothing may ever refetch a missing
historical decision input: that would decide a past call on today's prices.

This lives in its own module rather than inside ``listener.py`` so its source
hash is bindable into the gate fingerprint. Changing what a snapshot MEANS
without renaming a field or moving a threshold still has to split the cohort,
and hashing all of ``listener.py`` would split it on every unrelated listener
edit instead.
"""

from __future__ import annotations

import hashlib
import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from scout.social.telegram.models import ResolvedToken

# Bumped when the snapshot's FIELD SET or field semantics change. Participates
# in the gate fingerprint, so a bump starts a new generation.
SNAPSHOT_SCHEMA_VERSION = 1

# Bumped when the PRODUCER changes behaviour without changing the schema
# (e.g. a field starts being sourced differently). Declared separately from
# the module hash so an intentional semantic change is legible in review
# rather than only visible as a digest that moved.
SNAPSHOT_PRODUCER_SEMANTIC_VERSION = "tg-snap-v1"


def canonical_json_dumps(payload: Any) -> str:
    """Canonical JSON: sorted keys, no incidental whitespace, floats via repr.

    Shared with the gate fingerprint so a snapshot and the fingerprint that
    binds it cannot disagree about what "the same document" means.

    ``json`` serializes floats through ``float.__repr__``, the shortest string
    that round-trips — deliberately NOT ``Decimal`` normalization, which reads
    ambient decimal context and has produced a real hash collision in this
    codebase before. ``allow_nan=False`` refuses NaN/Infinity: those are
    Python-only extensions to JSON and would produce a document no other
    consumer can read.
    """
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
        ensure_ascii=False,
    )


def build_resolution_snapshot(token: ResolvedToken) -> str:
    """Serialize the decision-time resolver facts for one resolved token.

    A value the resolver could not supply is recorded as JSON ``null``, which
    is distinguishable from an ABSENT key: absent means "this schema version
    predates the field", null means "the field exists and was unknown".

    ``liquidity_usd`` is populated for DexScreener-resolved tokens, whose
    resolution response already carried it (the resolver reads it to pick the
    deepest pair). CG-resolved tokens record ``null`` because the CG response
    has no equivalent field — that is a real per-source availability fact, not
    a placeholder, and the evaluator's liquidity check is deliberately OFF by
    default so a source lacking it cannot collapse the whole population into
    one reason.
    """
    return canonical_json_dumps(
        {
            "snapshot_schema_version": SNAPSHOT_SCHEMA_VERSION,
            "price_usd": token.price_usd,
            "volume_24h_usd": token.volume_24h_usd,
            "age_days": token.age_days,
            "liquidity_usd": token.liquidity_usd,
            "safety_pass": token.safety_pass,
            "safety_check_completed": token.safety_check_completed,
            "safety_skipped_no_ca": token.safety_skipped_no_ca,
        }
    )


def parse_resolution_snapshot(text: str | None) -> dict[str, Any] | None:
    """Read a persisted snapshot back.

    Returns ``None`` for missing, blank, malformed, or non-object payloads —
    the evaluator turns that into ``shadow_block_snapshot_missing``, so an
    unreadable snapshot appears in the reason distribution instead of being
    silently skipped.
    """
    if not text or not text.strip():
        return None
    try:
        parsed = json.loads(text)
    except (ValueError, TypeError):
        return None
    if not isinstance(parsed, dict):
        return None
    return parsed


def hash_module_source(path: Path) -> str:
    """SHA-256 of a module's source with line endings normalized to ``\\n``.

    Shared by every module whose source is bound into the gate fingerprint. A
    CRLF working copy and an LF production checkout are the SAME commit and
    must hash identically, or a locally recomputed ``gate_version`` can never
    match production's and replay verification becomes impossible. Separated
    from `module_source_hash` so the invariance is directly testable —
    ``__file__`` cannot be varied inside a test.
    """
    return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


@lru_cache(maxsize=1)
def module_source_hash() -> str:
    """SHA-256 of this module's source, for the gate fingerprint.

    Read from the ``.py`` file rather than derived from the imported objects:
    the point is to notice edits that change extraction semantics without
    touching any declared version or threshold.

    Line endings normalized to ``\\n`` first: a CRLF checkout and an LF checkout
    of the SAME commit must produce the same gate_version, or a locally
    recomputed fingerprint can never match production's.
    """
    return hash_module_source(Path(__file__))
