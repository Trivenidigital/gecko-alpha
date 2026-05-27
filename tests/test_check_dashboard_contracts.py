"""Unit tests for scripts/check_dashboard_contracts.py."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "check_dashboard_contracts",
    Path(__file__).resolve().parent.parent / "scripts" / "check_dashboard_contracts.py",
)
_MOD = importlib.util.module_from_spec(_SPEC)
sys.modules["check_dashboard_contracts"] = _MOD
_SPEC.loader.exec_module(_MOD)


class _FakeResult:
    def __init__(self, *, criticals=None, warnings=None, passed=1):
        self.criticals = list(criticals or [])
        self.warnings = list(warnings or [])
        self.passed = passed


def test_main_json_ok_when_all_checkers_pass(monkeypatch, capsys):
    calls = []

    def fake_live_fetch(url, *, timeout_sec, slo_ms, limit, window_hours):
        calls.append(("live", url, timeout_sec, slo_ms, limit, window_hours))
        return _FakeResult(), 0

    def fake_trade_fetch(url, *, timeout_sec, limit_per_group, window_hours):
        calls.append(("trade", url, timeout_sec, limit_per_group, window_hours))
        return _FakeResult(), 0

    def fake_focus_fetch(url, *, timeout_sec, window_hours):
        calls.append(("focus", url, timeout_sec, window_hours))
        return _FakeResult(), 0

    monkeypatch.setattr(_MOD._LIVE_CHECKER, "fetch_and_validate", fake_live_fetch)
    monkeypatch.setattr(_MOD._TRADE_CHECKER, "fetch_and_validate", fake_trade_fetch)
    monkeypatch.setattr(_MOD._FOCUS_CHECKER, "fetch_and_validate", fake_focus_fetch)

    exit_code = _MOD.main(["--url", "http://dash", "--json"])

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "ok"
    assert payload["exit_code"] == 0
    assert set(payload["checks"]) == {
        "live_candidates",
        "trade_inbox",
        "todays_focus",
    }
    assert payload["checks"]["live_candidates"]["status"] == "ok"
    assert payload["checks"]["trade_inbox"]["status"] == "ok"
    assert payload["checks"]["todays_focus"]["status"] == "ok"
    assert [call[0] for call in calls] == ["live", "trade", "focus"]


def test_main_runs_both_checks_even_when_live_fails(monkeypatch, capsys):
    calls = []

    def fake_live_fetch(url, *, timeout_sec, slo_ms, limit, window_hours):
        calls.append("live")
        return _FakeResult(criticals=["live contract drift"], passed=0), 1

    def fake_trade_fetch(url, *, timeout_sec, limit_per_group, window_hours):
        calls.append("trade")
        return _FakeResult(), 0

    def fake_focus_fetch(url, *, timeout_sec, window_hours):
        calls.append("focus")
        return _FakeResult(), 0

    monkeypatch.setattr(_MOD._LIVE_CHECKER, "fetch_and_validate", fake_live_fetch)
    monkeypatch.setattr(_MOD._TRADE_CHECKER, "fetch_and_validate", fake_trade_fetch)
    monkeypatch.setattr(_MOD._FOCUS_CHECKER, "fetch_and_validate", fake_focus_fetch)

    exit_code = _MOD.main(["--url", "http://dash", "--json"])

    assert exit_code == 1
    assert calls == ["live", "trade", "focus"]
    payload = json.loads(capsys.readouterr().out)
    assert payload["checks"]["live_candidates"]["criticals"] == ["live contract drift"]
    assert payload["checks"]["trade_inbox"]["status"] == "ok"


def test_failure_json_preserves_per_check_details(monkeypatch, capsys):
    def fake_live_fetch(url, *, timeout_sec, slo_ms, limit, window_hours):
        return _FakeResult(
            criticals=["live contract drift"],
            warnings=["live slow"],
            passed=0,
        ), 1

    def fake_trade_fetch(url, *, timeout_sec, limit_per_group, window_hours):
        return _FakeResult(warnings=["trade warning"], passed=1), 0

    def fake_focus_fetch(url, *, timeout_sec, window_hours):
        return _FakeResult(warnings=["focus warning"], passed=1), 0

    monkeypatch.setattr(_MOD._LIVE_CHECKER, "fetch_and_validate", fake_live_fetch)
    monkeypatch.setattr(_MOD._TRADE_CHECKER, "fetch_and_validate", fake_trade_fetch)
    monkeypatch.setattr(_MOD._FOCUS_CHECKER, "fetch_and_validate", fake_focus_fetch)

    exit_code = _MOD.main(["--url", "http://dash", "--json"])

    assert exit_code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["checks"]["live_candidates"]["critical_count"] == 1
    assert payload["checks"]["live_candidates"]["warning_count"] == 1
    assert payload["checks"]["live_candidates"]["criticals"] == ["live contract drift"]
    assert payload["checks"]["live_candidates"]["warnings"] == ["live slow"]
    assert payload["checks"]["trade_inbox"]["warnings"] == ["trade warning"]
    assert payload["checks"]["todays_focus"]["warnings"] == ["focus warning"]


def test_failure_text_prints_per_check_details(monkeypatch, capsys):
    def fake_live_fetch(url, *, timeout_sec, slo_ms, limit, window_hours):
        return _FakeResult(criticals=["live contract drift"], passed=0), 1

    def fake_trade_fetch(url, *, timeout_sec, limit_per_group, window_hours):
        return _FakeResult(warnings=["trade warning"], passed=1), 0

    def fake_focus_fetch(url, *, timeout_sec, window_hours):
        return _FakeResult(warnings=["focus warning"], passed=1), 0

    monkeypatch.setattr(_MOD._LIVE_CHECKER, "fetch_and_validate", fake_live_fetch)
    monkeypatch.setattr(_MOD._TRADE_CHECKER, "fetch_and_validate", fake_trade_fetch)
    monkeypatch.setattr(_MOD._FOCUS_CHECKER, "fetch_and_validate", fake_focus_fetch)

    exit_code = _MOD.main(["--url", "http://dash"])

    assert exit_code == 1
    output = capsys.readouterr().out
    assert "FAIL: dashboard contract smoke failed (exit 1)" in output
    assert "live_candidates CRITICAL: live contract drift" in output
    assert "trade_inbox WARNING: trade warning" in output
    assert "todays_focus WARNING: focus warning" in output


def test_verbose_success_text_prints_warnings(monkeypatch, capsys):
    def fake_live_fetch(url, *, timeout_sec, slo_ms, limit, window_hours):
        return _FakeResult(warnings=["live slow"], passed=1), 0

    def fake_trade_fetch(url, *, timeout_sec, limit_per_group, window_hours):
        return _FakeResult(warnings=["trade warning"], passed=1), 0

    def fake_focus_fetch(url, *, timeout_sec, window_hours):
        return _FakeResult(warnings=["focus warning"], passed=1), 0

    monkeypatch.setattr(_MOD._LIVE_CHECKER, "fetch_and_validate", fake_live_fetch)
    monkeypatch.setattr(_MOD._TRADE_CHECKER, "fetch_and_validate", fake_trade_fetch)
    monkeypatch.setattr(_MOD._FOCUS_CHECKER, "fetch_and_validate", fake_focus_fetch)

    exit_code = _MOD.main(["--url", "http://dash", "--verbose"])

    assert exit_code == 0
    output = capsys.readouterr().out
    assert "OK: dashboard contracts clean" in output
    assert "live_candidates WARNING: live slow" in output
    assert "trade_inbox WARNING: trade warning" in output
    assert "todays_focus WARNING: focus warning" in output


def test_exit_code_priority_prefers_contract_over_http(monkeypatch, capsys):
    def fake_live_fetch(url, *, timeout_sec, slo_ms, limit, window_hours):
        return _FakeResult(criticals=["live http failed"], passed=0), 2

    def fake_trade_fetch(url, *, timeout_sec, limit_per_group, window_hours):
        return _FakeResult(criticals=["trade contract drift"], passed=0), 1

    def fake_focus_fetch(url, *, timeout_sec, window_hours):
        return _FakeResult(), 0

    monkeypatch.setattr(_MOD._LIVE_CHECKER, "fetch_and_validate", fake_live_fetch)
    monkeypatch.setattr(_MOD._TRADE_CHECKER, "fetch_and_validate", fake_trade_fetch)
    monkeypatch.setattr(_MOD._FOCUS_CHECKER, "fetch_and_validate", fake_focus_fetch)

    exit_code = _MOD.main(["--url", "http://dash", "--json"])

    assert exit_code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["exit_code"] == 1
    assert payload["checks"]["live_candidates"]["exit_code"] == 2
    assert payload["checks"]["trade_inbox"]["exit_code"] == 1
    assert payload["checks"]["todays_focus"]["exit_code"] == 0


def test_argument_forwarding_uses_endpoint_defaults(monkeypatch):
    calls = {}

    def fake_live_fetch(url, *, timeout_sec, slo_ms, limit, window_hours):
        calls["live"] = {
            "url": url,
            "timeout_sec": timeout_sec,
            "slo_ms": slo_ms,
            "limit": limit,
            "window_hours": window_hours,
        }
        return _FakeResult(), 0

    def fake_trade_fetch(url, *, timeout_sec, limit_per_group, window_hours):
        calls["trade"] = {
            "url": url,
            "timeout_sec": timeout_sec,
            "limit_per_group": limit_per_group,
            "window_hours": window_hours,
        }
        return _FakeResult(), 0

    def fake_focus_fetch(url, *, timeout_sec, window_hours):
        calls["focus"] = {
            "url": url,
            "timeout_sec": timeout_sec,
            "window_hours": window_hours,
        }
        return _FakeResult(), 0

    monkeypatch.setattr(_MOD._LIVE_CHECKER, "fetch_and_validate", fake_live_fetch)
    monkeypatch.setattr(_MOD._TRADE_CHECKER, "fetch_and_validate", fake_trade_fetch)
    monkeypatch.setattr(_MOD._FOCUS_CHECKER, "fetch_and_validate", fake_focus_fetch)

    exit_code = _MOD.main(
        [
            "--url",
            "http://dash",
            "--live-limit",
            "7",
            "--trade-limit-per-group",
            "8",
            "--window-hours",
            "24",
            "--timeout-sec",
            "1.5",
        ]
    )

    assert exit_code == 0
    assert calls["live"] == {
        "url": "http://dash",
        "timeout_sec": 1.5,
        "slo_ms": 3000,
        "limit": 7,
        "window_hours": 24,
    }
    assert calls["trade"] == {
        "url": "http://dash",
        "timeout_sec": 1.5,
        "limit_per_group": 8,
        "window_hours": 24,
    }
    assert calls["focus"] == {
        "url": "http://dash",
        "timeout_sec": 1.5,
        "window_hours": 24,
    }


def test_config_validation_rejects_bad_limits(monkeypatch, capsys):
    def fail_if_called(*args, **kwargs):
        raise AssertionError("checker should not run after config validation failure")

    monkeypatch.setattr(_MOD._LIVE_CHECKER, "fetch_and_validate", fail_if_called)
    monkeypatch.setattr(_MOD._TRADE_CHECKER, "fetch_and_validate", fail_if_called)
    monkeypatch.setattr(_MOD._FOCUS_CHECKER, "fetch_and_validate", fail_if_called)

    exit_code = _MOD.main(["--live-limit", "0"])

    assert exit_code == 4
    assert "--live-limit must be in [1, 50]" in capsys.readouterr().err
