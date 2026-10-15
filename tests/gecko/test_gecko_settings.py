from gecko.settings import GeckoSettings


def test_defaults(tmp_path):
    s = GeckoSettings(db_path=str(tmp_path / "g.db"), _env_file=None)
    assert s.chains == ["solana", "base", "ethereum", "bsc"]
    assert s.min_liquidity_usd == 250_000
    assert s.min_mcap_usd == 500_000
    assert s.max_mcap_usd == 200_000_000
    assert s.survivor_hours == 24
    assert s.watchlist_cap == 50
    assert s.f4_max_age_days == 90
    assert s.f4_min_vol_ratio == 2.0
    assert s.coingecko_monthly_cap == 8000
    assert s.coingecko_demo_api_key is None
    assert s.http_timeout_sec == 20


def test_env_prefix(monkeypatch, tmp_path):
    monkeypatch.setenv("GECKO_MIN_LIQUIDITY_USD", "100000")
    s = GeckoSettings(db_path=str(tmp_path / "g.db"), _env_file=None)
    assert s.min_liquidity_usd == 100_000


def test_chain_validation(tmp_path):
    import pytest

    with pytest.raises(ValueError):
        GeckoSettings(db_path=str(tmp_path / "g.db"), chains=["tron"], _env_file=None)


def test_env_file_is_dot_env_gecko_never_dot_env(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("GECKO_MIN_LIQUIDITY_USD", raising=False)
    (tmp_path / ".env").write_text("GECKO_MIN_LIQUIDITY_USD=1\n")
    assert GeckoSettings().min_liquidity_usd == 250_000  # .env ignored
    (tmp_path / ".env.gecko").write_text("GECKO_MIN_LIQUIDITY_USD=2\n")
    assert GeckoSettings().min_liquidity_usd == 2


def test_ledger_max_overdue_days_floor(tmp_path):
    # an expiry below the floor would close the whole due ledger unmeasured
    import pytest
    from pydantic import ValidationError

    db = str(tmp_path / "g.db")
    assert GeckoSettings(db_path=db, ledger_max_overdue_days=7, _env_file=None)
    with pytest.raises(ValidationError):
        GeckoSettings(db_path=db, ledger_max_overdue_days=6, _env_file=None)
