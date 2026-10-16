"""deploy/: systemd units + timers, the env example, the backup script, gecko-query."""

import importlib.util
import os
import re
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from gecko.__main__ import JOBS, _load_jobs
from gecko.settings import GeckoSettings
from gecko.store import Store, parse_utc

ROOT = Path(__file__).resolve().parents[2]
DEPLOY = ROOT / "deploy"
GECKO = DEPLOY / "gecko"
SKILL = DEPLOY / "hermes" / "skills" / "gecko-query"
SECRETS = (
    "GECKO_TELEGRAM_BOT_TOKEN",
    "GECKO_TG_API_HASH",
    "GECKO_ANTHROPIC_API_KEY",
    "GECKO_COINGECKO_DEMO_API_KEY",
)
SCHEDULES = {  # job -> OnCalendar (spec §11 + the system-review amendments)
    "dexscreener": "*-*-* *:00/10:00",
    "geckoterminal": "*-*-* *:07/15:00",
    "geckoterminal-ohlcv": "*-*-* 04:20:00",
    "benchmark": "*-*-* *:35:00",
    "enrich": "*-*-* *:17:00",
    "rank": "*-*-* *:45:00",
    "ledger": "*-*-* 05:10:00",
    "freshness": "*-*-* *:05/30:00",
    "prune": "*-*-* 03:40:00",
    "research": "*-*-* 06:00:00",
    "digest": "*-*-* 07:00:00",
    "scorecard": "Mon *-*-* 08:00:00",
    "coingecko-markets": "*-*-* 02:30:00",
    "coingecko-hourly": "*-*-* *:50:00",
}
ON_FAILURE = "OnFailure=codex-systemd-failure-alert@%n.service"
posix_only = pytest.mark.skipif(sys.platform == "win32", reason="bash scripts")


def _lines(p: Path) -> list[str]:
    return [ln.strip() for ln in p.read_text(encoding="utf-8").splitlines()]


def _keys(p: Path) -> dict[str, str]:
    return dict(ln.split("=", 1) for ln in _lines(p) if "=" in ln and ln[0] != "#")


def test_every_timer_starts_a_registered_job_on_its_schedule():
    _load_jobs()
    seen = {}
    for t in sorted((GECKO / "timers").glob("*.timer")):
        k = _keys(t)
        assert k["Persistent"] == "true" and k["AccuracySec"] == "1min", t.name
        assert k["WantedBy"] == "timers.target", t.name
        if t.name == "gecko-db-backup.timer":
            assert k["Unit"] == "gecko-db-backup.service"
            assert k["OnCalendar"] == "*-*-* 03:24:00"  # off the :00/10 grid
            continue
        m = re.fullmatch(r"gecko-job@(.+)\.service", k["Unit"])
        assert m and m.group(1) in JOBS, f"{t.name}: {k['Unit']}"
        assert t.name == f"gecko-{m.group(1)}.timer"
        seen[m.group(1)] = k["OnCalendar"]
    assert seen == SCHEDULES
    dex = _keys(GECKO / "timers" / "gecko-dexscreener.timer")
    assert dex["RandomizedDelaySec"] == "60"


def test_every_service_pages_on_failure():
    services = sorted(GECKO.glob("*.service"))
    assert {p.name for p in services} == {
        "gecko-job@.service",
        "gecko-telegram-reader.service",
        "gecko-db-backup.service",
    }
    for p in services:
        assert ON_FAILURE in _lines(p), p.name


@pytest.mark.parametrize(
    "name", ["gecko-job@.service", "gecko-telegram-reader.service"]
)
def test_units_share_one_env_file_and_pin_the_db_path(name):
    lines = _lines(GECKO / name)
    env_file = lines.index("EnvironmentFile=/root/gecko-alpha/.env.gecko")
    db = lines.index("Environment=GECKO_DB_PATH=/root/gecko-alpha/gecko.db")
    assert env_file < db
    assert "WorkingDirectory=/root/gecko-alpha" in lines


def test_job_template_and_reader_contracts():
    job = _keys(GECKO / "gecko-job@.service")
    assert job["Type"] == "oneshot" and job["Nice"] == "5"
    assert job["ExecStart"] == "/root/.local/bin/uv run --frozen python -m gecko %i"
    assert job["TimeoutStartSec"] == "900"
    dropin = GECKO / "gecko-job@coingecko-markets.service.d" / "timeout.conf"
    assert _keys(dropin)["TimeoutStartSec"] == "1500"
    assert job["Environment"] == "PATH=/root/.local/bin:/usr/local/bin:/usr/bin:/bin"
    reader = _keys(GECKO / "gecko-telegram-reader.service")
    assert reader["RestartPreventExitStatus"] == "2"
    # a persistent crash stops after 5 starts/h; >=254 no page per auto-restart
    assert reader["StartLimitIntervalSec"] == "1h" and reader["StartLimitBurst"] == "5"
    assert reader["RestartMode"] == "direct"
    assert reader["Restart"] == "always" and reader["RestartSec"] == "30"
    assert reader["ExecStart"].endswith("python -m gecko telegram-reader")
    backup = _keys(GECKO / "gecko-db-backup.service")
    assert backup["ExecStart"] == "/usr/local/bin/gecko-db-backup.sh"


def test_no_secret_value_anywhere_under_deploy():
    keys = "|".join(SECRETS)  # [ \t]: a blank value must not reach the next line
    pat = re.compile(rf"^[ \t]*#?[ \t]*({keys})[ \t]*=[ \t]*(\S+)", re.M)
    for p in DEPLOY.rglob("*"):
        if p.is_file():
            hits = pat.findall(p.read_text(encoding="utf-8", errors="replace"))
            assert not hits, f"{p}: {[k for k, _ in hits]}"


def test_env_example_covers_exactly_the_settings_and_parses():
    example = GECKO / "env.gecko.example"
    names = set(re.findall(r"^#?\s*(GECKO_[A-Z0-9_]+)=", example.read_text(), re.M))
    fields = {f"GECKO_{f.upper()}" for f in GeckoSettings.model_fields}
    assert names == fields
    s = GeckoSettings(_env_file=str(example))
    assert s.db_path == "/root/gecko-alpha/gecko.db"
    assert (s.gt_min_interval_sec, s.ohlcv_max_per_run) == (6.0, 50)
    assert (s.obs_retention_days, s.log_format) == (60, "json")
    # commented out, not blank: an empty secret is SecretStr(""), not None
    assert s.coingecko_demo_api_key is None and s.tg_api_hash is None
    assert s.tg_channels == []  # set only after tg-login + the reader unit starts
    assert s.chains == ["solana", "base", "ethereum", "bsc"]
    # JSON values single-quoted: systemd EnvironmentFile= and dotenv agree on them
    for ln in _lines(example):
        value = ln.split("=", 1)[1] if "=" in ln else ""
        if value.startswith(("[", "{")):
            pytest.fail(f"unquoted JSON value: {ln}")


def test_backup_script_is_local_only():
    code = [
        ln
        for ln in _lines(GECKO / "gecko-db-backup.sh")
        if ln and not ln.startswith("#")
    ]
    assert not [ln for ln in code if "offhost" in ln or "rclone" in ln]
    assert "set -euo pipefail" in code


@posix_only
@pytest.mark.parametrize("script", ["install.sh", "gecko-db-backup.sh"])
def test_scripts_parse(script):
    assert subprocess.run(["bash", "-n", str(GECKO / script)]).returncode == 0


@posix_only
@pytest.mark.parametrize(
    "flags, verb, coingecko",
    [
        ([], "enable --now", False),
        (["--with-coingecko"], "enable --now", True),
        (["--no-start"], "enable", False),
    ],
)
def test_install_dry_run_enables_the_right_timers(flags, verb, coingecko):
    r = subprocess.run(
        ["bash", str(GECKO / "install.sh"), "--dry-run", *flags],
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0, r.stderr
    calls = re.findall(r"^\+ systemctl (enable(?: --now)?) (\S+)$", r.stdout, re.M)
    assert {v for v, _ in calls} == {verb}
    enabled = {u for _, u in calls}
    timers = {p.name for p in (GECKO / "timers").glob("*.timer")}
    cg = {"gecko-coingecko-markets.timer", "gecko-coingecko-hourly.timer"}
    assert enabled == (timers if coingecko else timers - cg)  # never the reader


@posix_only
def test_install_remove_dry_run_disables_everything_and_keeps_data():
    r = subprocess.run(
        ["bash", str(GECKO / "install.sh"), "--remove", "--dry-run"],
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0, r.stderr
    out = r.stdout
    for t in (GECKO / "timers").glob("*.timer"):
        assert f"+ systemctl disable --now {t.name}\n" in out
        assert f"+ rm -f /etc/systemd/system/{t.name}\n" in out
    assert "+ systemctl disable --now gecko-telegram-reader.service\n" in out
    assert "+ rm -f /etc/systemd/system/gecko-job@.service\n" in out
    assert "+ rm -f /usr/local/bin/gecko-db-backup.sh\n" in out
    rms = [ln for ln in out.splitlines() if ln.startswith("+ rm")]
    assert not [
        ln for ln in rms if "gecko.db" in ln or ".env" in ln or "/var/lib" in ln
    ]


@posix_only
@pytest.mark.skipif(os.name == "posix" and os.geteuid() == 0, reason="runs as root")
def test_install_refuses_to_run_as_non_root():
    r = subprocess.run(
        ["bash", str(GECKO / "install.sh")], capture_output=True, text=True
    )
    assert r.returncode == 1 and "run as root" in r.stderr
    assert "+ install" not in r.stdout  # refused before any action


# sqlite3 CLI stand-in: the subset gecko-db-backup.sh uses, plus fault knobs
SHIM = """#!{py}
import os, re, sqlite3, sys
db, cmds = sys.argv[1], sys.argv[2:]
# FAKE_VANISH: the source path disappears after the [ -f ] check (a race)
if os.environ.get("FAKE_VANISH") and any(x.startswith("VACUUM INTO") for x in cmds):
    os.remove(db.removeprefix("file:").split("?")[0])
c = sqlite3.connect(db, uri=db.startswith("file:"))
fakes = {{"PRAGMA quick_check;": "FAKE_QC", "PRAGMA journal_mode=DELETE;": "FAKE_MODE"}}
for cmd in cmds:
    if cmd.startswith(".timeout"):
        continue
    rows = c.execute(cmd).fetchall()  # no .backup: the script must use VACUUM INTO
    if os.environ.get(fakes.get(cmd, "-")):
        rows = [(os.environ[fakes[cmd]],)]
    for r in rows:
        print("|".join(map(str, r)))
c.commit()
"""


async def _backup_env(tmp_path: Path, shim: bool, **bins: str) -> tuple[Path, dict]:
    """The backup script repointed at tmp_path; a live WAL gecko.db; 3 old baks."""
    s = await Store.open(str(tmp_path / "gecko.db"))
    await s.close()
    script = (GECKO / "gecko-db-backup.sh").read_text(encoding="utf-8")
    for var, val in (("DB", "/gecko.db"), ("BAK_DIR", ""), ("SNAP_DIR", "/snap")):
        script = re.sub(rf"(?m)^{var}=.*$", f"{var}={tmp_path}{val}", script)
    (tmp_path / "b.sh").write_text(script, encoding="utf-8")
    for ts in ("20200101T000000Z", "20200102T000000Z", "20200103T000000Z"):
        (tmp_path / f"gecko.db.bak.{ts}").write_text("old")
    env = dict(os.environ)
    if shim:
        bins["sqlite3"] = SHIM.format(py=sys.executable)
    (tmp_path / "bin").mkdir()
    for name, body in bins.items():  # stand-ins ahead of the real tools on PATH
        (tmp_path / "bin" / name).write_text(body)
        (tmp_path / "bin" / name).chmod(0o755)
    env["PATH"] = f"{tmp_path / 'bin'}:{env['PATH']}"
    return tmp_path / "b.sh", env


def _check_snapshot(snap: Path) -> None:
    assert snap.stat().st_mode & 0o777 == 0o644
    with sqlite3.connect(f"file:{snap}?mode=ro&immutable=1", uri=True) as c:
        assert c.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
        assert c.execute("SELECT COUNT(*) FROM watchlist").fetchone() == (0,)


def _dated_baks(d: Path) -> list[str]:
    pat = r"gecko\.db\.bak\.\d{8}T\d{6}Z"
    return sorted(p.name for p in d.iterdir() if re.fullmatch(pat, p.name))


def _backup_runs(d: Path) -> list[tuple]:
    with sqlite3.connect(d / "gecko.db") as c:
        sql = "SELECT * FROM runs WHERE job='db-backup'"
        return [tuple(r) for r in c.execute(sql).fetchall()]


def _assert_failed_cleanly(d: Path) -> None:
    """A failed run: no new or rotated backup, no .partial left, no runs row."""
    assert _dated_baks(d) == [f"gecko.db.bak.2020010{i}T000000Z" for i in (1, 2, 3)]
    assert not list(d.glob("*.partial*"))  # the EXIT trap removed it
    assert not (d / "gecko.db").exists() or _backup_runs(d) == []


def _assert_runs_row(d: Path) -> None:
    rows = _backup_runs(d)
    assert len(rows) == 1, rows  # (id, job, started, finished, ok, rows, credits, err)
    _, _, started, finished, ok, written, credits, error = rows[0]
    assert (ok, written, credits, error) == (1, 1, 0, None)
    assert parse_utc(started) <= parse_utc(finished)  # gecko.store's own format
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", finished)


@posix_only
@pytest.mark.skipif(shutil.which("sqlite3") is None, reason="sqlite3 CLI")
async def test_backup_script_with_the_real_sqlite3_cli(tmp_path):
    script, env = await _backup_env(tmp_path, shim=False)
    (tmp_path / "gecko.db.bak.20200104T000000Z.partial").write_text("crashed")
    r = subprocess.run(["bash", str(script)], env=env, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    baks = sorted(p.name for p in tmp_path.glob("gecko.db.bak.*"))
    assert len(baks) == 3 and baks[0] == "gecko.db.bak.20200102T000000Z", baks
    _check_snapshot(tmp_path / "snap" / "gecko-snapshot.db")
    _assert_runs_row(tmp_path)
    assert not list(tmp_path.glob("*.partial*"))


@posix_only
async def test_backup_rotation_touches_only_dated_gecko_backups(tmp_path):
    script, env = await _backup_env(tmp_path, shim=True)
    decoys = ["gecko.db-wal-copy", "gecko.db.bak.notadate", "scout.db.bak.X"]
    for d in decoys:
        (tmp_path / d).write_text("keep")
    r = subprocess.run(["bash", str(script)], env=env, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    dated = _dated_baks(tmp_path)
    assert len(dated) == 3 and dated[0] == "gecko.db.bak.20200102T000000Z", dated
    assert all((tmp_path / d).read_text() == "keep" for d in decoys)
    assert (tmp_path / "gecko.db").stat().st_size > 0  # the live database survives
    _check_snapshot(tmp_path / "snap" / "gecko-snapshot.db")
    _assert_runs_row(tmp_path)


@posix_only
@pytest.mark.parametrize("fault", [{"FAKE_QC": "corrupt"}, {"FAKE_MODE": "wal"}])
async def test_backup_check_failure_replaces_nothing(tmp_path, fault):
    script, env = await _backup_env(tmp_path, shim=True)
    (tmp_path / "snap").mkdir()
    (tmp_path / "snap" / "gecko-snapshot.db").write_text("PREV")
    r = subprocess.run(
        ["bash", str(script)], env={**env, **fault}, capture_output=True, text=True
    )
    assert r.returncode != 0 and "gecko-db-backup:" in r.stderr
    assert (tmp_path / "snap" / "gecko-snapshot.db").read_text() == "PREV"
    _assert_failed_cleanly(tmp_path)


def _query_module():
    spec = importlib.util.spec_from_file_location("gecko_query", SKILL / "query.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
async def snapshot(tmp_path):
    """A snapshot as gecko-db-backup.sh makes it: Store schema, DELETE journal."""
    s = await Store.open(str(tmp_path / "gecko.db"))
    await s.close()
    src = sqlite3.connect(tmp_path / "gecko.db")
    src.execute(
        "INSERT INTO watchlist(token_id, entered_at, rule_id, rank, score,"
        " features_at_entry_json, last_ranked_at) VALUES"
        " ('solana:A','2026-10-01','F4',3,1.5,'{}','2026-10-01')"
    )
    src.execute(
        "INSERT INTO tokens(token_id, chain, address, symbol, name, first_seen_at,"
        " first_source, updated_at) VALUES ('solana:A','solana','A','AAA','A',"
        " '2026-10-01','dexscreener','2026-10-01')"
    )
    src.executemany(
        "INSERT INTO runs(job, started_at, finished_at, ok) VALUES ('rank',?,?,1)",
        [(f"2026-10-01T{i:04d}", f"2026-10-01T{i:04d}") for i in range(250)],
    )
    src.execute("INSERT INTO kv VALUES ('secret','x','2026-10-01')")
    src.commit()
    dst = sqlite3.connect(tmp_path / "snap.db")
    src.backup(dst)
    src.close()
    dst.execute("PRAGMA journal_mode=DELETE")
    dst.close()
    return f"file:{(tmp_path / 'snap.db').as_posix()}?mode=ro&immutable=1"


@pytest.mark.parametrize(
    "sql",
    [
        "DELETE FROM tokens",
        "SELECT * FROM kv",
        "PRAGMA x",
        "SELECT 1; SELECT 2",
        "SELECT token_id FROM watchlist; DELETE FROM watchlist",
        "SELECT * FROM tokens t, kv",
        "SELECT * FROM (SELECT 1 FROM tokens) AS x, kv",
        "SELECT * FROM watchlist WHERE token_id IN (SELECT key FROM kv)",
        "SELECT name FROM sqlite_master",
        "SELECT * FROM pragma_table_info('tokens')",
        "select 1 -- comment",
        "SELECT * FROM watchlist /* c */",
        "SELECT replace(token_id, 'a', 'b') FROM watchlist",  # forbidden token only
        "SELECT * FROM json_each('[1]')",  # a FROM source outside the allow-list
    ],
)
def test_query_refuses(sql, snapshot, capsys):
    assert _query_module().run(sql, snapshot) == 2
    out = capsys.readouterr()
    assert out.out == "" and "refused" in out.err


def test_query_refusal_exit_code_as_a_subprocess():
    r = subprocess.run(
        [sys.executable, str(SKILL / "query.py"), "DELETE FROM tokens"],
        capture_output=True,
        text=True,
    )
    assert r.returncode == 2 and "refused" in r.stderr


def test_query_accepts_a_select_and_forces_the_limit(snapshot, capsys, monkeypatch):
    mod = _query_module()
    seen = []
    real = sqlite3.connect

    def spy(*a, **k):
        conn = real(*a, **k)
        conn.set_trace_callback(seen.append)
        return conn

    monkeypatch.setattr(mod.sqlite3, "connect", spy)
    assert mod.run("SELECT token_id FROM watchlist", snapshot) == 0
    assert capsys.readouterr().out.strip() == '{"token_id": "solana:A"}'
    assert seen[-1] == "SELECT token_id FROM watchlist LIMIT 200"
    assert mod.run("select rank from watchlist limit 5", snapshot) == 0
    assert seen[-1] == "select rank from watchlist limit 5"


def test_query_never_prints_more_than_200_rows(snapshot, capsys):
    assert _query_module().run("SELECT id FROM runs LIMIT 1000", snapshot) == 0
    assert len(capsys.readouterr().out.splitlines()) == 200


def test_skill_examples_run(snapshot, capsys):
    text = (SKILL / "SKILL.md").read_text(encoding="utf-8").split("## Examples")[1]
    examples = re.findall(r'query\.py "(SELECT [^"]+)"', text)
    assert len(examples) >= 3
    for sql in examples:
        assert _query_module().run(sql, snapshot) == 0, sql
    assert '"rank": 3' in capsys.readouterr().out


@posix_only
async def test_backup_of_a_vanished_db_fails_and_creates_nothing(tmp_path):
    # the source is opened file:...?mode=ro, so a path removed after the
    # existence check fails instead of being created as an empty database
    script, env = await _backup_env(tmp_path, shim=True)
    r = subprocess.run(
        ["bash", str(script)],
        env={**env, "FAKE_VANISH": "1"},
        capture_output=True,
        text=True,
    )
    assert r.returncode != 0 and "backup failed" in r.stderr
    assert not (tmp_path / "gecko.db").exists()
    _assert_failed_cleanly(tmp_path)


def test_backup_copies_with_vacuum_into_not_dot_backup():
    # one read transaction: .backup restarts whenever another connection commits
    code = [ln for ln in _lines(GECKO / "gecko-db-backup.sh") if not ln[:1] == "#"]
    vacuum = [ln for ln in code if "VACUUM INTO" in ln]
    assert len(vacuum) == 1 and 'sqlite3 "file:$DB?mode=ro"' in vacuum[0]
    assert not [ln for ln in code if ".backup" in ln]


@posix_only
async def test_backup_refuses_without_free_space(tmp_path):
    df = "#!/bin/sh\necho 'Filesystem 1-blocks Used Available Capacity Mounted'\n"
    df += "echo '/dev/x 100 99 1 99% /'\n"  # 1 byte free
    script, env = await _backup_env(tmp_path, shim=True, df=df)
    r = subprocess.run(["bash", str(script)], env=env, capture_output=True, text=True)
    assert r.returncode != 0 and "free space 1 B < 2 x gecko.db + 1 GiB" in r.stderr
    _assert_failed_cleanly(tmp_path)
    assert not (tmp_path / "snap").exists()


@posix_only
@pytest.mark.parametrize("link_works", [True, False])
async def test_backup_snapshot_shares_the_backup_inode(tmp_path, link_works):
    bins = {} if link_works else {"ln": "#!/bin/sh\nexit 1\n"}  # cross-filesystem
    script, env = await _backup_env(tmp_path, shim=True, **bins)
    r = subprocess.run(["bash", str(script)], env=env, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    snap = tmp_path / "snap" / "gecko-snapshot.db"
    newest = tmp_path / _dated_baks(tmp_path)[-1]
    assert (snap.stat().st_ino == newest.stat().st_ino) is link_works
    assert snap.read_bytes() == newest.read_bytes()
    _check_snapshot(snap)
