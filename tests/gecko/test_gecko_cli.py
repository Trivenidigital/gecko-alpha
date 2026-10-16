import os

from gecko.__main__ import main


def test_unknown_job_exits_2(capsys):
    assert main(["no-such-job"]) == 2
    assert "unknown job" in capsys.readouterr().err


def test_list_jobs(capsys):
    assert main(["--list"]) == 0
    assert "freshness" in capsys.readouterr().out


def test_python_dash_m_entry_sees_registered_jobs():
    # `python -m gecko` executes this module as `__main__`; job modules import
    # `gecko.__main__`, a distinct module object. The entry point must dispatch
    # through the canonical module or the registry it reads is empty.
    import subprocess
    import sys
    from pathlib import Path

    repo_root = Path(__file__).resolve().parents[2]
    r = subprocess.run(
        [sys.executable, "-m", "gecko", "--list"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert r.returncode == 0, r.stderr
    assert "freshness" in r.stdout.split()


def test_single_job_registry():
    import gecko.__main__
    import gecko.jobs

    assert gecko.__main__.JOBS is gecko.jobs.JOBS


def test_job_crash_before_run_job_exits_1(monkeypatch, capsys, tmp_path):
    from gecko.jobs import JOBS

    monkeypatch.setenv("GECKO_DB_PATH", str(tmp_path / "gecko.db"))

    async def boom(settings, ns):
        raise RuntimeError("crashed before run_job")

    monkeypatch.setitem(JOBS, "boom", boom)
    assert main(["boom"]) == 1
    assert "crashed before run_job" in capsys.readouterr().err


def _patch_settings(monkeypatch, settings):
    import gecko.__main__

    monkeypatch.setattr(gecko.__main__, "GeckoSettings", lambda: settings)


def _noop_job(monkeypatch, name):
    from gecko.jobs import JOBS

    ran = []

    async def job(settings, ns):
        ran.append(name)
        return 0

    monkeypatch.setitem(JOBS, name, job)
    return ran


def test_ledger_refuses_as_of(monkeypatch, settings, capsys):
    _patch_settings(monkeypatch, settings)
    ran = _noop_job(monkeypatch, "ledger")
    assert main(["ledger", "--as-of", "2026-10-01T00:00:00Z"]) == 2
    assert "ledger is live-only" in capsys.readouterr().err
    assert ran == []
    assert main(["ledger"]) == 0 and ran == ["ledger"]


def test_digest_refuses_as_of(monkeypatch, settings, capsys):
    _patch_settings(monkeypatch, settings)
    ran = _noop_job(monkeypatch, "digest")
    assert main(["digest", "--as-of", "2026-10-01T00:00:00Z"]) == 2
    assert "digest is live-only" in capsys.readouterr().err
    assert ran == [] and not os.path.exists(settings.db_path)  # no run row
    assert main(["digest"]) == 0 and ran == ["digest"]


def test_coingecko_jobs_refuse_without_key(monkeypatch, settings, capsys):
    from pydantic import SecretStr

    _patch_settings(monkeypatch, settings)
    for name in ("coingecko-markets", "coingecko-hourly"):
        ran = _noop_job(monkeypatch, name)
        assert main([name]) == 1
        assert "GECKO_COINGECKO_DEMO_API_KEY" in capsys.readouterr().err
        assert ran == []
    settings.coingecko_demo_api_key = SecretStr("k")
    assert main(["coingecko-hourly"]) == 0 and ran == ["coingecko-hourly"]


def test_relative_db_path_is_a_usage_error(monkeypatch, settings, capsys):
    _patch_settings(monkeypatch, settings)
    ran = _noop_job(monkeypatch, "rank")
    assert main(["rank"]) == 0 and ran == ["rank"]
    settings.db_path = "gecko.db"
    assert main(["rank"]) == 2
    assert "GECKO_DB_PATH must be absolute" in capsys.readouterr().err
    assert ran == ["rank"]


def test_help_epilog_lists_jobs_and_exit_codes(capsys):
    import pytest

    with pytest.raises(SystemExit) as ei:
        main(["--help"])
    assert ei.value.code == 0
    out = capsys.readouterr().out
    assert "exit codes: 0 ok, 1 failure, 2 usage" in out
    for job in (
        "dexscreener",
        "ledger",
        "rank",
        "freshness",
        "coingecko-hourly",
        "enrich",
        "telegram-reader",
        "tg-login",
        "migrate-calls",
        "prune",
    ):
        assert f"\n  {job}" in out
