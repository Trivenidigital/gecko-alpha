"""Pilot threshold watchdog — read-only observer for pre-registered gates.

The two things most likely to be wrong here are (a) the admission-writer
definition, which has already produced false readings twice via `pgrep -f`
self-matching, and (b) leaking mid-cohort analysis into an operational page.
Both are tested directly.
"""

from __future__ import annotations

import importlib.util
import sqlite3
from datetime import datetime, timedelta, timezone
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

    def test_K3_fires_on_the_FIRST_null_close(self):
        """*** NO MINIMUM-SAMPLE FLOOR. ***

        An earlier revision of this watchdog only evaluated K3 once 20 trades
        had closed. That silently weakened a FROZEN kill criterion: the
        registered rule is "NULL rate > 5% on new eligible closes → halt", with
        no sample floor. A single post-T0 close carrying NULL IS an
        instrumentation failure, and under the floor it could stay silent for
        another 19 closes — exactly the window in which the pilot's only
        deliverable stops being produced.

        This test previously asserted the floor. It was wrong, and a green
        suite around it made an unauthorized amendment look sanctioned.
        """
        assert "K3_instrumentation_broken" in _eval(
            cohort={"entries": 1, "open": 0, "closed": 1, "n_eff": 0, "net_usd": 0.0}
        )
        assert "K3_instrumentation_broken" in _eval(
            cohort={"entries": 2, "open": 0, "closed": 2, "n_eff": 1, "net_usd": 0.0}
        )

    def test_K3_silent_when_every_close_is_instrumented(self):
        """Discriminating control: 0% NULL must NOT fire."""
        assert "K3_instrumentation_broken" not in _eval(
            cohort={"entries": 30, "open": 0, "closed": 30, "n_eff": 30, "net_usd": 0.0}
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


# ======================================================================
# Re-review proofs — each covers a defect the previous green suite missed
# ======================================================================


def _seed_db(
    path,
    *,
    entries,
    open_n,
    closed_n,
    eligible,
    t0="2026-08-08T17:17:39+00:00",
    gainers=0,
):
    """Minimal schema the watchdog reads.

    Deliberately NOT built via `Database.initialize()` — the observer must work
    against the real column shape without the migration machinery it is
    forbidden to touch.
    """
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE paper_trades (id INTEGER PRIMARY KEY, token_id TEXT, "
        "signal_type TEXT, status TEXT, opened_at TEXT, pnl_usd REAL, "
        "pre_leg1_mae_pct REAL, mae_pct REAL)"
    )
    conn.execute(
        "CREATE TABLE signal_params (signal_type TEXT PRIMARY KEY, "
        "enabled INTEGER, drawdown_baseline_at TEXT)"
    )
    conn.execute(
        "CREATE TABLE paper_migrations (name TEXT PRIMARY KEY, cutover_ts TEXT)"
    )
    conn.execute("INSERT INTO signal_params VALUES ('losers_contrarian', 1, ?)", (t0,))
    conn.execute(
        "INSERT INTO paper_migrations VALUES ('bl_trade_adverse_excursion_v1', ?)",
        ("2026-08-03T22:05:50+00:00",),
    )
    opened = "2026-08-08T18:00:00+00:00"
    for i in range(entries):
        closed = i < closed_n
        conn.execute(
            "INSERT INTO paper_trades (token_id, signal_type, status, opened_at, "
            "pnl_usd, pre_leg1_mae_pct, mae_pct) VALUES (?,?,?,?,?,?,?)",
            (
                "tok%d" % i,
                "losers_contrarian",
                "closed_sl" if closed else "open",
                opened,
                0.0,
                -1.0 if (closed and i < eligible) else None,
                None,
            ),
        )
    for i in range(gainers):
        conn.execute(
            "INSERT INTO paper_trades (token_id, signal_type, status, opened_at, "
            "pnl_usd, pre_leg1_mae_pct, mae_pct) VALUES (?,?,?,?,?,?,?)",
            (
                "g%d" % i,
                "gainers_early",
                "closed_sl",
                "2026-08-05T00:00:00+00:00",
                0.0,
                None,
                -5.0,
            ),
        )
    conn.commit()
    conn.close()


class TestDeliveryPath:
    """*** THE ALERT PATH WAS NEVER EXECUTED BY ANY TEST. ***

    The first revision called
    ``send_telegram_message(bot_token, chat_id, text, parse_mode=None)``.
    The real contract is ``(text, session, settings, *, parse_mode,
    raise_on_failure, source)``. The first fired trigger would have raised
    TypeError before reaching the network — and the live production dry-run
    could not catch it, because ``fired=[]`` meant ``_send()`` never ran.
    """

    def test_it_matches_the_CURRENT_alerter_signature(self):
        import inspect

        from scout.alerter import send_telegram_message

        params = list(inspect.signature(send_telegram_message).parameters)
        assert params[:3] == ["text", "session", "settings"], (
            "alerter contract changed: %s" % params
        )
        for kw in ("parse_mode", "raise_on_failure", "source"):
            assert kw in params, "missing %s" % kw

    def test_raise_on_failure_is_set(self):
        """Load-bearing: the default alerter SWALLOWS non-200/network errors.

        Without the flag a rejected page is logged as delivered AND its dedup
        marker is written — permanently suppressing that trigger for the life of
        the pilot.
        """
        import ast

        src = (REPO_ROOT / "scripts" / "pilot_threshold_watchdog.py").read_text("utf-8")
        tree = ast.parse(src)

        # Asserted on the CALL NODE, not the source text. The docstring right
        # above the call explains why this flag is load-bearing, so a substring
        # grep matches the PROSE and passes even when the argument is deleted —
        # verified by mutation: removing the keyword left a text-based guard
        # green. Sixth self-referential-grep trip in this workstream.
        call = None
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "send_telegram_message"
            ):
                call = node
                break
        assert call is not None, "no send_telegram_message call found"

        kw = {k.arg: k.value for k in call.keywords}
        assert "raise_on_failure" in kw and kw["raise_on_failure"].value is True, (
            "raise_on_failure=True missing — a rejected page would be logged as "
            "delivered AND its dedup marker written, suppressing the trigger "
            "for the life of the pilot"
        )
        assert "parse_mode" in kw and kw["parse_mode"].value is None, (
            "parse_mode must be None; trigger names carry underscores"
        )
        assert "source" in kw, "source label missing"
        # Positional contract: (text, session, settings)
        assert len(call.args) == 3, (
            "expected 3 positional args (text, session, settings), got %d"
            % len(call.args)
        )

    def test_send_failure_leaves_NO_dedup_state(self, tmp_path, monkeypatch):
        """*** THE SILENT-SUPPRESSION BUG. ***

        If a failed send still wrote dedup state, the trigger would never page
        again for this pilot — the watchdog would go dark on exactly the
        condition it exists to report.
        """
        db = tmp_path / "s.db"
        _seed_db(db, entries=201, open_n=1, closed_n=200, eligible=200)

        async def _boom(_text):
            raise RuntimeError("telegram rejected")

        monkeypatch.setattr(wd, "_SEND", _boom)
        state = tmp_path / "state"
        rc = wd.main(["--db", str(db), "--enabled", "true", "--state-dir", str(state)])
        assert rc == 1, "a dispatch failure must exit non-zero"
        assert not list(state.glob("fired_*")), (
            "dedup state written despite a FAILED send — the trigger would be "
            "permanently suppressed"
        )

    def test_successful_send_writes_dedup_and_suppresses_the_repeat(
        self, tmp_path, monkeypatch
    ):
        db = tmp_path / "s.db"
        _seed_db(db, entries=201, open_n=1, closed_n=200, eligible=200)
        sent = []

        async def _ok(text):
            sent.append(text)

        monkeypatch.setattr(wd, "_SEND", _ok)
        state = tmp_path / "state"
        args = ["--db", str(db), "--enabled", "true", "--state-dir", str(state)]

        assert wd.main(args) == 5
        assert len(sent) == 1
        assert list(state.glob("fired_*")), "successful send must record dedup state"

        assert wd.main(args) == 5  # still detected...
        assert len(sent) == 1, "...but must NOT page twice for the same pilot"


class TestK4RealDatabase:
    """*** ZERO DAYS MUST SURVIVE THE SQL. ***

    A GROUP BY returns only dates that HAVE rows. Feeding ``[1,2,0,1,2]`` to the
    pure evaluator proves nothing about whether the reader can PRODUCE that 0 —
    and it could not: the missing bucket made K4 LESS likely to fire on the day
    it should fire hardest.
    """

    async def test_a_day_with_no_entries_reads_as_zero(self, tmp_path):
        import aiosqlite

        db = tmp_path / "k4.db"
        today = datetime.now(timezone.utc).date()
        t0 = (today - timedelta(days=10)).isoformat() + "T00:00:00+00:00"
        _seed_db(db, entries=0, open_n=0, closed_n=0, eligible=0, t0=t0)

        conn = sqlite3.connect(db)
        for n, count in ((5, 2), (4, 1), (3, 0), (2, 2), (1, 1)):
            d = today - timedelta(days=n)
            for i in range(count):
                conn.execute(
                    "INSERT INTO paper_trades (token_id, signal_type, status, "
                    "opened_at, pnl_usd, pre_leg1_mae_pct, mae_pct) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (
                        "t%d-%d" % (n, i),
                        "losers_contrarian",
                        "open",
                        "%sT12:00:00+00:00" % d.isoformat(),
                        0.0,
                        None,
                        None,
                    ),
                )
        conn.commit()
        conn.close()

        async with aiosqlite.connect("file:%s?mode=ro" % db, uri=True) as c:
            rate = await wd._entry_rate_days(c, t0, 5)

        assert rate == [2, 1, 0, 2, 1], (
            "expected the empty day to read as 0, got %s" % rate
        )

    async def test_a_pilot_younger_than_the_window_is_inapplicable(self, tmp_path):
        """None (window does not exist) — NOT an empty list, which would
        silently satisfy ``all()`` and fire K4 on a brand-new pilot."""
        import aiosqlite

        db = tmp_path / "young.db"
        t0 = datetime.now(timezone.utc).isoformat()
        _seed_db(db, entries=0, open_n=0, closed_n=0, eligible=0, t0=t0)
        async with aiosqlite.connect("file:%s?mode=ro" % db, uri=True) as c:
            assert await wd._entry_rate_days(c, t0, 5) is None

    def test_none_or_short_rate_never_fires_K4(self):
        assert "K4_entry_rate_collapsed" not in _eval(entry_rate=None)
        assert "K4_entry_rate_collapsed" not in _eval(entry_rate=[1, 2])


class TestGainersDedupAnchor:
    """Gainers dedups against ITS OWN run, not the losers revival timestamp."""

    def test_gainers_key_differs_by_run(self):
        a = wd._state_key("gainers_instrumentation_gate", "2026-08-03T22:05:50+00:00")
        b = wd._state_key("gainers_instrumentation_gate", "2026-08-08T17:17:39+00:00")
        assert a != b

    def test_a_new_losers_pilot_does_not_repage_a_done_gainers_gate(
        self, tmp_path, monkeypatch
    ):
        """*** THE CROSS-RUN LEAK. ***

        Keying every trigger to the losers PILOT_T0 meant a future losers
        revival re-armed — and therefore re-paged — an already-completed
        gainers gate, while a genuinely new gainers run could never re-arm.
        """
        db = tmp_path / "g.db"
        _seed_db(db, entries=0, open_n=0, closed_n=0, eligible=0, gainers=100)
        sent = []

        async def _ok(text):
            sent.append(text)

        monkeypatch.setattr(wd, "_SEND", _ok)
        state = tmp_path / "state"
        args = ["--db", str(db), "--enabled", "true", "--state-dir", str(state)]

        wd.main(args)
        assert any("gainers_instrumentation_gate" in t for t in sent)
        before = len(sent)

        conn = sqlite3.connect(db)
        conn.execute(
            "UPDATE signal_params SET drawdown_baseline_at = ? "
            "WHERE signal_type = 'losers_contrarian'",
            ("2026-12-25T00:00:00+00:00",),
        )
        conn.commit()
        conn.close()

        wd.main(args)
        assert len(sent) == before, (
            "a new losers pilot re-paged a completed gainers gate — the gainers "
            "trigger must dedup against the MAE cutover, not the losers anchor"
        )
