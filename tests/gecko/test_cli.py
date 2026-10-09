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


def test_job_crash_before_run_job_exits_1(monkeypatch, capsys):
    from gecko.jobs import JOBS

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


def test_relative_db_path_is_logged_with_resolved_path(monkeypatch, settings):
    import os

    from structlog.testing import capture_logs

    _patch_settings(monkeypatch, settings)
    _noop_job(monkeypatch, "rank")
    with capture_logs() as logs:
        assert main(["rank"]) == 0
    assert not [e for e in logs if e["event"] == "gecko_relative_db_path"]
    settings.db_path = "gecko.db"
    with capture_logs() as logs:
        assert main(["rank"]) == 0
    (e,) = [e for e in logs if e["event"] == "gecko_relative_db_path"]
    assert e["log_level"] == "warning" and e["resolved"] == os.path.abspath("gecko.db")
