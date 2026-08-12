"""W4 — evaluator check order and gate-version fingerprint binding.

Binding criterion 3 lives here: changing any TG_SHADOW_* threshold, the
snapshot schema version, or a bound module hash must produce a DIFFERENT
gate_version, while an unrelated setting must not. A fingerprint that misses
a threshold silently merges two rule iterations into one cohort; one that
catches unrelated settings splits cohorts for nothing.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from scout.social.telegram import shadow, snapshot as snapshot_module
from scout.social.telegram.shadow import (
    GATE_VERSION_PREFIX,
    SHADOW_REASONS,
    ShadowDecision,
    build_features_json,
    compute_config_fingerprint,
    current_gate_version,
    evaluate_tg_actionability_shadow,
    gate_version_for,
    identity_class_for,
)

_NOW = datetime(2026, 8, 12, 12, 0, tzinfo=timezone.utc)


def _signal(**overrides) -> dict:
    defaults = dict(
        id=1,
        token_id="some-coin",
        symbol="TOK",
        contract_address="0xabc",
        chain="solana",
        mcap_at_sighting=250_000.0,
        resolution_state="RESOLVED",
        source_channel_handle="@gem",
        created_at=_NOW.isoformat(),
        resolution_snapshot_json=None,
    )
    defaults.update(overrides)
    return defaults


def _snapshot(**overrides) -> dict:
    defaults = dict(
        snapshot_schema_version=1,
        price_usd=1.0,
        volume_24h_usd=1000.0,
        age_days=5.0,
        liquidity_usd=None,
        safety_pass=True,
        safety_check_completed=True,
        safety_skipped_no_ca=False,
    )
    defaults.update(overrides)
    return defaults


def _features(**overrides) -> dict:
    defaults = dict(
        history_eligible_distinct_clusters=25,
        history_priceable_coverage_rate=0.9,
        event_duplicate_rank_in_cluster=1,
    )
    defaults.update(overrides)
    return defaults


# ---------------------------------------------------------------------------
# Evaluator
# ---------------------------------------------------------------------------


def test_all_inputs_clean_passes(settings_factory):
    decision = evaluate_tg_actionability_shadow(
        _signal(), _snapshot(), _features(), settings_factory()
    )
    assert decision == ShadowDecision(True, "shadow_pass")


_ORDER_CASES = [
    # 1. snapshot missing wins over everything else being broken too
    ({"mcap_at_sighting": None}, None, {}, {}, "shadow_block_snapshot_missing"),
    # 2. mcap missing wins over a missing CA
    (
        {"mcap_at_sighting": None, "contract_address": None},
        {},
        {},
        {},
        "shadow_block_missing_mcap",
    ),
    ({"mcap_at_sighting": 0.0}, {}, {}, {}, "shadow_block_missing_mcap"),
    # 3. identity wins over failed safety
    (
        {"contract_address": None},
        {"safety_pass": False},
        {},
        {},
        "shadow_block_identity_unpriceable_class",
    ),
    (
        {"token_id": "dex:robinhood:0xff"},
        {},
        {},
        {"TG_SHADOW_UNPRICEABLE_IDENTITY_CLASSES": ["dex:robinhood"]},
        "shadow_block_identity_unpriceable_class",
    ),
    # 4. safety wins over an out-of-band mcap
    (
        {"mcap_at_sighting": 1.0e12},
        {"safety_pass": False},
        {},
        {},
        "shadow_block_safety_not_passed",
    ),
    ({}, {"safety_check_completed": False}, {}, {}, "shadow_block_safety_not_passed"),
    # 5. mcap band wins over caller stats
    (
        {"mcap_at_sighting": 9_999.0},
        {},
        {"history_eligible_distinct_clusters": 0},
        {},
        "shadow_block_mcap_band",
    ),
    (
        {"mcap_at_sighting": 500_000_001.0},
        {},
        {},
        {},
        "shadow_block_mcap_band",
    ),
    # 6. liquidity wins over caller stats, only when required
    (
        {},
        {"liquidity_usd": None},
        {"history_eligible_distinct_clusters": 0},
        {"TG_SHADOW_REQUIRE_LIQUIDITY": True},
        "shadow_block_liquidity_unknown",
    ),
    # 7. min-N wins over coverage
    (
        {},
        {},
        {
            "history_eligible_distinct_clusters": 9,
            "history_priceable_coverage_rate": 0.0,
        },
        {},
        "shadow_block_caller_insufficient_n",
    ),
    # 8. coverage wins over duplicate
    (
        {},
        {},
        {
            "history_priceable_coverage_rate": 0.49,
            "event_duplicate_rank_in_cluster": 3,
        },
        {},
        "shadow_block_caller_quality",
    ),
    # 9. duplicate
    ({}, {}, {"event_duplicate_rank_in_cluster": 2}, {}, "shadow_block_duplicate_call"),
]


@pytest.mark.parametrize(
    "signal_kw, snapshot_kw, feature_kw, settings_kw, expected", _ORDER_CASES
)
def test_reason_order_is_the_contract(
    signal_kw, snapshot_kw, feature_kw, settings_kw, expected, settings_factory
):
    """Each case makes MORE than one check fail; the asserted reason is the
    earlier one. That is what pins the order rather than merely the checks."""
    snap = None if snapshot_kw is None else _snapshot(**snapshot_kw)
    decision = evaluate_tg_actionability_shadow(
        _signal(**signal_kw),
        snap,
        _features(**feature_kw),
        settings_factory(**settings_kw),
    )
    assert decision.reason == expected
    assert decision.actionable is False


def test_reason_enum_matches_what_the_evaluator_can_actually_emit(settings_factory):
    """No dead enum members, no unlisted reason. Both directions matter: an
    unlisted reason silently vanishes from the distribution, and a dead member
    invites analysis of a bucket that can never fill."""
    settings = settings_factory()
    emitted = {
        evaluate_tg_actionability_shadow(
            _signal(), _snapshot(), _features(), settings
        ).reason
    }
    for signal_kw, snapshot_kw, feature_kw, settings_kw, _ in _ORDER_CASES:
        snap = None if snapshot_kw is None else _snapshot(**snapshot_kw)
        emitted.add(
            evaluate_tg_actionability_shadow(
                _signal(**signal_kw),
                snap,
                _features(**feature_kw),
                settings_factory(**settings_kw),
            ).reason
        )
    assert emitted == set(SHADOW_REASONS)
    assert len(set(SHADOW_REASONS)) == len(SHADOW_REASONS)


def test_missing_feature_keys_block_rather_than_raise(settings_factory):
    """A provider that omits a field must produce a visible block. Raising
    would be swallowed by the live hook's containment and vanish."""
    decision = evaluate_tg_actionability_shadow(
        _signal(), _snapshot(), {}, settings_factory()
    )
    assert decision.reason == "shadow_block_caller_insufficient_n"


def test_mcap_band_boundaries_are_inclusive(settings_factory):
    settings = settings_factory()
    for mcap in (settings.TG_SHADOW_MCAP_MIN_USD, settings.TG_SHADOW_MCAP_MAX_USD):
        decision = evaluate_tg_actionability_shadow(
            _signal(mcap_at_sighting=mcap), _snapshot(), _features(), settings
        )
        assert decision.reason == "shadow_pass", mcap


@pytest.mark.parametrize(
    "token_id, expected",
    [
        ("bitcoin", "coingecko"),
        ("dex:solana:So111", "dex:solana"),
        ("dex:robinhood:0xabc", "dex:robinhood"),
        ("dex:", "dex:unknown"),
    ],
)
def test_identity_class_keeps_strata_separate(token_id, expected):
    assert identity_class_for({"token_id": token_id}) == expected


def test_default_unpriceable_class_list_is_empty(settings_factory):
    """Populated only after a Stage C PQ verdict — pre-populating it would
    encode a guess as policy."""
    assert settings_factory().TG_SHADOW_UNPRICEABLE_IDENTITY_CLASSES == []


# ---------------------------------------------------------------------------
# Fingerprint (binding criterion 3)
# ---------------------------------------------------------------------------


def test_gate_version_shape(settings_factory, fixture_caller_feature_provider):
    provider = fixture_caller_feature_provider()
    gate_version, fingerprint = current_gate_version(settings_factory(), provider)
    assert gate_version == GATE_VERSION_PREFIX + fingerprint[:16]
    assert len(fingerprint) == 64
    assert gate_version == gate_version_for(fingerprint)


def test_fingerprint_is_stable_across_calls(
    settings_factory, fixture_caller_feature_provider
):
    provider = fixture_caller_feature_provider()
    settings = settings_factory()
    assert compute_config_fingerprint(settings, provider) == compute_config_fingerprint(
        settings, provider
    )


@pytest.mark.parametrize(
    "override",
    [
        {"TG_SHADOW_MCAP_MIN_USD": 20_000.0},
        {"TG_SHADOW_MCAP_MAX_USD": 400_000_000.0},
        {"TG_SHADOW_REQUIRE_LIQUIDITY": True},
        {"TG_SHADOW_MIN_CALLER_COVERAGE": 0.6},
        {"TG_CALLER_MIN_ELIGIBLE_CLUSTERS": 12},
        {"TG_SHADOW_UNPRICEABLE_IDENTITY_CLASSES": ["dex:robinhood"]},
        {"TG_SHADOW_LAG_THRESHOLD_MIN": 90},
        {"TG_SHADOW_SCAN_CADENCE_MIN": 15},
    ],
)
def test_any_shadow_threshold_change_moves_the_gate_version(
    override, settings_factory, fixture_caller_feature_provider
):
    provider = fixture_caller_feature_provider()
    base, _ = current_gate_version(settings_factory(), provider)
    changed, _ = current_gate_version(settings_factory(**override), provider)
    assert changed != base, override


def test_unrelated_setting_change_does_not_move_the_gate_version(
    settings_factory, fixture_caller_feature_provider
):
    """Unrelated commits must not split cohorts — only the decision-producing
    surface is bound."""
    provider = fixture_caller_feature_provider()
    base, _ = current_gate_version(settings_factory(), provider)
    other, _ = current_gate_version(
        settings_factory(TG_SOCIAL_MAX_OPEN_TRADES=9, PAPER_MOONSHOT_ENABLED=True),
        provider,
    )
    assert other == base


def test_enabled_flag_does_not_move_the_gate_version(
    settings_factory, fixture_caller_feature_provider
):
    """Disable/enable must RESUME the same generation. If the flag were in the
    fingerprint, re-enabling would compute a different gate_version and orphan
    the existing generation's rows."""
    provider = fixture_caller_feature_provider()
    off, _ = current_gate_version(settings_factory(TG_SHADOW_ENABLED=False), provider)
    on, _ = current_gate_version(settings_factory(TG_SHADOW_ENABLED=True), provider)
    assert off == on


def test_snapshot_schema_version_change_moves_the_gate_version(
    monkeypatch, settings_factory, fixture_caller_feature_provider
):
    provider = fixture_caller_feature_provider()
    base, _ = current_gate_version(settings_factory(), provider)
    monkeypatch.setattr(snapshot_module, "SNAPSHOT_SCHEMA_VERSION", 2)
    changed, _ = current_gate_version(settings_factory(), provider)
    assert changed != base


def test_snapshot_producer_version_change_moves_the_gate_version(
    monkeypatch, settings_factory, fixture_caller_feature_provider
):
    provider = fixture_caller_feature_provider()
    base, _ = current_gate_version(settings_factory(), provider)
    monkeypatch.setattr(
        snapshot_module, "SNAPSHOT_PRODUCER_SEMANTIC_VERSION", "tg-snap-v2"
    )
    changed, _ = current_gate_version(settings_factory(), provider)
    assert changed != base


def test_snapshot_module_hash_change_moves_the_gate_version(
    monkeypatch, settings_factory, fixture_caller_feature_provider
):
    """The mechanical half of the binding: an edit that changes what a
    snapshot MEANS, without touching a version or a threshold, still splits."""
    provider = fixture_caller_feature_provider()
    base, _ = current_gate_version(settings_factory(), provider)
    monkeypatch.setattr(snapshot_module, "module_source_hash", lambda: "f" * 64)
    changed, _ = current_gate_version(settings_factory(), provider)
    assert changed != base


def test_evaluator_module_hash_change_moves_the_gate_version(
    monkeypatch, settings_factory, fixture_caller_feature_provider
):
    provider = fixture_caller_feature_provider()
    base, _ = current_gate_version(settings_factory(), provider)
    monkeypatch.setattr(shadow, "module_source_hash", lambda: "e" * 64)
    changed, _ = current_gate_version(settings_factory(), provider)
    assert changed != base


def test_provider_module_hash_change_moves_the_gate_version(
    settings_factory, fixture_caller_feature_provider
):
    settings = settings_factory()
    base, _ = current_gate_version(
        settings, fixture_caller_feature_provider(module_source_hash="a" * 64)
    )
    changed, _ = current_gate_version(
        settings, fixture_caller_feature_provider(module_source_hash="b" * 64)
    )
    assert changed != base


def test_provider_semantic_version_change_moves_the_gate_version(
    settings_factory, fixture_caller_feature_provider
):
    settings = settings_factory()
    base, _ = current_gate_version(settings, fixture_caller_feature_provider())
    changed, _ = current_gate_version(
        settings, fixture_caller_feature_provider(semantic_version="tg-caller-v2")
    )
    assert changed != base


def test_feature_schema_change_moves_the_gate_version(
    settings_factory, fixture_caller_feature_provider
):
    settings = settings_factory()
    base, _ = current_gate_version(settings, fixture_caller_feature_provider())
    changed, _ = current_gate_version(
        settings,
        fixture_caller_feature_provider(
            schema=[
                ("history_eligible_distinct_clusters", "int"),
                ("history_priceable_coverage_rate", "float"),
                ("event_duplicate_rank_in_cluster", "int"),
                ("history_corroborating_channels", "int"),
            ]
        ),
    )
    assert changed != base


def test_identity_class_list_order_is_not_a_policy_change(
    settings_factory, fixture_caller_feature_provider
):
    """An operator re-ordering the list in `.env` changed nothing about the
    rule, so it must not split the cohort."""
    provider = fixture_caller_feature_provider()
    a, _ = current_gate_version(
        settings_factory(TG_SHADOW_UNPRICEABLE_IDENTITY_CLASSES=["a", "b"]), provider
    )
    b, _ = current_gate_version(
        settings_factory(TG_SHADOW_UNPRICEABLE_IDENTITY_CLASSES=["b", "a"]), provider
    )
    assert a == b


# ---------------------------------------------------------------------------
# features_json
# ---------------------------------------------------------------------------


def test_features_json_carries_the_full_digest(
    settings_factory, fixture_caller_feature_provider
):
    """`gate_version` shows 16 chars; the authority on which config produced a
    decision is the full digest, and it has to be IN the row."""
    import json

    provider = fixture_caller_feature_provider()
    gate_version, fingerprint = current_gate_version(settings_factory(), provider)
    body = json.loads(
        build_features_json(
            config_fingerprint=fingerprint,
            gate_version=gate_version,
            decision_as_of=_NOW.isoformat(),
            features=_features(),
            snapshot=_snapshot(),
        )
    )
    assert body["config_fingerprint"] == fingerprint
    assert len(body["config_fingerprint"]) == 64
    assert body["gate_version"] == gate_version
    assert body["decision_as_of"] == _NOW.isoformat()


def test_features_json_is_canonical_and_wall_clock_free():
    """Two builds of the same decision are byte-identical, and today's date
    appears nowhere — if `now` leaked in, catch-up could never reproduce a
    live write. `decision_as_of` is deliberately an old fixed date so this
    stays true on every future run rather than only on the day it was written.
    """
    old = datetime(2020, 1, 1, tzinfo=timezone.utc).isoformat()
    kwargs = dict(
        config_fingerprint="f" * 64,
        gate_version="tg-shadow-v1+ffffffffffffffff",
        decision_as_of=old,
        features=_features(),
        snapshot=_snapshot(),
    )
    first = build_features_json(**kwargs)
    later = build_features_json(**kwargs)
    assert first == later
    assert datetime.now(timezone.utc).strftime("%Y-%m-%d") not in first
