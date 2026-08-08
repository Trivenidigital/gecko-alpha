"""Pilot threshold watchdog — read-only observer for pre-registered gates.

The two things most likely to be wrong here are (a) the admission-writer
definition, which has already produced false readings twice via `pgrep -f`
self-matching, and (b) leaking mid-cohort analysis into an operational page.
Both are tested directly.
"""

from __future__ import annotations

import importlib.util
import sqlite3
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "pilot_threshold_watchdog", REPO_ROOT / "scripts" / "pilot_threshold_watchdog.py"
)
wd = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(wd)


def _args(**over):
    class A:
        max_entries = 200
        k2_net_usd = -400.0
        k3_null_pct = 5.0
        k4_min_per_day = 3
        k4_days = 5
        k5_max_open = 60
        n_eff_floor = 120
        gainers_gate = 100

    a = A()
    for k, v in over.items():
        setattr(a, k, v)
    return a


def _eval(**over):
    base = dict(
        t0="2026-08-08T17:17:39+00:00",
        enabled=1,
        cohort={"entries": 3, "open": 3, "closed": 0, "n_eff": 0, "net_usd": 0.0},
        entry_rate=[10, 10, 10, 10, 10],
        writers=1,
        gainers_n=92,
        max_entries=200,
        k2_net_usd=-400.0,
        k3_null_pct=5.0,
        k4_min_per_day=3,
        k4_days=5,
        k5_max_open=60,
        n_eff_floor=120,
        gainers_gate=100,
    )
    base.update(over)
    return {f["trigger"] for f in wd.evaluate_triggers(**base)}


class TestAdmissionWriterCounting:
    """*** DO NOT COUNT WITH pgrep -f. ***

    `uv run python -m scout.main` is TWO processes for ONE writer, and a
    checking process whose own cmdline mentions the pattern counts itself.
    Both mistakes have already produced false readings during this incident.
    """

    def test_uv_wrapper_plus_child_is_ONE_writer(self):
        procs = [
            (100, ["/root/.local/bin/uv", "run", "python", "-m", "scout.main"]),
            (101, ["/root/gecko-alpha/.venv/bin/python3", "-m", "scout.main"]),
        ]
        assert wd.count_admission_writers(procs, self_pid=0) == 1, (
            "the uv wrapper is not a writer; counting it reports permanent overlap"
        )

    def test_two_real_writers_are_detected(self):
        procs = [
            (100, ["/root/.local/bin/uv", "run", "python", "-m", "scout.main"]),
            (101, ["/usr/bin/python3", "-m", "scout.main"]),
            (200, ["/usr/bin/python3", "-m", "scout.main"]),
        ]
        assert wd.count_admission_writers(procs, self_pid=0) == 2

    def test_the_watchdog_does_not_count_itself(self):
        """Its own argv mentions the module it inspects."""
        procs = [
            (101, ["/usr/bin/python3", "-m", "scout.main"]),
            (999, ["/usr/bin/python3", "scripts/pilot_threshold_watchdog.py",
                   "--db", "scout.db"]),
        ]
        assert wd.count_admission_writers(procs, self_pid=999) == 1

    def test_a_shell_quoting_the_pattern_is_not_a_writer(self):
        """The exact false positive seen in production residue checks."""
        procs = [
            (101, ["/usr/bin/python3", "-m", "scout.main"]),
            (500, ["bash", "-c", "ps -eo cmd | grep 'python -m scout.main'"]),
        ]
        assert wd.count_admission_writers(procs, self_pid=0) == 1

    def test_unrelated_python_modules_are_not_writers(self):
        procs = [(1, ["/usr/bin/python3", "-m", "scout.live.solana_lane", "watchdog"])]
        assert wd.count_admission_writers(procs, self_pid=0) == 0

    def test_missing_proc_is_unknown_not_zero(self):
        """An empty process table must not read as 'no overlap'."""
        assert wd.count_admission_writers([], self_pid=0) == 0
        # ...and the caller converts an empty table to None, never to 0 writers:
        assert "writers if procs else None" in (
            REPO_ROOT / "scripts" / "pilot_threshold_watchdog.py"
        ).read_text("utf-8").replace("count_admission_writers(procs, self_pid=0) ", "writers ")


class TestNoMidCohortAnalysis:
    """*** THE EMBARGO. *** Operational state only before the terminal gate."""

    def test_instrumentation_trigger_carries_no_mae_value(self):
        fired = wd.evaluate_triggers(
            **dict(
                t0="T0", enabled=1,
                cohort={"entries": 5, "open": 4, "closed": 1, "n_eff": 1, "net_usd": -3.0},
                entry_rate=[10] * 5, writers=1, gainers_n=0, max_entries=200,
                k2_net_usd=-400.0, k3_null_pct=5.0, k4_min_per_day=3, k4_days=5,
                k5_max_open=60, n_eff_floor=120, gainers_gate=100,
            )
        )
        t = next(f for f in fired if f["trigger"] == "pilot_instrumentation_validated")
        assert "measurement pipe validated" in t["detail"]
        for banned in ("mae_pct=", "D(", "C(", "%", "win", "pnl"):
            assert banned not in t["detail"], f"leaked analysis: {banned}"

    def test_n_eff_is_NOT_alerted_at_the_power_floor(self):
        """`n_eff >= 120` is a power floor, not an action boundary. Surfacing it
        while admissions run invites the inspection the pre-registration
        defers."""
        fired = _eval(
            cohort={"entries": 150, "open": 30, "closed": 120, "n_eff": 120,
                    "net_usd": -50.0},
        )
        assert "pilot_tail_resolved" not in fired, (
            "n_eff must not be reported while admissions are still open"
        )

    def test_n_eff_IS_reported_once_the_tail_resolves(self):
        fired = wd.evaluate_triggers(
            **dict(
                t0="T0", enabled=0,
                cohort={"entries": 200, "open": 0, "closed": 200, "n_eff": 130,
                        "net_usd": -20.0},
                entry_rate=[], writers=1, gainers_n=0, max_entries=200,
                k2_net_usd=-400.0, k3_null_pct=5.0, k4_min_per_day=3, k4_days=5,
                k5_max_open=60, n_eff_floor=120, gainers_gate=100,
            )
        )
        t = next(f for f in fired if f["trigger"] == "pilot_tail_resolved")
        assert "n_eff=130" in t["detail"] and "verdict may proceed" in t["detail"]

    def test_below_floor_reports_NO_VERDICT(self):
        fired = wd.evaluate_triggers(
            **dict(
                t0="T0", enabled=0,
                cohort={"entries": 200, "open": 0, "closed": 200, "n_eff": 90,
                        "net_usd": -20.0},
                entry_rate=[], writers=1, gainers_n=0, max_entries=200,
                k2_net_usd=-400.0, k3_null_pct=5.0, k4_min_per_day=3, k4_days=5,
                k5_max_open=60, n_eff_floor=120, gainers_gate=100,
            )
        )
        t = next(f for f in fired if f["trigger"] == "pilot_tail_resolved")
        assert "NO_VERDICT" in t["detail"]

    def test_source_contains_no_verdict_metrics(self):
        """Structural guard: the module must never grow D(X)/C(X)/win-rate."""
        src = (REPO_ROOT / "scripts" / "pilot_threshold_watchdog.py").read_text("utf-8")
        body = src[src.index("def evaluate_triggers") :]
        for banned in ("win_rate", "mean_mae", "avg(", "d_10", "capture_incidence"):
            assert banned not in body.lower(), f"verdict metric leaked: {banned}"


class TestKillConditions:
    def test_K2_only_before_admission_close(self):
        assert "K2_net_loss" in _eval(
            cohort={"entries": 50, "open": 10, "closed": 40, "n_eff": 40,
                    "net_usd": -450.0}
        )
        # After admissions close, a deep net is a RESULT, not an abort trigger.
        assert "K2_net_loss" not in _eval(
            enabled=0,
            cohort={"entries": 200, "open": 0, "closed": 200, "n_eff": 190,
                    "net_usd": -450.0},
        )

    def test_K3_fires_on_broken_instrumentation(self):
        assert "K3_instrumentation_broken" in _eval(
            cohort={"entries": 40, "open": 0, "closed": 40, "n_eff": 30,
                    "net_usd": -10.0}
        )

    def test_K3_needs_a_sample_before_judging(self):
        """1 of 2 NULL is 50% but proves nothing; don't page on n=2."""
        assert "K3_instrumentation_broken" not in _eval(
            cohort={"entries": 2, "open": 0, "closed": 2, "n_eff": 1, "net_usd": 0.0}
        )

    def test_K4_needs_all_days_below_threshold(self):
        assert "K4_entry_rate_collapsed" in _eval(entry_rate=[1, 2, 0, 1, 2])
        assert "K4_entry_rate_collapsed" not in _eval(entry_rate=[1, 2, 9, 1, 2])

    def test_K5_concurrency_tripwire(self):
        assert "K5_concurrency_tripwire" in _eval(
            cohort={"entries": 80, "open": 61, "closed": 19, "n_eff": 19,
                    "net_usd": 0.0}
        )

    def test_K6_cohort_overshoot_is_read_from_the_DB(self):
        """Durable form of `pilot_entry_cap_exceeded` — not dependent on a log
        line surviving rotation."""
        assert "K6_cohort_overshoot" in _eval(
            cohort={"entries": 201, "open": 1, "closed": 200, "n_eff": 200,
                    "net_usd": 0.0}
        )

    def test_K6_multiple_writers(self):
        assert "K6_multiple_admission_writers" in _eval(writers=2)
        assert "K6_multiple_admission_writers" not in _eval(writers=1)
        assert "K6_multiple_admission_writers" not in _eval(writers=None)


class TestAdmissionCloseAndGainers:
    def test_rollback_due_when_cap_reached_while_enabled(self):
        assert "pilot_admission_close_rollback_due" in _eval(
            enabled=1,
            cohort={"entries": 200, "open": 40, "closed": 160, "n_eff": 160,
                    "net_usd": 0.0},
        )

    def test_no_rollback_alert_once_disabled(self):
        assert "pilot_admission_close_rollback_due" not in _eval(
            enabled=0,
            cohort={"entries": 200, "open": 40, "closed": 160, "n_eff": 160,
                    "net_usd": 0.0},
        )

    def test_gainers_gate_fires_at_100(self):
        assert "gainers_instrumentation_gate" in _eval(gainers_n=100)
        assert "gainers_instrumentation_gate" not in _eval(gainers_n=99)

    def test_no_pilot_anchor_fires_nothing(self):
        assert _eval(t0=None) == set()


class TestReadOnlyAndSafety:
    def test_it_never_imports_Database(self):
        """A read-only observer entering migration machinery is the exact
        failure class removed in #520.

        Asserted against the AST, not the text. The module's own docstring
        explains that it must not call `Database.initialize()`, so a substring
        grep fails on the correct disclaimer — the same self-referential trap
        that has now bitten four separate guards in this workstream.
        """
        import ast

        src = (REPO_ROOT / "scripts" / "pilot_threshold_watchdog.py").read_text("utf-8")
        tree = ast.parse(src)

        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                mod = getattr(node, "module", "") or ""
                names = [a.name for a in node.names]
                assert "Database" not in names, f"imports Database: {ast.dump(node)}"
                assert mod != "scout.db", "must not import from scout.db"
            if isinstance(node, ast.Call):
                fn = node.func
                if isinstance(fn, ast.Attribute) and fn.attr == "initialize":
                    raise AssertionError("calls .initialize() — migration machinery")
                if isinstance(fn, ast.Name) and fn.id == "Database":
                    raise AssertionError("constructs Database()")

    def test_it_opens_mode_ro(self):
        src = (REPO_ROOT / "scripts" / "pilot_threshold_watchdog.py").read_text("utf-8")
        assert "?mode=ro" in src and "uri=True" in src

    def test_it_performs_no_writes(self):
        src = (REPO_ROOT / "scripts" / "pilot_threshold_watchdog.py").read_text("utf-8")
        low = src.lower()
        for banned in ("update signal_params", "insert into", "delete from", "alter table"):
            assert banned not in low, f"mutation found: {banned}"

    def test_alert_is_plain_text(self):
        """Signal names carry underscores; MarkdownV1 mangles them and Telegram
        still returns HTTP 200 (§12b)."""
        src = (REPO_ROOT / "scripts" / "pilot_threshold_watchdog.py").read_text("utf-8")
        assert "parse_mode=None" in src

    async def test_readonly_connection_refuses_writes(self, tmp_path):
        import aiosqlite

        path = tmp_path / "ro.db"
        c = sqlite3.connect(path)
        c.execute("CREATE TABLE t (x INTEGER)")
        c.commit()
        c.close()

        conn = await aiosqlite.connect(f"file:{path}?mode=ro", uri=True)
        try:
            with pytest.raises(sqlite3.OperationalError) as exc:
                await conn.execute("CREATE TABLE nope (x INTEGER)")
            assert "readonly" in str(exc.value).lower()
        finally:
            await conn.close()


class TestDedup:
    def test_key_is_scoped_to_the_pilot_anchor(self):
        a = wd._state_key("K5_concurrency_tripwire", "2026-08-08T17:17:39+00:00")
        b = wd._state_key("K5_concurrency_tripwire", "2026-09-01T00:00:00+00:00")
        assert a != b, "a NEW pilot must re-arm every trigger"

    def test_same_trigger_same_pilot_dedups(self, tmp_path):
        from datetime import datetime, timezone

        k = wd._state_key("K5_concurrency_tripwire", "T0")
        assert not wd._already_fired(str(tmp_path), k)
        wd._mark_fired(str(tmp_path), k, datetime.now(timezone.utc))
        assert wd._already_fired(str(tmp_path), k)
