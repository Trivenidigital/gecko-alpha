"""Resolution-status classifier — makes the narrative resolve outcome legible.

cashtag_only (composition, expected-unresolvable) vs ca_resolved vs
ca_unresolved. Pure function (no I/O), so it runs locally.
"""

from scout.api.narrative_resolver import classify_resolution_status


def test_cashtag_only_when_no_ca():
    assert classify_resolution_status(extracted_ca=None, resolved_coin_id=None) == "cashtag_only"
    assert classify_resolution_status(extracted_ca="", resolved_coin_id=None) == "cashtag_only"
    assert classify_resolution_status(extracted_ca="   ", resolved_coin_id=None) == "cashtag_only"


def test_ca_resolved_when_ca_and_coin_id():
    assert (
        classify_resolution_status(extracted_ca="0xabc", resolved_coin_id="the-black-bull")
        == "ca_resolved"
    )


def test_ca_unresolved_when_ca_present_but_no_coin_id():
    assert classify_resolution_status(extracted_ca="0xabc", resolved_coin_id=None) == "ca_unresolved"
    assert classify_resolution_status(extracted_ca="0xabc", resolved_coin_id="") == "ca_unresolved"


def test_cashtag_only_takes_precedence_over_stray_coin_id():
    # no CA -> cashtag_only even if a coin_id somehow present (CA is the gate)
    assert classify_resolution_status(extracted_ca=None, resolved_coin_id="x") == "cashtag_only"
