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


def test_tg_catchup_limit_floor(tmp_path):
    # 0 reads nothing while every poll stays ok (the legacy "dark" setting)
    import pytest
    from pydantic import ValidationError

    db = str(tmp_path / "g.db")
    assert GeckoSettings(db_path=db, tg_catchup_limit=1, _env_file=None)
    with pytest.raises(ValidationError):
        GeckoSettings(db_path=db, tg_catchup_limit=0, _env_file=None)


def test_tg_poll_interval_floor(tmp_path):
    # a fast poll invites FloodWait on gecko's own session
    import pytest
    from pydantic import ValidationError

    db = str(tmp_path / "g.db")
    assert GeckoSettings(db_path=db, _env_file=None).tg_poll_interval_sec == 120
    assert GeckoSettings(db_path=db, tg_poll_interval_sec=30, _env_file=None)
    with pytest.raises(ValidationError):
        GeckoSettings(db_path=db, tg_poll_interval_sec=29, _env_file=None)


def test_channel_normalisation(tmp_path, monkeypatch):
    # one canonical key per channel: "@foo" is what calls.event_id and the
    # migrated history (source_id "@gem") are keyed on.
    import pytest

    db = str(tmp_path / "g.db")
    s = GeckoSettings(
        db_path=db,
        tg_channels=[" @Foo ", "t.me/bar", "https://t.me/Baz", "http://t.me/qux", "x"],
        _env_file=None,
    )
    assert s.tg_channels == ["@foo", "@bar", "@baz", "@qux", "@x"]
    monkeypatch.setenv("GECKO_TG_CHANNELS", '["HTTPS://T.ME/Foo"]')
    assert GeckoSettings(db_path=db, _env_file=None).tg_channels == ["@foo"]
    assert GeckoSettings(
        db_path=db, tg_channels=["t.me/foo/", "a_1"], _env_file=None
    ).tg_channels == ["@foo", "@a_1"]
    for bad in (
        ["@Foo", "t.me/foo"],
        ["t.me/foo/", "@foo"],  # one trailing "/" is stripped, then deduped
        ["@"],
        ["  "],
        ["https://t.me/"],
        ["t.me/foo//"],
        ["t.me/foo?x"],
        ["https://t.me/s/foo"],
        ["t.me/+invite"],
        ["@@foo"],
        ["telegram.me/foo"],
        ["foo bar"],
    ):
        with pytest.raises(ValueError):
            GeckoSettings(db_path=db, tg_channels=bad, _env_file=None)


def test_ai_layer_defaults(tmp_path):
    s = GeckoSettings(db_path=str(tmp_path / "g.db"), _env_file=None)
    assert s.anthropic_api_key is None
    assert s.research_model == "claude-sonnet-5-5"
    assert s.research_max_per_run == 10
    assert s.research_daily_usd_cap == 1.5
    assert s.research_max_searches == 5
    assert s.research_usd_per_mtok_in == 2.0
    assert s.research_usd_per_mtok_out == 10.0
    assert s.research_usd_per_search == 0.01
    assert s.research_max_tokens == 2000
    assert s.telegram_digest_chat_id is None
    assert s.digest_top_n == 10
    assert s.scorecard_min_n == 20
    assert s.anthropic_base_url == "https://api.anthropic.com"
    assert s.log_format == "json"


def test_ai_layer_bounds(tmp_path):
    import pytest
    from pydantic import ValidationError

    db = str(tmp_path / "g.db")
    ok = dict(
        research_max_searches=20,
        research_max_per_run=0,
        research_daily_usd_cap=0,
        digest_top_n=50,
        scorecard_min_n=1,
        log_format="console",
    )
    assert GeckoSettings(db_path=db, _env_file=None, **ok)
    assert GeckoSettings(db_path=db, digest_top_n=1, _env_file=None)
    for n in (256, 8192):
        assert GeckoSettings(db_path=db, research_max_tokens=n, _env_file=None)
    zero_prices = dict(
        research_usd_per_mtok_in=0,
        research_usd_per_mtok_out=0,
        research_usd_per_search=0,
    )
    assert GeckoSettings(db_path=db, _env_file=None, **zero_prices)
    for bad in (
        {"research_max_searches": 21},
        {"research_max_searches": -1},
        {"research_max_per_run": -1},
        {"research_daily_usd_cap": -0.01},
        {"digest_top_n": 0},
        {"digest_top_n": 51},
        {"scorecard_min_n": 0},
        {"log_format": "xml"},
        {"research_usd_per_mtok_in": -0.01},
        {"research_usd_per_mtok_out": -0.01},
        {"research_usd_per_search": -0.01},
        {"research_max_tokens": 255},
        {"research_max_tokens": 8193},
        {"anthropic_base_url": "http://api.anthropic.com"},
        {"anthropic_base_url": "api.anthropic.com"},
    ):
        with pytest.raises(ValidationError):
            GeckoSettings(db_path=db, _env_file=None, **bad)


def test_anthropic_api_key_is_secret(tmp_path, monkeypatch):
    from pydantic import SecretStr

    monkeypatch.setenv("GECKO_ANTHROPIC_API_KEY", "sk-ant-topsecret")
    s = GeckoSettings(db_path=str(tmp_path / "g.db"), _env_file=None)
    assert isinstance(s.anthropic_api_key, SecretStr)
    assert s.anthropic_api_key.get_secret_value() == "sk-ant-topsecret"
    assert "topsecret" not in repr(s.anthropic_api_key)
    assert "topsecret" not in repr(s)
