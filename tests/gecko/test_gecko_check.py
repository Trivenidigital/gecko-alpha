import asyncio
import io
import json
import sqlite3
from collections import namedtuple

import pytest
import structlog
from aioresponses import aioresponses

from gecko.__main__ import main
from gecko.settings import GeckoSettings
from gecko.store import Store, utcnow

WRITERS = (
    "dexscreener",
    "geckoterminal",
    "geckoterminal-ohlcv",
    "benchmark",
    "enrich",
    "rank",
    "ledger",
    "freshness",
    "prune",
)
GIB = 1024**3
_Usage = namedtuple("_Usage", "total used free")


@pytest.fixture(autouse=True)
def _reset_structlog():
    structlog.reset_defaults()  # a json config leaked from another test proves nothing
    yield
    structlog.reset_defaults()


def _run(monkeypatch, settings, argv, free_gib=50.0):
    import gecko.__main__
    import gecko.check

    monkeypatch.setattr(gecko.__main__, "GeckoSettings", lambda: settings)
    monkeypatch.setattr(
        gecko.check.shutil,
        "disk_usage",
        lambda p: _Usage(100 * GIB, 0, int(free_gib * GIB)),
    )
    return main(argv)


def _configured(tmp_path, **kw) -> GeckoSettings:
    base = dict(
        db_path=str(tmp_path / "gecko.db"),
        telegram_bot_token="SECRET-BOT-TOKEN",
        telegram_health_chat_id="-100health",
        telegram_digest_chat_id="-100digest",
        coingecko_demo_api_key="SECRET-CG-KEY",
        anthropic_api_key="SECRET-ANTHROPIC-KEY",
        _env_file=None,
    )
    base.update(kw)
    return GeckoSettings(**base)


def _seed_runs(db_path: str, jobs, ok: bool = True) -> None:
    async def go():
        s = await Store.open(db_path)
        try:
            for j in jobs:
                await s.record_run(j, utcnow(), utcnow(), ok)
        finally:
            await s.close()

    asyncio.run(go())


def _runs_count(db_path: str) -> int:
    with sqlite3.connect(db_path) as c:
        return c.execute("SELECT COUNT(*) FROM runs").fetchone()[0]


def _lines(out: str) -> dict[str, list[str]]:
    by: dict[str, list[str]] = {}
    for line in out.splitlines():
        level, rest = line.split(" ", 1)
        name, _, detail = rest.partition(": ")
        by.setdefault(name, []).append(f"{level} {detail}")
    return by


def test_check_reports_missing_keys_as_warn_and_missing_telegram_as_fail(
    monkeypatch, settings, capsys
):
    rc = _run(monkeypatch, settings, ["check"])
    out = capsys.readouterr().out
    by = _lines(out)
    assert rc == 1
    assert by["telegram"][0].startswith("FAIL")
    assert by["digest_chat"][0].startswith("WARN")
    assert by["coingecko"][0].startswith("WARN")
    assert "markets/hourly dark" in by["coingecko"][0]
    assert by["anthropic"][0].startswith("WARN")
    assert "research dark" in by["anthropic"][0]
    # no channels configured: reader not activated, a WARN
    assert by["telegram_reader"][0].startswith("WARN")
    assert by["db_path"][0].startswith("OK")
    assert by["chains"][0].startswith("OK")
    assert by["retention"][0].startswith("OK")
    # the only FAIL is telegram
    assert [n for n, ls in by.items() for x in ls if x.startswith("FAIL")] == [
        "telegram"
    ]
    # diagnostic: writes no runs row
    assert _runs_count(settings.db_path) == 0


def test_check_send_posts_to_health_chat(monkeypatch, tmp_path, capsys):
    s = _configured(tmp_path)
    _seed_runs(s.db_path, WRITERS)
    url = "https://api.telegram.org/botSECRET-BOT-TOKEN/sendMessage"
    with aioresponses() as m:
        m.post(url, payload={"ok": True})
        rc = _run(monkeypatch, s, ["check", "--send"])
        calls = [c for (meth, u), cs in m.requests.items() for c in cs]
    out = capsys.readouterr().out
    assert rc == 0, out
    assert len(calls) == 1
    body = calls[0].kwargs["json"]
    assert body["chat_id"] == "-100health"
    assert body["text"].startswith("gecko check: ok ")
    assert _lines(out)["telegram"][0].startswith("OK")
    for secret in ("SECRET-BOT-TOKEN", "SECRET-CG-KEY", "SECRET-ANTHROPIC-KEY"):
        assert secret not in out
    assert _runs_count(s.db_path) == len(WRITERS)  # no check row

    # delivery failure is a FAIL
    with aioresponses() as m:
        m.post(url, payload={"ok": False, "description": "chat not found"})
        rc = _run(monkeypatch, s, ["check", "--send"])
    out = capsys.readouterr().out
    assert rc == 1
    assert _lines(out)["telegram"][0].startswith("FAIL")

    # without --send nothing is posted (an unmocked request would be recorded)
    with aioresponses() as m:
        rc = _run(monkeypatch, s, ["check"])
        assert not m.requests
    assert rc == 0


def test_check_jobs_ran_once_warns_per_missing_job(monkeypatch, tmp_path, capsys):
    s = _configured(tmp_path)
    _seed_runs(s.db_path, ["dexscreener", "rank", "ledger"])
    _seed_runs(s.db_path, ["prune"], ok=False)  # a failed run is not a run
    rc = _run(monkeypatch, s, ["check"])
    ls = _lines(capsys.readouterr().out)["jobs_ran_once"]
    missing = [j for j in WRITERS if j not in ("dexscreener", "rank", "ledger")]
    assert len(ls) == len(missing)
    for job, line in zip(missing, ls):
        assert line.startswith("WARN")
        assert job in line
        assert "never-run writers are invisible to freshness" in line
    assert rc == 0  # WARNs only

    _seed_runs(s.db_path, WRITERS)
    _run(monkeypatch, s, ["check"])
    ls = _lines(capsys.readouterr().out)["jobs_ran_once"]
    assert len(ls) == 1 and ls[0].startswith("OK")


def test_check_exit_codes(monkeypatch, tmp_path, capsys):
    s = _configured(tmp_path)
    _seed_runs(s.db_path, WRITERS)
    assert _run(monkeypatch, s, ["check"]) == 0
    out = capsys.readouterr().out
    warns = [n for n, ls in _lines(out).items() for x in ls if not x.startswith("OK")]
    assert warns == ["telegram_reader"], out  # reader not activated: a WARN only

    # disk thresholds: >= 10 GiB OK, >= 5 GiB WARN, else FAIL
    assert _run(monkeypatch, s, ["check"], free_gib=7) == 0
    assert _lines(capsys.readouterr().out)["disk"][0].startswith("WARN")
    assert _run(monkeypatch, s, ["check"], free_gib=4.9) == 1
    assert _lines(capsys.readouterr().out)["disk"][0].startswith("FAIL")

    # retention below 30 + overdue + 7 is a WARN
    short = _configured(tmp_path, obs_retention_days=50, ledger_max_overdue_days=14)
    assert _run(monkeypatch, short, ["check"]) == 0
    assert _lines(capsys.readouterr().out)["retention"][0].startswith("WARN")

    # channels configured but no session: the reader is meant to run, so FAIL
    tg = _configured(tmp_path, tg_channels=["@foo"])
    assert _run(monkeypatch, tg, ["check"]) == 1
    assert _lines(capsys.readouterr().out)["telegram_reader"][0].startswith("FAIL")

    # a foreign database is a FAIL line, not a crash and not exit 2
    foreign = tmp_path / "foreign.db"
    with sqlite3.connect(foreign) as c:
        c.execute("CREATE TABLE other(x)")
    assert (
        _run(monkeypatch, _configured(tmp_path, db_path=str(foreign)), ["check"]) == 1
    )
    by = _lines(capsys.readouterr().out)
    assert by["db_path"][0].startswith("FAIL")
    assert "not a gecko database" in by["db_path"][0]

    # a relative db path is reported, not refused before the check runs
    assert _run(monkeypatch, _configured(tmp_path, db_path="gecko.db"), ["check"]) == 1
    assert _lines(capsys.readouterr().out)["db_path"][0].startswith("FAIL")

    # usage error: argparse exits 2
    with pytest.raises(SystemExit) as ei:
        _run(monkeypatch, s, ["check", "--no-such-flag"])
    assert ei.value.code == 2


def test_main_configures_json_logging(monkeypatch, settings):
    from gecko.jobs import JOBS
    import gecko.__main__

    assert settings.log_format == "json"  # the default

    async def noop(settings, ns):
        return 0

    monkeypatch.setitem(JOBS, "noop", noop)
    monkeypatch.setattr(gecko.__main__, "GeckoSettings", lambda: settings)
    assert main(["noop"]) == 0
    buf = io.StringIO()
    structlog.configure(logger_factory=structlog.PrintLoggerFactory(buf))
    structlog.get_logger().info("probe", k=1)
    lines = buf.getvalue().splitlines()
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["event"] == "probe" and rec["k"] == 1
    assert rec["level"] == "info"
    assert rec["timestamp"].endswith("Z")
