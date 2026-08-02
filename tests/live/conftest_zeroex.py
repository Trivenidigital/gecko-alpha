"""Shared 0x AllowanceHolder fixture — REAL mainnet calldata, Gecko taker.

The calldata below was returned by ``/swap/allowance-holder/quote`` on 2026-08-02
for 0.1 WETH → USDC on chainId 1, with ``taker`` set to the Gecko-owned UNFUNDED
test address. It is stored as a fixture rather than fetched at test time so the
suite is hermetic, and it is real rather than synthetic so the decoder is proven
against bytes 0x actually produced.

The earlier fixture used a funded third-party address as taker. That was wrong:
0x builds calldata and reports ``issues.allowance`` / ``issues.balance`` FOR THE
TAKER, so a third-party address made those fields describe somebody else's wallet.
With the Gecko address the balance shortfall is visible, which is the truth.
"""

from __future__ import annotations

import json
from pathlib import Path

#: Gecko-owned, UNFUNDED, generated 2026-08-02. Never funded, never used to sign.
GECKO_TEST_TAKER = "0x2db703e30c186474b43fa1dbf004655160e7ef42"

ALLOWANCE_HOLDER_ADDRESS = "0x0000000000001ff3684f28c67538d4d072c22734"
SETTLER_ADDRESS = "0x0889e9327b98d7d1be3c301a4585ff3330502c9a"
WETH = "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2"
USDC = "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"

SELL_AMOUNT = 100000000000000000
BUY_AMOUNT = 187886590
MIN_BUY_AMOUNT = 186005090
QUOTE_BLOCK = 25670500

_FIXTURE = Path(__file__).parent / "fixtures" / "zeroex_allowance_holder_calldata.txt"


def real_calldata() -> str:
    return _FIXTURE.read_text(encoding="utf-8").strip()


def allowance_holder_response(**over) -> dict:
    """The reviewed response shape, with real calldata."""
    payload = {
        "liquidityAvailable": True,
        "transaction": {
            "to": ALLOWANCE_HOLDER_ADDRESS,
            "data": real_calldata(),
            "gas": "300000",
            "gasPrice": "73673518",
            "value": "0",
        },
        "sellToken": WETH,
        "buyToken": USDC,
        "sellAmount": str(SELL_AMOUNT),
        "buyAmount": str(BUY_AMOUNT),
        "minBuyAmount": str(MIN_BUY_AMOUNT),
        "allowanceTarget": ALLOWANCE_HOLDER_ADDRESS,
        "blockNumber": str(QUOTE_BLOCK),
        "zid": "0x14908f2e535aa3a139d351b7",
        "issues": {
            "allowance": {"actual": "0", "spender": ALLOWANCE_HOLDER_ADDRESS},
            "balance": {
                "token": WETH,
                "actual": "0",
                "expected": str(SELL_AMOUNT),
            },
            "simulationIncomplete": False,
            "invalidSourcesPassed": [],
        },
        "fees": {
            "zeroExFee": {"amount": "282240", "token": USDC, "type": "volume"},
            "integratorFee": None,
            "gasFee": None,
        },
        "tokenMetadata": {
            "buyToken": {"buyTaxBps": "0", "sellTaxBps": "0"},
            "sellToken": {"buyTaxBps": "0", "sellTaxBps": "0"},
        },
    }
    payload.update(json.loads(json.dumps(over)) if over else {})
    return payload
