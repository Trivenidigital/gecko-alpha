from __future__ import annotations

from decimal import Decimal

from scout.config import Settings

_REQUIRED = dict(TELEGRAM_BOT_TOKEN="t", TELEGRAM_CHAT_ID="c", ANTHROPIC_API_KEY="k")


def test_solana_defaults():
    s = Settings(_env_file=None, **_REQUIRED)
    assert s.SOLANA_RPC_URL.startswith("https://")
    assert s.SOLANA_JUPITER_URL == "https://api.jup.ag/swap/v1"
    assert s.SOLANA_JUPITER_API_KEY is None
    assert s.SOLANA_WALLET_SECRET is None
    assert s.SOLANA_SLIPPAGE_BPS_CAP == 100
    assert s.SOLANA_MAX_PRICE_IMPACT_PCT == 3.0
    assert s.SOLANA_MIN_SOL_GAS_RESERVE == 0.02
    assert s.SOLANA_FLOAT_CAP_USD == Decimal("100")
    assert s.SOLANA_CONFIRM_TIMEOUT_SEC == 60.0


def test_solana_secret_is_not_plaintext_in_repr():
    s = Settings(_env_file=None, **_REQUIRED, SOLANA_WALLET_SECRET="supersecret")
    assert "supersecret" not in repr(s.SOLANA_WALLET_SECRET)
    assert s.SOLANA_WALLET_SECRET.get_secret_value() == "supersecret"
