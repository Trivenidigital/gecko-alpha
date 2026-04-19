"""Tests for build_combo_key (spec §4.4)."""

from __future__ import annotations

from scout.trading.combo_key import build_combo_key


def test_single_signal_no_extras():
    assert build_combo_key("volume_spike", None) == "volume_spike"
    assert build_combo_key("volume_spike", []) == "volume_spike"


def test_signal_type_plus_one_extra():
    assert (
        build_combo_key("first_signal", ["momentum_ratio"])
        == "first_signal+momentum_ratio"
    )


def test_extras_sorted_alphabetically_for_pick():
    # When extras=['zzz', 'aaa', 'mmm'], alphabetically-first is 'aaa'.
    assert build_combo_key("first_signal", ["zzz", "aaa", "mmm"]) == "aaa+first_signal"


def test_output_is_sorted():
    # signal_type='xray', extra='apple' => 'apple+xray' (sorted output).
    assert build_combo_key("xray", ["apple"]) == "apple+xray"


def test_signal_type_dedup_from_extras():
    # If signals includes signal_type itself, don't double-count.
    assert build_combo_key("volume_spike", ["volume_spike"]) == "volume_spike"


def test_triple_truncates_to_pair_and_logs(capsys):
    """D2: pair cap — 3+ signals collapse to 2 and emit `combo_key_truncated` log."""
    import structlog

    # Capture structlog output via its default stdout renderer.
    result = build_combo_key("first_signal", ["momentum_ratio", "vol_acceleration"])
    # Kept: alphabetically-first of extras = 'momentum_ratio'
    assert result == "first_signal+momentum_ratio"
    out = capsys.readouterr().out + capsys.readouterr().err
    assert (
        "combo_key_truncated" in out
    ), f"expected 'combo_key_truncated' log event; stdout/stderr was:\n{out}"


def test_pair_cap_keeps_alphabetically_first():
    # extras sorted: ['aaa', 'bbb', 'ccc']; kept='aaa'; dropped=['bbb','ccc'].
    assert build_combo_key("zulu", ["ccc", "bbb", "aaa"]) == "aaa+zulu"


def test_none_signals_equivalent_to_empty():
    assert build_combo_key("trending_catch", None) == "trending_catch"


def test_signal_type_always_included():
    # Even when extras sort before signal_type, signal_type is in the output.
    result = build_combo_key("zzz", ["aaa"])
    assert "zzz" in result
    assert "aaa" in result
