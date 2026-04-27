"""BL-064 parser tests — pure regex extraction."""

from __future__ import annotations

import pytest

from scout.social.telegram.parser import parse_message


def test_empty_input():
    p = parse_message("")
    assert p.is_empty
    assert p.cashtags == []
    assert p.contracts == []
    assert p.urls == []


def test_none_input():
    p = parse_message(None)
    assert p.is_empty


def test_single_cashtag():
    p = parse_message("Looking at $RIV — solid setup")
    assert p.cashtags == ["RIV"]


def test_cashtag_normalised_uppercase():
    p = parse_message("$wif moon time")
    assert p.cashtags == ["WIF"]


def test_cashtag_dedup():
    p = parse_message("$RIV $RIV $riv all the same")
    assert p.cashtags == ["RIV"]


def test_cashtag_skips_dollar_amounts():
    p = parse_message("RIV at $3M moving to $60M")
    assert "3M" not in p.cashtags  # leading digit excluded by regex
    assert "60M" not in p.cashtags
    assert p.cashtags == []


def test_solana_contract_extraction():
    text = "Someone asked me to look at $RIV: 2bpT3ksMdwdZ6DuHyq3FDUr7HDwvZ5DRZoT1fUPALJaH"
    p = parse_message(text)
    assert p.cashtags == ["RIV"]
    assert len(p.contracts) == 1
    assert p.contracts[0].chain == "solana"
    assert p.contracts[0].address == "2bpT3ksMdwdZ6DuHyq3FDUr7HDwvZ5DRZoT1fUPALJaH"


def test_evm_contract_extraction():
    text = "$ARB on Arbitrum: 0x912CE59144191C1204E64559FE8253a0e49E6548 — strong"
    p = parse_message(text)
    assert len(p.contracts) == 1
    assert p.contracts[0].address == "0x912CE59144191C1204E64559FE8253a0e49E6548"
    # EVM hex CAs default to 'ethereum' tag; resolver can re-attribute
    assert p.contracts[0].chain == "ethereum"


def test_contract_dedup_within_message():
    addr = "2bpT3ksMdwdZ6DuHyq3FDUr7HDwvZ5DRZoT1fUPALJaH"
    text = f"{addr}\nfollow up: {addr} also see {addr}"
    p = parse_message(text)
    assert len(p.contracts) == 1


def test_dexscreener_url_extracts_ca():
    text = (
        "$RIV chart: https://dexscreener.com/solana/2bpT3ksMdwdZ6DuHyq3FDUr7HDwvZ5DRZoT1fUPALJaH"
    )
    p = parse_message(text)
    assert "https://dexscreener.com/solana/2bpT3ksMdwdZ6DuHyq3FDUr7HDwvZ5DRZoT1fUPALJaH" in p.urls
    assert any(c.address == "2bpT3ksMdwdZ6DuHyq3FDUr7HDwvZ5DRZoT1fUPALJaH" for c in p.contracts)


def test_birdeye_url_extracts_ca():
    text = "https://birdeye.so/token/2bpT3ksMdwdZ6DuHyq3FDUr7HDwvZ5DRZoT1fUPALJaH?chain=solana"
    p = parse_message(text)
    assert any(c.address == "2bpT3ksMdwdZ6DuHyq3FDUr7HDwvZ5DRZoT1fUPALJaH" for c in p.contracts)


def test_multi_token_message():
    text = "$WIF and $POPCAT both pumping\nWIF: EKpQGSJtjMFqKZ9KQanSqYXRcF8fBopzLHYxdM65zcjm\nPOPCAT: 7GCihgDB8fe6KNjn2MYtkzZcRjQy3t9GHdC8uHYmW2hr"
    p = parse_message(text)
    assert "WIF" in p.cashtags
    assert "POPCAT" in p.cashtags
    assert len(p.contracts) == 2


def test_emoji_only_no_signal():
    p = parse_message("🚀🚀🚀")
    assert p.is_empty


def test_multiline_message():
    text = """
    GM!
    $RIV looks good here
    https://dexscreener.com/solana/2bpT3ksMdwdZ6DuHyq3FDUr7HDwvZ5DRZoT1fUPALJaH
    Strong setup.
    """
    p = parse_message(text)
    assert p.cashtags == ["RIV"]
    assert len(p.contracts) == 1


def test_no_cashtags_no_contracts():
    p = parse_message("Just chatting about the market today, nothing specific.")
    assert p.is_empty


def test_cashtag_with_underscore():
    p = parse_message("$my_token might pump")
    assert "MY_TOKEN" in p.cashtags
