from scout.trading.actionability import evaluate_actionability_v1


def _decision(signal_type, *, signal_data=None, signal_combo=None):
    return evaluate_actionability_v1(
        signal_type=signal_type,
        signal_data=signal_data or {},
        signal_combo=signal_combo or signal_type,
        conviction_stack=0,
    )


def test_narrative_prediction_passes_with_10_50m_mcap():
    d = _decision("narrative_prediction", signal_data={"mcap": 20_000_000})
    assert d.actionable is True
    assert d.reason == "v1_pass_core_signal_mcap_10_50m"
    assert d.version == "v1"


def test_chain_completed_passes_with_over_50m_mcap():
    d = _decision("chain_completed", signal_data={"market_cap": 75_000_000})
    assert d.actionable is True
    assert d.reason == "v1_pass_core_signal_mcap_50m_plus"


def test_chain_completed_missing_mcap_uses_explicit_exception():
    d = _decision("chain_completed", signal_data={})
    assert d.actionable is True
    assert d.reason == "v1_pass_chain_completed_mcap_unknown_exception"


def test_volume_spike_passes_with_10_50m_mcap():
    d = _decision("volume_spike", signal_data={"market_cap_usd": 12_000_000})
    assert d.actionable is True


def test_losers_contrarian_is_non_actionable_by_default():
    d = _decision("losers_contrarian", signal_data={"mcap": 8_000_000})
    assert d.actionable is False
    assert d.reason == "v1_block_losers_contrarian_exploratory"


def test_trending_catch_is_non_actionable_by_default():
    d = _decision("trending_catch", signal_data={"mcap": 80_000_000})
    assert d.actionable is False
    assert d.reason == "v1_block_trending_catch_low_n"


def test_tg_social_is_non_actionable_by_default():
    d = _decision("tg_social", signal_data={"mcap": 20_000_000})
    assert d.actionable is False
    assert d.reason == "v1_block_tg_social_low_n"


def test_gainers_early_blocks_5_to_10m():
    d = _decision("gainers_early", signal_data={"mcap": 7_000_000})
    assert d.actionable is False
    assert d.reason == "v1_block_gainers_early_mcap_5_10m"


def test_gainers_early_blocks_confluence_3():
    d = _decision(
        "gainers_early",
        signal_data={"mcap": 80_000_000},
        signal_combo="gainers_early+cg_trending_rank+momentum_ratio",
    )
    assert d.actionable is False
    assert d.reason == "v1_block_gainers_early_confluence_3"


def test_gainers_early_blocks_conviction_stack_3_when_combo_is_pair_capped():
    d = evaluate_actionability_v1(
        signal_type="gainers_early",
        signal_data={"mcap": 80_000_000},
        signal_combo="gainers_early+momentum_ratio",
        conviction_stack=3,
    )
    assert d.actionable is False
    assert d.reason == "v1_block_gainers_early_confluence_3"


def test_gainers_early_over_50m_passes_when_confluence_below_3():
    d = _decision("gainers_early", signal_data={"mcap": 80_000_000})
    assert d.actionable is True
    assert d.reason == "v1_pass_gainers_early_mcap_50m_plus"


def test_gainers_early_10_to_50m_blocks_as_observe():
    d = _decision("gainers_early", signal_data={"mcap": 20_000_000})
    assert d.actionable is False
    assert d.reason == "v1_block_gainers_early_mcap_10_50m_observe"


def test_unknown_mcap_blocks_explicitly_for_non_chain_signal():
    d = _decision("narrative_prediction", signal_data={})
    assert d.actionable is False
    assert d.reason == "v1_block_missing_mcap"


def test_mcap_extraction_continues_after_invalid_candidate_key():
    d = _decision(
        "volume_spike",
        signal_data={"mcap": "unknown", "market_cap_usd": 12_000_000},
    )
    assert d.actionable is True
    assert d.reason == "v1_pass_core_signal_mcap_10_50m"


def test_unknown_signal_blocks_explicitly():
    d = _decision("new_signal", signal_data={"mcap": 20_000_000})
    assert d.actionable is False
    assert d.reason == "v1_block_unknown_signal_type"
