"""Disposable isolated-copy validation. Never run against production DB paths."""

import argparse
import asyncio
import contextlib
import hashlib
import json
import os
import socket
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import unquote, urlparse

REAL_CONNECT = sqlite3.connect
POLICY_TABLES = {
    "signal_params",
    "chain_patterns",
    "live_control",
    "combo_performance",
    "tg_social_channels",
    "kill_events",
    "venue_overrides",
    "live_operator_overrides",
    "signal_params_audit",
    "signal_venue_correction_count",
}
POLICY_COLUMNS = {
    "enabled",
    "is_enabled",
    "suspended_at",
    "disabled_at",
    "live_eligible",
    "tg_alert_eligible",
    "is_protected_builtin",
    "active_kill_event_id",
    "suppressed",
    "sl_pct",
    "tp_pct",
    "position_size_usd",
}


def quote(value):
    return '"' + value.replace('"', '""') + '"'


def digest_rows(rows):
    # Only hashes leave the process; preserve SQLite types, never expose cell values.
    hashes = sorted(hashlib.sha256(repr(tuple(row)).encode()).digest() for row in rows)
    return hashlib.sha256(b"".join(hashes)).hexdigest()


def fingerprint(path, extra_tables=()):
    conn = REAL_CONNECT(Path(path).resolve().as_uri() + "?mode=ro", uri=True)
    try:
        deadline = time.monotonic() + 30
        conn.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
        schema = conn.execute(
            "SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name"
        ).fetchall()
        tables = {row[1] for row in schema if row[0] == "table"}
        protected = (POLICY_TABLES | set(extra_tables)) & tables
        for table in tables:
            columns = {
                row[1] for row in conn.execute(f"PRAGMA table_info({quote(table)})")
            }
            if columns & POLICY_COLUMNS:
                protected.add(table)
        policy = {}
        for table in sorted(
            protected | ({"paper_migrations", "schema_version"} & tables)
        ):
            rows = conn.execute(f"SELECT * FROM {quote(table)}").fetchall()
            policy[table] = {"count": len(rows), "sha256": digest_rows(rows)}
        return {
            "schema": digest_rows(schema),
            "policy": policy,
            "missing_critical_tables": sorted(POLICY_TABLES - tables),
            "schema_version": conn.execute("PRAGMA schema_version").fetchone()[0],
            "user_version": conn.execute("PRAGMA user_version").fetchone()[0],
        }
    finally:
        conn.close()


def database_path(value):
    value = os.fspath(value)
    if value.startswith("file:"):
        parsed = urlparse(value)
        if parsed.netloc:
            raise PermissionError("SQLite network authority refused")
        value = unquote(parsed.path)
        if os.name == "nt" and len(value) > 2 and value[0] == "/" and value[2] == ":":
            value = value[1:]
    if value == ":memory:":
        raise PermissionError("unexpected SQLite target")
    return Path(value).resolve()


def guarded_connect(original, root, writes, readonly=None, allowed=None):
    root = Path(root).resolve()
    readonly = readonly if readonly is not None else [False]

    def connect(database, *args, **kwargs):
        path = database_path(database)
        if not path.is_relative_to(root) or path == root or not path.is_file():
            raise PermissionError("SQLite target outside existing isolated copies")
        if allowed is not None and path not in allowed:
            raise PermissionError("only the mutable test copy may be opened")
        conn = original(database, *args, **kwargs)
        deadline = time.monotonic() + 120

        def authorizer(action, arg1, arg2, database_name, trigger):
            if action in {sqlite3.SQLITE_ATTACH, sqlite3.SQLITE_DETACH}:
                return sqlite3.SQLITE_DENY
            if action in {
                sqlite3.SQLITE_INSERT,
                sqlite3.SQLITE_UPDATE,
                sqlite3.SQLITE_DELETE,
            }:
                writes.append({"action": action, "table": arg1})
                if readonly[0]:
                    return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        conn.set_authorizer(authorizer)
        conn.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
        return conn

    return connect


def attest(source, manifest, baseline=False):
    source = Path(source).resolve()
    entries = manifest["baseline_files"] if baseline else manifest["expected_files"]
    allowed = set(entries) | set(manifest.get("metadata_allowlist", []))
    for target in source.rglob("*"):
        if target.is_symlink():
            raise ValueError("source symlink refused")
        if target.is_file() and target.relative_to(source).as_posix() not in allowed:
            raise ValueError(
                "unexpected source file: " + str(target.relative_to(source))
            )
    for path, expected in entries.items():
        target = source / path
        if target.is_symlink() or not target.is_file():
            raise ValueError("source missing or symlink: " + path)
        content = target.read_bytes()
        actual = hashlib.sha1(f"blob {len(content)}\0".encode() + content).hexdigest()
        if actual != expected["blob"]:
            raise ValueError("source blob mismatch: " + path)
        if os.name != "nt" and bool(target.stat().st_mode & 0o111) != (
            expected["mode"] == "100755"
        ):
            raise ValueError("source mode mismatch: " + path)


async def app_check(source, db_path, candidate):
    writes, readonly = [], [False]
    sqlite3.connect = guarded_connect(
        REAL_CONNECT, db_path.parent, writes, readonly, {db_path.resolve()}
    )

    def denied(*args, **kwargs):
        raise PermissionError("network/subprocess disabled in isolated validation")

    socket.socket.connect = denied
    socket.socket.connect_ex = denied
    socket.socket.sendto = denied
    socket.create_connection = denied
    socket.getaddrinfo = denied
    subprocess.Popen = denied
    os.environ.clear()
    os.environ.update(
        TELEGRAM_BOT_TOKEN="test", TELEGRAM_CHAT_ID="test", ANTHROPIC_API_KEY="test"
    )
    os.chdir(source)
    sys.path.insert(0, str(source))
    from dashboard import api
    import httpx

    origins = {}
    for name, module in list(sys.modules.items()):
        if (
            name == "scout"
            or name == "dashboard"
            or name.startswith(("scout.", "dashboard."))
        ):
            if getattr(module, "__file__", None):
                file = Path(module.__file__).resolve()
                if not file.is_relative_to(source):
                    raise ValueError("foreign application module: " + name)
                origins[name] = str(file.relative_to(source))
    app = api.create_app(str(db_path))
    statuses = {}
    try:
        await app.router.startup()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://isolated"
        ) as client:
            # Existing real GET reaches the cached OLD Database.initialize path.
            response = await client.get("/api/tg_alerts/recent?limit=1")
            statuses["existing_initializer_route"] = response.status_code
            if response.status_code != 200:
                raise ValueError("old-core initialization GET failed")
            init_writes = list(writes)
            writes.clear()
            readonly[0] = True
            paths = ["/api/status", "/api/trading/history?limit=20"]
            if candidate:
                paths += [
                    "/api/postmortems/moved-already?limit=25",
                    "/api/tg_alerts/outcomes",
                    "/api/trading/stop-shortfall-summary",
                ]
            for path in paths:
                response = await client.get(path)
                statuses[path] = response.status_code
                if response.status_code != 200:
                    raise ValueError("reader did not return200: " + path)
                payload = response.json()
                if path.endswith("stop-shortfall-summary"):
                    meta, data = payload["meta"], payload["data"]
                    if (
                        not meta["ok"]
                        or not meta["read_only"]
                        or meta["scope"] != "all_stored_closed_sl"
                    ):
                        raise ValueError("summary unavailable or wrong scope")
                    with contextlib.closing(
                        sqlite3.connect(db_path.as_uri() + "?mode=ro", uri=True)
                    ) as probe:
                        total = probe.execute(
                            "SELECT count(*) FROM paper_trades WHERE status='closed_sl'"
                        ).fetchone()[0]
                    if total != data["total_stop_rows"] or total != sum(
                        data[k]
                        for k in ("eligible_rows", "modeled_rows", "unavailable_rows")
                    ):
                        raise ValueError("summary population mismatch")
                    statuses["summary_counts"] = {
                        k: data[k]
                        for k in (
                            "total_stop_rows",
                            "eligible_rows",
                            "modeled_rows",
                            "unavailable_rows",
                        )
                    }
                if path == "/api/tg_alerts/outcomes":
                    if not payload["meta"]["ok"] or not payload["meta"]["read_only"]:
                        raise ValueError("Telegram outcomes unavailable")
                    if payload["total_events"] != sum(payload["outcomes"].values()):
                        raise ValueError("Telegram outcome partition mismatch")
            if writes:
                raise ValueError("read-only routes attempted DB mutation")
            for name, module in list(sys.modules.items()):
                if name.startswith(("scout.", "dashboard.")) and getattr(
                    module, "__file__", None
                ):
                    file = Path(module.__file__).resolve()
                    if not file.is_relative_to(source):
                        raise ValueError("foreign late application module: " + name)
                    origins[name] = str(file.relative_to(source))
            return {
                "statuses": statuses,
                "init_write_tables": sorted({w["table"] for w in init_writes}),
                "origins": origins,
            }
    finally:
        await app.router.shutdown()
        if api._scout_db is not None:
            await api._scout_db.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--copy", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--baseline", action="store_true")
    args = parser.parse_args()
    root = Path("/root/gecko-dashboard-release-20260914").resolve()
    db_path, source = args.copy.resolve(), args.source_root.resolve()
    if not db_path.is_relative_to(root) or not source.is_relative_to(root):
        raise ValueError("CLI accepts only the approved Linux scratch tree")
    if args.copy.is_symlink() or args.source_root.is_symlink() or not db_path.is_file():
        raise ValueError("existing private copy/source required")
    if (
        db_path.name not in {"baseline-init.sqlite", "candidate-init.sqlite"}
        or (source / ".env").exists()
    ):
        raise ValueError("immutable baseline/production environment must not be opened")
    if db_path.stat().st_mode & 0o077:
        raise ValueError("copy must have private0600 permissions")
    manifest = json.loads(args.manifest.read_text())
    if not manifest.get("verified"):
        raise ValueError("exact release candidate manifest must be verified first")
    attest(source, manifest, args.baseline)
    immutable = db_path.parent / "baseline.sqlite"
    if (
        immutable.is_symlink()
        or not immutable.is_file()
        or immutable.stat().st_mode & 0o077
    ):
        raise ValueError("private immutable baseline required beside test copy")
    before = fingerprint(db_path)
    original = fingerprint(immutable)
    if before != original:
        raise ValueError("test copy does not match immutable baseline")
    with contextlib.redirect_stdout(sys.stderr):
        result = asyncio.run(app_check(source, db_path, not args.baseline))
    # Include every observed DML target, including trigger writes, not only shortlist.
    targets = [t for t in result["init_write_tables"] if not t.startswith("sqlite_")]
    before = fingerprint(immutable, targets)
    after = fingerprint(db_path, targets)
    result.update(
        before=before,
        after=after,
        ok=before == after and not before["missing_critical_tables"],
        source_sha=(
            manifest["production_base_sha"]
            if args.baseline
            else manifest["release_sha"]
        ),
    )
    print(json.dumps(result, sort_keys=True))
    if not result["ok"]:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
