"""The read-only auditor must find contradictory state, and only that.

Each case plants one kind of bad row in a freshly migrated database and
checks the auditor reports exactly the machine concerned. The clean database
and the pre-migration (empty) database must both come back with nothing to
report.
"""

from __future__ import annotations

import importlib.util
import json
import sqlite3
from pathlib import Path

import pytest

from scout.db import Database
from scout.statemachines.audit import audit

REPO_ROOT = Path(__file__).resolve().parent.parent

_SPEC = importlib.util.spec_from_file_location(
    "audit_state_machines", REPO_ROOT / "scripts" / "audit_state_machines.py"
)
script = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(script)


@pytest.fixture
async def db_path(tmp_path):
    path = tmp_path / "audit.db"
    db = Database(path)
    await db.initialize()
    await db.close()
    return path


def _insert_minimal(conn: sqlite3.Connection, table: str, **overrides) -> None:
    """Insert a row supplying a placeholder for every NOT NULL column that has
    no default, so a test can set just the columns it is about."""
    values = dict(overrides)
    info = conn.execute(f"PRAGMA table_info({table})").fetchall()
    pk_columns = [row for row in info if row[5]]
    # A lone INTEGER primary key is the rowid and fills itself in; the members
    # of a composite key do not.
    rowid_alias = (
        pk_columns[0][1]
        if len(pk_columns) == 1 and "INT" in (pk_columns[0][2] or "").upper()
        else None
    )
    for _cid, name, ctype, notnull, default, pk in info:
        if name in values or name == rowid_alias or default is not None:
            continue
        if not notnull and not pk:
            continue
        kind = (ctype or "").upper()
        if "INT" in kind:
            values[name] = 0
        elif "REAL" in kind or "FLOA" in kind or "NUM" in kind:
            values[name] = 0.0
        else:
            values[name] = "x"
    cols = ", ".join(values)
    marks = ", ".join("?" for _ in values)
    conn.execute(
        f"INSERT INTO {table} ({cols}) VALUES ({marks})", tuple(values.values())
    )


def _audit(path: Path) -> dict:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return audit(conn)
    finally:
        conn.close()


def _entry(report: dict, machine: str) -> dict:
    return next(m for m in report["machines"] if m["machine"] == machine)


def test_fresh_database_has_nothing_to_report(db_path, capsys):
    assert script.main(["--db", str(db_path)]) == 0
    assert "0 with findings" in capsys.readouterr().out


def test_json_output_is_the_report(db_path, capsys):
    assert script.main(["--db", str(db_path), "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["machines_with_findings"] == []
    assert _entry(report, "signal_lane")["counts"]["enabled"] > 0


def test_missing_database_is_exit_2(tmp_path):
    assert script.main(["--db", str(tmp_path / "absent.db")]) == 2


def test_database_without_the_tables_reports_absent_not_findings(tmp_path):
    path = tmp_path / "empty.db"
    sqlite3.connect(path).close()
    report = _audit(path)
    assert report["machines_with_findings"] == []
    assert _entry(report, "paper_trade")["status"] == "table_absent"
    assert _entry(report, "ingest_source")["status"] == "not_stored_in_db"


def test_a_database_with_none_of_the_tables_is_not_a_clean_bill(tmp_path, capsys):
    """Pointing --db at the wrong file must not exit 0."""
    path = tmp_path / "empty.db"
    sqlite3.connect(path).close()
    assert script.main(["--db", str(path)]) == 2
    assert "is this the right database" in capsys.readouterr().err


def test_older_schema_is_reported_unreadable_and_exits_2(tmp_path, capsys):
    """A table that exists but lacks the state column must not look clean."""
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE paper_trades (id INTEGER PRIMARY KEY, token_id TEXT)")
    conn.commit()
    conn.close()

    report = _audit(path)
    assert _entry(report, "paper_trade")["status"] == "unreadable"
    assert "paper_trade" in report["machines_unreadable"]
    assert script.main(["--db", str(path)]) == 2
    out = capsys.readouterr().out
    assert "could not be read" in out
    assert "could not be fully read: " in out


def test_findings_take_precedence_over_unreadable_parts(tmp_path):
    path = tmp_path / "mixed.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE paper_trades (id INTEGER PRIMARY KEY, token_id TEXT)")
    conn.execute("CREATE TABLE live_trades (status TEXT, closed_at TEXT)")
    conn.execute("INSERT INTO live_trades VALUES ('closed_bogus', NULL)")
    conn.commit()
    conn.close()
    assert script.main(["--db", str(path)]) == 1


def test_unknown_status_value_is_reported(db_path, capsys):
    conn = sqlite3.connect(db_path)
    _insert_minimal(conn, "paper_trades", status="closed_bogus")
    conn.commit()
    conn.close()

    report = _audit(db_path)
    entry = _entry(report, "paper_trade")
    assert entry["undeclared"] == {"closed_bogus": 1}
    assert report["machines_with_findings"] == ["paper_trade"]
    assert script.main(["--db", str(db_path)]) == 1
    assert "NOT A DECLARED STATE" in capsys.readouterr().out


def test_anchor_sentinels_are_not_findings(db_path):
    conn = sqlite3.connect(db_path)
    _insert_minimal(conn, "paper_trades", status="kraken_pilot_anchor")
    conn.commit()
    conn.close()

    report = _audit(db_path)
    entry = _entry(report, "paper_trade")
    assert entry["status"] == "ok"
    assert entry["foreign"] == {"kraken_pilot_anchor": 1}
    # The anchor is not a trade, so the exit-ladder machine must not count it
    # (it has no remaining_qty and would otherwise read as a legacy trade).
    ladder = _entry(report, "exit_ladder")
    assert ladder["status"] == "ok"
    assert sum(ladder["counts"].values()) == 0


def test_a_non_text_status_is_reported_not_matched(db_path):
    conn = sqlite3.connect(db_path)
    _insert_minimal(conn, "paper_trades", status=b"open")
    conn.commit()
    conn.close()

    entry = _entry(_audit(db_path), "paper_trade")
    assert entry["counts"]["open"] == 0
    assert entry["undeclared"] == {"b'open'": 1}


def test_null_in_a_column_with_no_null_state_is_reported(tmp_path):
    path = tmp_path / "nulls.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE live_trades (status TEXT, closed_at TEXT)")
    conn.execute("INSERT INTO live_trades VALUES (NULL, NULL)")
    conn.commit()
    conn.close()
    assert _entry(_audit(path), "live_trade")["undeclared"] == {"(NULL)": 1}


def test_closed_trade_without_close_time_breaks_an_invariant(db_path):
    conn = sqlite3.connect(db_path)
    _insert_minimal(conn, "paper_trades", status="closed_tp", closed_at=None)
    conn.commit()
    conn.close()

    entry = _entry(_audit(db_path), "paper_trade")
    violated = [i["name"] for i in entry["invariants"] if i["status"] == "violated"]
    assert violated == ["closed_without_closed_at"]
    assert entry["counts"]["closed_tp"] == 1


def test_lane_enabled_but_carrying_a_suspension_is_reported(db_path):
    conn = sqlite3.connect(db_path)
    name = conn.execute("SELECT signal_type FROM signal_params LIMIT 1").fetchone()[0]
    conn.execute(
        "UPDATE signal_params SET suspended_at = '2026-11-19T00:00:00+00:00' "
        "WHERE signal_type = ?",
        (name,),
    )
    conn.commit()
    conn.close()

    report = _audit(db_path)
    assert _entry(report, "signal_lane")["undeclared"] == {
        "(matches no declared state)": 1
    }
    assert report["machines_with_findings"] == ["signal_lane"]


def test_suspended_lane_is_a_declared_state_not_a_finding(db_path):
    conn = sqlite3.connect(db_path)
    name = conn.execute("SELECT signal_type FROM signal_params LIMIT 1").fetchone()[0]
    conn.execute(
        "UPDATE signal_params SET enabled = 0, "
        "suspended_at = '2026-11-19T00:00:00+00:00' WHERE signal_type = ?",
        (name,),
    )
    conn.commit()
    conn.close()

    entry = _entry(_audit(db_path), "signal_lane")
    assert entry["status"] == "ok"
    assert entry["counts"]["suspended"] == 1


def test_kill_pointer_left_on_a_cleared_event_is_reported(db_path):
    conn = sqlite3.connect(db_path)
    cur = conn.execute(
        "INSERT INTO kill_events (triggered_at, triggered_by, killed_until, "
        "cleared_at, cleared_by) VALUES ('2026-11-18T00:00:00+00:00', 'manual', "
        "'2026-11-19T00:00:00+00:00', '2026-11-18T01:00:00+00:00', 'manual')"
    )
    conn.execute(
        "UPDATE live_control SET active_kill_event_id = ? WHERE id = 1",
        (cur.lastrowid,),
    )
    conn.commit()
    conn.close()

    report = _audit(db_path)
    entry = _entry(report, "kill_switch")
    assert entry["counts"] == {"inactive": 0, "engaged": 0, "expired_uncleared": 0}
    assert entry["undeclared"] == {"(matches no declared state)": 1}


def _point_kill_switch_at(db_path: Path, killed_until: str) -> None:
    conn = sqlite3.connect(db_path)
    cur = conn.execute(
        "INSERT INTO kill_events (triggered_at, triggered_by, killed_until) "
        "VALUES ('2020-01-01T00:00:00+00:00', 'manual', ?)",
        (killed_until,),
    )
    conn.execute(
        "UPDATE live_control SET active_kill_event_id = ? WHERE id = 1",
        (cur.lastrowid,),
    )
    conn.commit()
    conn.close()


def test_engaged_kill_switch_is_a_declared_state(db_path):
    _point_kill_switch_at(db_path, "2999-01-01T00:00:00+00:00")

    entry = _entry(_audit(db_path), "kill_switch")
    assert entry["status"] == "ok"
    assert entry["counts"] == {"inactive": 0, "engaged": 1, "expired_uncleared": 0}


def test_expired_but_uncleared_kill_is_told_apart_from_engaged(db_path):
    """Past killed_until the switch no longer halts orders, yet the pointer
    still blocks a new kill from engaging. That is its own state."""
    _point_kill_switch_at(db_path, "2020-01-02T00:00:00+00:00")

    entry = _entry(_audit(db_path), "kill_switch")
    assert entry["status"] == "ok"
    assert entry["counts"] == {"inactive": 0, "engaged": 0, "expired_uncleared": 1}


def test_suppressed_combo_with_no_parole_date_is_reported(db_path):
    conn = sqlite3.connect(db_path)
    _insert_minimal(
        conn, "combo_performance", suppressed=1, parole_at=None, window="30d"
    )
    conn.commit()
    conn.close()

    report = _audit(db_path)
    assert _entry(report, "combo_suppression")["undeclared"] == {
        "(matches no declared state)": 1
    }


def test_seven_day_combo_rows_are_outside_the_suppression_machine(db_path):
    """Only the 30-day row carries suppression; a 7-day row sharing the
    columns must be neither counted nor reported."""
    conn = sqlite3.connect(db_path)
    _insert_minimal(
        conn, "combo_performance", suppressed=1, parole_at=None, window="7d"
    )
    conn.commit()
    conn.close()

    entry = _entry(_audit(db_path), "combo_suppression")
    assert entry["status"] == "ok"
    assert sum(entry["counts"].values()) == 0


def _derived_machine(predicates: dict[str, str]):
    from scout.statemachines.model import START, Machine, State, Transition

    return Machine(
        name="probe",
        title="Probe",
        entity="probe.n",
        storage="derived",
        table="probe",
        summary="",
        vocabulary="none",
        enforcement="declared",
        states=tuple(
            State(name, name, name, initial=True, predicate=pred)
            for name, pred in predicates.items()
        ),
        transitions=tuple(
            Transition(START, name, "x.py::f", "made", "insert") for name in predicates
        ),
    )


def test_a_row_matching_two_states_or_none_is_reported():
    from scout.statemachines.audit import audit_machine

    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE probe (n INTEGER)")
    conn.executemany("INSERT INTO probe VALUES (?)", [(1,), (5,), (9,), (None,)])
    machine = _derived_machine({"low": "n < 6", "high": "n > 4"})
    assert machine.problems() == []

    entry = audit_machine(conn, machine)
    # 1 is low, 9 is high, 5 is both, and NULL compares as neither.
    assert entry["counts"] == {"low": 2, "high": 2}
    assert entry["undeclared"] == {
        "(matches no declared state)": 1,
        "(matches more than one declared state)": 1,
    }
    assert entry["status"] == "findings"


def test_null_counts_as_the_declared_null_state(db_path):
    conn = sqlite3.connect(db_path)
    _insert_minimal(conn, "predictions", outcome_class=None)
    conn.commit()
    conn.close()

    entry = _entry(_audit(db_path), "prediction_outcome")
    assert entry["status"] == "ok"
    assert entry["counts"]["pending"] == 1


@pytest.mark.parametrize("immutable", [False, True])
def test_the_script_opens_the_database_read_only(db_path, immutable):
    """The connection the script builds must refuse writes — this exercises
    the script's own URI, not a connection the test made for itself."""
    uri = script.database_uri(db_path, immutable=immutable)
    assert "mode=ro" in uri
    conn = sqlite3.connect(uri, uri=True)
    try:
        assert audit(conn)["machines_with_findings"] == []
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("UPDATE live_control SET active_kill_event_id = NULL")
    finally:
        conn.close()


def test_the_script_never_connects_any_other_way():
    source = (REPO_ROOT / "scripts" / "audit_state_machines.py").read_text(
        encoding="utf-8"
    )
    assert source.count("sqlite3.connect(") == 1
    assert "sqlite3.connect(database_uri(" in source


def test_database_path_with_uri_metacharacters_is_opened_correctly(tmp_path):
    """A '?' or '#' in the path must not be read as URI syntax (which would
    open a different file, or drop mode=ro)."""
    odd = tmp_path / "back up #1?.db"
    conn = sqlite3.connect(odd)
    conn.execute("CREATE TABLE live_trades (status TEXT, closed_at TEXT)")
    conn.execute("INSERT INTO live_trades VALUES ('closed_bogus', NULL)")
    conn.commit()
    conn.close()
    assert script.main(["--db", str(odd)]) == 1


def test_auditing_does_not_change_the_database_file(db_path):
    before = db_path.read_bytes()
    assert script.main(["--db", str(db_path)]) == 0
    assert db_path.read_bytes() == before
