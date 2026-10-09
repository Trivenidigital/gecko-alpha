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
