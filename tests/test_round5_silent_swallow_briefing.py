"""Round 5 — extend silent-swallow observability sweep to scout/briefing/.

PR #245 + #251 covered db.py rollback cleanup and broader scout/ `except
Exception: pass` sites. This round catches the `except Exception: return
[]` / `except Exception: return None` family, where the catch returns a
safe sentinel but emits no log — Class-1 silent failures hidden behind
"return safe default" patterns.

Fixed sites:
  scout/briefing/collector.py — 8 sites (return None x2, return [] x6)
  scout/chains/events.py:117 — settings load failure swallow
"""

from __future__ import annotations

import re
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
COLLECTOR = REPO_ROOT / "scout" / "briefing" / "collector.py"
CHAIN_EVENTS = REPO_ROOT / "scout" / "chains" / "events.py"


def test_briefing_collector_silent_swallow_sites_all_log():
    """Every `except Exception: return []` / `return None` in collector.py
    must be preceded by a logger.exception() call."""
    src = COLLECTOR.read_text(encoding="utf-8")
    # Find `except Exception:` blocks that ONLY have a return statement.
    pat = re.compile(
        r"except Exception:\s*\n(\s+)(return (\[\]|None|\{\}))",
        re.MULTILINE,
    )
    silent_sites = []
    for m in pat.finditer(src):
        line = src.count("\n", 0, m.start()) + 1
        silent_sites.append((line, m.group(2).strip()))

    assert not silent_sites, (
        "scout/briefing/collector.py has silent except-return blocks "
        "(no logger.exception before sentinel return). Sites:\n"
        + "\n".join(f"  - line {ln}: {body}" for ln, body in silent_sites)
        + "\n\nAdd `logger.exception('briefing_<func>_query_failed')` "
        "above the return so journalctl shows the path failed."
    )


def test_chain_events_settings_load_swallow_logs():
    """scout/chains/events.py:117 settings-load swallow must log."""
    src = CHAIN_EVENTS.read_text(encoding="utf-8")
    assert "chain_event_settings_load_failed" in src, (
        "scout/chains/events.py settings-import-swallow no longer emits a "
        "structured log; restore logger.exception("
        "'chain_event_settings_load_failed') before the return None."
    )


def test_close_trade_path_param_ge_1():
    """POST /api/trading/close/{trade_id} must reject trade_id <= 0."""
    import inspect

    from dashboard import api as dashboard_api

    src = inspect.getsource(dashboard_api.create_app)
    assert "FPath(..., ge=1)" in src, (
        "POST /api/trading/close/{trade_id} should declare ge=1 via "
        "fastapi.Path so trade_id=0 / negative integers are rejected at "
        "the framework boundary instead of hitting the DB."
    )
