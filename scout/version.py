"""Runtime version + git-SHA helpers — shared across scout/.

PR #247 introduced these inline in scout/main.py for the
``scanner_starting`` startup banner. Round 16 promotes them to a shared
module so scout/heartbeat.py (and any future surface that needs to log
the running commit) can import without a scout.main → scout.heartbeat
circular dependency.

Both helpers swallow all errors and return ``"unknown"`` — they must
never crash a startup path or a periodic heartbeat.
"""

from __future__ import annotations


def runtime_version() -> str:
    """Return the gecko-alpha package version from importlib.metadata.

    Returns the literal string ``"unknown"`` if metadata is unavailable
    (e.g. running from a source checkout without editable-install). Never
    raises.
    """
    try:
        from importlib.metadata import PackageNotFoundError, version as _pkg_version

        try:
            return _pkg_version("gecko-alpha")
        except PackageNotFoundError:
            return "unknown"
    except Exception:
        return "unknown"


def runtime_git_sha() -> str:
    """Return the short git SHA of the current checkout, or ``"unknown"``.

    Cheap subprocess to ``git rev-parse --short HEAD``. Bounded to 2s so a
    pathological repo state cannot block startup or heartbeat emission.
    Returns ``"unknown"`` on any failure (no git, not a repo, command
    timeout, permission denied).
    """
    import subprocess

    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=2.0,
            cwd=__file__.rsplit("/", 2)[0] if "/" in __file__ else None,
            check=False,
        )
        sha = (proc.stdout or "").strip()
        return sha or "unknown"
    except Exception:
        return "unknown"
