"""SIG-10 trust-weighted paper-trade sizing helpers.

Resolves an opening signal's *trust tier* from the signal-trust registry
(``docs/superpowers/registries/signal_trust_registry.v1.json``) and maps that
tier onto a size multiplier applied to the paper-trade notional at open time.

Paper-only and flag-gated (``PAPER_TRUST_SIZING_ENABLED``). The registry file
is stamped ``not_for_sizing`` for LIVE/production paths; this module consumes
its ``maturity_state`` *as data* to drive an explicit, operator-opted-in paper
sizing policy that makes ``would_be_live`` re-analysis reflect realistic per-
tier sizing. It never gates live execution — SIG-03 dispatch quarantine still
supersedes it for ``narrative_prediction`` / ``tg_social``.

Kept import-light on purpose (``json`` / ``pathlib`` / ``structlog`` only) so
the resolver can be unit-tested on Windows, where the aiohttp-dependent
trading modules cannot import (OPENSSL_Uplink).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

import structlog

from scout.config import _REPO_ROOT

if TYPE_CHECKING:  # avoid importing Settings at runtime (duck-typed arg)
    from scout.config import Settings

log = structlog.get_logger()

REGISTRY_RELATIVE_PATH = "docs/superpowers/registries/signal_trust_registry.v1.json"

# The three trust tiers SIG-10 sizes on, and how the registry's five
# maturity_states collapse onto them. A signal_type absent from the registry —
# or carrying a maturity_state not listed here — resolves to
# ``TIER_UNKNOWN_DEFAULT`` (experimental), logged by the caller.
TIER_TRUSTED = "trusted"
TIER_EXPERIMENTAL = "experimental"
TIER_NON_TRADABLE = "non_tradable"
TIER_UNKNOWN_DEFAULT = TIER_EXPERIMENTAL

MATURITY_STATE_TO_TIER: dict[str, str] = {
    "trusted_experimental": TIER_TRUSTED,
    "context_only": TIER_EXPERIMENTAL,
    "data_insufficient": TIER_EXPERIMENTAL,
    "quarantined": TIER_NON_TRADABLE,
    "retire_candidate": TIER_NON_TRADABLE,
}

# Fallback multiplier used only if a resolved tier is missing from the
# operator's PAPER_TRUST_SIZE_MULTIPLIERS map (misconfiguration guard).
_DEFAULT_MULTIPLIER = 0.5

# {registry_path_str: (mtime_ns, {signal_type: tier})} — re-parses only when
# the file changes so operator edits take effect without a process restart.
_TIER_CACHE: dict[str, tuple[int, dict[str, str]]] = {}


def _default_registry_path() -> Path:
    return _REPO_ROOT / REGISTRY_RELATIVE_PATH


def _load_signal_type_tiers(registry_path: Path) -> dict[str, str]:
    """Read the registry and return ``{signal_type: tier}`` (mtime-cached).

    Fail-soft: any stat/read/parse error returns an empty map (every signal
    then resolves to the unknown-tier default) and is logged.
    """
    try:
        mtime = registry_path.stat().st_mtime_ns
    except OSError:
        log.warning("trust_registry_unavailable", registry_path=str(registry_path))
        return {}

    key = str(registry_path)
    cached = _TIER_CACHE.get(key)
    if cached is not None and cached[0] == mtime:
        return cached[1]

    mapping: dict[str, str] = {}
    try:
        doc = json.loads(registry_path.read_text(encoding="utf-8"))
        for entry in doc.get("entries", []) or []:
            if not isinstance(entry, dict):
                continue
            signal_type = entry.get("signal_type")
            maturity_state = entry.get("maturity_state")
            if isinstance(signal_type, str) and isinstance(maturity_state, str):
                mapping[signal_type] = MATURITY_STATE_TO_TIER.get(
                    maturity_state, TIER_UNKNOWN_DEFAULT
                )
    except (OSError, ValueError) as exc:
        log.warning(
            "trust_registry_parse_failed",
            registry_path=str(registry_path),
            error=str(exc),
        )
        mapping = {}

    _TIER_CACHE[key] = (mtime, mapping)
    return mapping


def resolve_trust_tier(
    signal_type: str, *, registry_path: Path | None = None
) -> tuple[str, bool]:
    """Return ``(tier, known)`` for *signal_type*.

    ``known`` is ``False`` when *signal_type* is absent from the registry; the
    returned tier is then ``TIER_UNKNOWN_DEFAULT``.
    """
    path = registry_path or _default_registry_path()
    tiers = _load_signal_type_tiers(path)
    tier = tiers.get(signal_type)
    if tier is None:
        return TIER_UNKNOWN_DEFAULT, False
    return tier, True


def resolve_paper_trust_size(
    signal_type: str,
    settings: "Settings",
    *,
    registry_path: Path | None = None,
) -> tuple[str, float]:
    """Resolve ``(tier, multiplier)`` for a signal at open time.

    ``tier`` comes from the registry maturity_state (unknown signal_type ->
    experimental default, logged). ``multiplier`` comes from
    ``settings.paper_trust_size_multipliers_map`` keyed by tier; a tier missing
    from that map falls back to ``_DEFAULT_MULTIPLIER``.
    """
    tier, known = resolve_trust_tier(signal_type, registry_path=registry_path)
    multipliers = settings.paper_trust_size_multipliers_map
    multiplier = multipliers.get(tier, _DEFAULT_MULTIPLIER)
    if not known:
        log.info(
            "trust_tier_unknown_defaulted",
            signal_type=signal_type,
            defaulted_tier=tier,
            multiplier=multiplier,
        )
    return tier, multiplier
