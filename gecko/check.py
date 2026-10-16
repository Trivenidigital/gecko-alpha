from __future__ import annotations

import os
import shutil
from pathlib import Path

import aiohttp

from gecko.jobs import register
from gecko.notify import send_telegram
from gecko.settings import GeckoSettings
from gecko.store import ForeignDatabase, Store, utcnow
from gecko.telegram_reader import session_problem

GIB = 1024**3
DISK_FAIL_GIB = 5  # below this the next prune/backup can fill the disk
DISK_WARN_GIB = 10
# Writers whose freshness keys only start ticking after a first ok run.
WRITER_JOBS = (
    "dexscreener",
    "geckoterminal",
    "geckoterminal-ohlcv",
    "benchmark",
    "enrich",
    "rank",
    "ledger",
    "freshness",
    "prune",
)

Line = tuple[str, str, str]  # (OK|WARN|FAIL, check name, detail)


def _present(name: str, value, dark: str) -> Line:
    # Presence only: a key's value never reaches an output line.
    if value:
        return ("OK", name, "set")
    return ("WARN", name, f"not set: {dark}")


async def _db_checks(settings: GeckoSettings) -> list[Line]:
    path = Path(settings.db_path)
    if not path.is_absolute():  # never open it: that would create a stray file
        return [("FAIL", "db_path", f"not absolute: {settings.db_path!r}")]
    if not os.access(path.parent, os.W_OK):
        return [("FAIL", "db_path", f"parent {path.parent} missing or not writable")]
    try:
        store = await Store.open(str(path))
    except ForeignDatabase as exc:
        return [("FAIL", "db_path", str(exc))]
    except Exception as exc:  # noqa: BLE001 — reported as a FAIL line
        return [("FAIL", "db_path", f"open failed: {type(exc).__name__}")]
    try:
        (app_id,) = await (await store.conn.execute("PRAGMA application_id")).fetchone()
        rows = await (
            await store.conn.execute(
                "SELECT DISTINCT job FROM runs WHERE ok=1 AND job IN (%s)"
                % ",".join("?" * len(WRITER_JOBS)),
                WRITER_JOBS,
            )
        ).fetchall()
    finally:
        await store.close()
    out: list[Line] = [
        (
            ("OK", "db_path", f"{path} (application_id stamped)")
            if app_id == Store.APPLICATION_ID
            else ("FAIL", "db_path", f"{path} application_id={app_id} not stamped")
        )
    ]
    ran = {r[0] for r in rows}
    missing = [j for j in WRITER_JOBS if j not in ran]
    for job in missing:
        out.append(
            (
                "WARN",
                "jobs_ran_once",
                f"{job} has no ok run; never-run writers are invisible to freshness",
            )
        )
    if not missing:
        out.append(("OK", "jobs_ran_once", f"all {len(WRITER_JOBS)} writers ran ok"))
    return out


async def _telegram(settings: GeckoSettings, send: bool) -> Line:
    missing = [
        env
        for env, v in (
            ("GECKO_TELEGRAM_BOT_TOKEN", settings.telegram_bot_token),
            ("GECKO_TELEGRAM_HEALTH_CHAT_ID", settings.telegram_health_chat_id),
        )
        if not v
    ]
    if missing:
        return ("FAIL", "telegram", f"not set: {', '.join(missing)} (no health pages)")
    if not send:
        return ("OK", "telegram", "bot token and health chat set")
    timeout = aiohttp.ClientTimeout(total=settings.http_timeout_sec)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        delivered = await send_telegram(
            session,
            settings.telegram_bot_token.get_secret_value(),
            settings.telegram_health_chat_id,
            f"gecko check: ok {utcnow()}",
        )
    if delivered:
        return ("OK", "telegram", "test message delivered to the health chat")
    return ("FAIL", "telegram", "test message to the health chat not delivered")


def _disk(settings: GeckoSettings) -> Line:
    d = Path(settings.db_path).parent
    try:
        free = shutil.disk_usage(d).free
    except OSError as exc:
        return ("FAIL", "disk", f"cannot stat {d}: {type(exc).__name__}")
    detail = f"{free / GIB:.1f} GiB free at {d}"
    if free >= DISK_WARN_GIB * GIB:
        return ("OK", "disk", detail)
    if free >= DISK_FAIL_GIB * GIB:
        return ("WARN", "disk", f"{detail} (< {DISK_WARN_GIB} GiB)")
    return ("FAIL", "disk", f"{detail} (< {DISK_FAIL_GIB} GiB)")


async def run_checks(settings: GeckoSettings, *, send: bool) -> list[Line]:
    """Every check, in order. No network unless ``send``."""
    db = await _db_checks(settings)
    out: list[Line] = [db[0]]
    chains = settings.chains
    out.append(
        ("OK", "chains", ",".join(chains))
        if chains
        else ("WARN", "chains", "empty: nothing is collected")
    )
    out.append(await _telegram(settings, send))
    out.append(
        _present(
            "digest_chat",
            settings.telegram_digest_chat_id,
            "GECKO_TELEGRAM_DIGEST_CHAT_ID, digest dark",
        )
    )
    out.append(
        _present(
            "coingecko",
            settings.coingecko_demo_api_key,
            "GECKO_COINGECKO_DEMO_API_KEY, markets/hourly dark",
        )
    )
    out.append(
        _present(
            "anthropic",
            settings.anthropic_api_key,
            "GECKO_ANTHROPIC_API_KEY, research dark",
        )
    )
    problem = session_problem(settings, must_exist=True)
    if problem is None:
        out.append(("OK", "telegram_reader", f"{len(settings.tg_channels)} channel(s)"))
    elif settings.tg_channels:
        out.append(("FAIL", "telegram_reader", problem))
    else:
        out.append(("WARN", "telegram_reader", f"reader not activated: {problem}"))
    out.append(_disk(settings))
    jobs = db[1:] or [("WARN", "jobs_ran_once", "skipped: database unavailable")]
    out.extend(jobs)
    floor = 30 + settings.ledger_max_overdue_days + 7
    days = settings.obs_retention_days
    out.append(
        ("OK", "retention", f"obs_retention_days={days} >= {floor}")
        if days >= floor
        else (
            "WARN",
            "retention",
            f"obs_retention_days={days} < {floor} (30 + ledger_max_overdue_days + 7)",
        )
    )
    return out


@register("check")
async def check_job(settings: GeckoSettings, ns) -> int:
    # A diagnostic: no run_job, so it writes no runs row.
    lines = await run_checks(settings, send=bool(getattr(ns, "send", False)))
    for level, name, detail in lines:
        print(f"{level} {name}: {detail}")
    return 1 if any(level == "FAIL" for level, _, _ in lines) else 0
