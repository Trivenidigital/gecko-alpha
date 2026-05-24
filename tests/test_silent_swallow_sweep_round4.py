"""Round 4 silent-swallow sweep — extends PR #245's static lint beyond db.py.

PR #245 fixed 12 ROLLBACK-cleanup silent-swallow sites in scout/db.py. This
round extends the rule to the entire scout/ tree: every `except Exception:
pass` is a Class-1 silent failure (per CLAUDE.md §12) UNLESS it's
intentional defense-in-depth — and even then the failure must be observable
via a log call.

Sites fixed in this PR:
  - scout/config.py:1457 — settings-validation-alert dispatch error
  - scout/chains/tracker.py:191 — chain_check rollback failure
  - scout/social/telegram/listener.py:560 — tg-social message-write rollback
  - scout/social/telegram/listener.py:1109 — catchup circuit-break alert
  - scout/social/telegram/listener.py:1176 — listener circuit-break alert
  - scout/social/telegram/listener.py:1195 — AuthKeyError alert

The test below is the regression gate.
"""

from __future__ import annotations

import re
from pathlib import Path


SCOUT_DIR = Path(__file__).resolve().parent.parent / "scout"


def _iter_scout_py_files() -> list[Path]:
    skip = {"__pycache__", ".pytest_cache", ".venv"}
    files = []
    for path in SCOUT_DIR.rglob("*.py"):
        if any(part in skip for part in path.parts):
            continue
        files.append(path)
    return files


def test_no_silent_except_exception_pass_in_scout():
    """Every `except Exception: pass` is forbidden in scout/.

    If genuine defense-in-depth is needed (e.g. cleanup must not mask the
    outer error), use:

        except Exception:
            log.exception("descriptive_event_name")

    so the swallowed failure remains observable via journalctl.
    """
    pat = re.compile(
        r"except Exception:\s*\n\s+pass\b", re.MULTILINE
    )
    offenders: list[tuple[Path, int]] = []
    for path in _iter_scout_py_files():
        src = path.read_text(encoding="utf-8")
        for m in pat.finditer(src):
            line = src.count("\n", 0, m.start()) + 1
            offenders.append((path, line))

    assert not offenders, (
        "scout/ reintroduced `except Exception: pass` silent-swallow at:\n"
        + "\n".join(
            f"  - {p.relative_to(SCOUT_DIR.parent)}:{ln}" for p, ln in offenders
        )
        + "\n\nReplace pass with a log.exception(...) call so the swallowed "
        "failure stays observable. See PR #245 + Round 4 sweep for the "
        "canonical pattern."
    )


def test_no_bare_except_pass_in_scout():
    """Bare `except: pass` (catches SystemExit/KeyboardInterrupt too) is
    universally forbidden in scout/."""
    pat = re.compile(r"^\s*except:\s*\n\s+pass\b", re.MULTILINE)
    offenders: list[tuple[Path, int]] = []
    for path in _iter_scout_py_files():
        src = path.read_text(encoding="utf-8")
        for m in pat.finditer(src):
            line = src.count("\n", 0, m.start()) + 1
            offenders.append((path, line))

    assert not offenders, (
        "scout/ has bare `except: pass`:\n"
        + "\n".join(
            f"  - {p.relative_to(SCOUT_DIR.parent)}:{ln}" for p, ln in offenders
        )
    )
