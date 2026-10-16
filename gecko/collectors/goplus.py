"""GoPlus token security + holders (keyless REST), one address per call.

The free tier silently drops every address after the first in a batch, and
answers an unknown token with ``code: 1`` and an empty result; both are
handled here so "unknown" can never read as "safe".
"""

from __future__ import annotations

import asyncio
from datetime import timedelta

import aiohttp
import structlog

from gecko.http import Pacer, UpstreamError, get_json
from gecko.identity import EVM_CHAINS, is_valid_address, norm_address
from gecko.jobs import register
from gecko.numeric import as_int, num
from gecko.runs import JobResult, PartialFailure, run_job
from gecko.settings import GeckoSettings
from gecko.store import Store, canon_ts, parse_utc, utcnow

log = structlog.get_logger("gecko.goplus")

GOPLUS_EVM_URL = "https://api.gopluslabs.io/api/v1/token_security/{chain_id}"
GOPLUS_SOL_URL = "https://api.gopluslabs.io/api/v1/solana/token_security"
CHAIN_IDS = {"ethereum": "1", "bsc": "56", "base": "8453"}
OK_CODES = (
    1,
    2,
)  # 2 = partial data: parsed, and any missing core flag reads as unknown
MAX_TAX = 0.10
# Launch taxes often drop within hours: a tax-only failure is re-read, not final.
TAX_FLAGS = {"buy_tax", "sell_tax"}
RENOUNCED = {
    "",
    "0x0000000000000000000000000000000000000000",
    "0x000000000000000000000000000000000000dead",
}
BURN = RENOUNCED | {"1nc1nerator11111111111111111111111111111111"}
# Solana AMM authorities that hold pool vaults (Raydium AMM v4, Raydium CPMM).
SOL_AMM_AUTHORITIES = {
    "5Q544fKrFoe6tsEbD7S8EmxGTJYAKtTVhAW5Q5pge4j1",
    "GpMZbSM2GgvTKHJirzeGfMFoaZ8UR2X7F4v8vHTvxFbL",
}


class GoPlusError(Exception):
    def __init__(self, code):
        super().__init__(f"goplus code {code}")
        self.code = code


def pick_result(chain: str, address: str, body) -> dict | None:
    """This token's entry, or None when GoPlus does not know the token."""
    if not isinstance(body, dict) or body.get("code") not in OK_CODES:
        raise GoPlusError(body.get("code") if isinstance(body, dict) else None)
    res = body.get("result")
    if res in (None, {}, []):
        return None
    if not isinstance(res, dict):
        raise GoPlusError(body.get("code"))
    want = norm_address(chain, address)
    for k, v in res.items():
        if norm_address(chain, str(k)) == want and isinstance(v, dict):
            return v
    return None


def _flag(v) -> bool | None:
    """GoPlus "0"/"1" (str or int) -> bool; anything else -> None."""
    s = str(v) if v is not None else ""
    return {"0": False, "1": True}.get(s)


def _status(v) -> bool | None:
    return _flag(v.get("status")) if isinstance(v, dict) else None


def _evm_security(r: dict) -> tuple[list[str], bool]:
    """Core flags are honeypot/cannot_sell_all/blacklist; taxes are optional.

    A mintable or proxy contract needs ``owner_address`` to be judged: when the
    key is absent or null the result is unknown, never renounced ("" = no owner).
    """
    owned = str(r.get("owner_address") or "").lower() not in RENOUNCED
    owner_needed = _flag(r.get("is_mintable")) or _flag(r.get("is_proxy"))
    owner_known = not owner_needed or r.get("owner_address") is not None
    checks = {
        "honeypot": _flag(r.get("is_honeypot")),
        "cannot_sell_all": _flag(r.get("cannot_sell_all")),
        "blacklist": _flag(r.get("is_blacklisted")),
    }
    flags = [k for k, v in checks.items() if v]
    for key in ("buy_tax", "sell_tax"):
        tax = num(r.get(key))
        if tax is not None and tax > MAX_TAX:
            flags.append(key)
    if owner_known and owned and _flag(r.get("is_mintable")):
        flags.append("mintable_owned")
    if owner_known and owned and _flag(r.get("is_proxy")):
        flags.append("proxy_owned")
    known = owner_known and all(v is not None for v in checks.values())
    return flags, known


def _sol_security(r: dict) -> tuple[list[str], bool]:
    checks = {
        "mintable": _status(r.get("mintable")),
        "freezable": _status(r.get("freezable")),
        "balance_mutable": _status(r.get("balance_mutable_authority")),
        "non_transferable": _flag(r.get("non_transferable")),
    }
    flags = [k for k, v in checks.items() if v]
    hook = r.get("transfer_hook")
    if isinstance(hook, list) and hook:
        flags.append("transfer_hook")
    return flags, all(v is not None for v in checks.values())


def _list(v) -> list:
    return v if isinstance(v, list) else []


def _top10(chain: str, r: dict) -> float | None:
    holders = r.get("holders")
    if not isinstance(holders, list) or not holders:
        return None
    # Pools only, NOT lp_holders: those hold the LP token (often the deployer EOA).
    skip = {
        norm_address(chain, str(d.get("pair") or d.get("id") or ""))
        for d in _list(r.get("dex"))
        if isinstance(d, dict)
    }
    skip |= BURN | SOL_AMM_AUTHORITIES
    total = 0.0
    for h in holders[:10]:
        if not isinstance(h, dict):
            continue
        addr = norm_address(chain, str(h.get("address") or h.get("account") or ""))
        if addr in skip or _flag(h.get("is_locked")):
            continue
        if chain in EVM_CHAINS and _flag(h.get("is_contract")):
            continue
        total += num(h.get("percent")) or 0.0
    return round(total * 100, 4)


def goplus_features(chain: str, r: dict) -> dict:
    if chain in EVM_CHAINS:
        flags, known = _evm_security(r)
        creator = str(r.get("creator_address") or "")
        deployer = (
            norm_address(chain, creator) if is_valid_address(chain, creator) else None
        )
    else:
        flags, known = _sol_security(r)
        deployer = None
        if _flag(r.get("trusted_token")):
            flags, known = [], True
    security_ok = False if flags else (True if known else None)
    structural = [k for k in flags if chain not in EVM_CHAINS or k not in TAX_FLAGS]
    return {
        "goplus_status": "ok",
        "security_ok": security_ok,
        "security_structural": False if structural else (True if known else None),
        "security_flags": sorted(flags),
        "holders": as_int(r.get("holder_count")),
        "top10_pct": _top10(chain, r),
        "deployer": deployer,
    }


UNKNOWN = {"goplus_status": "unknown"}
# An enrich row that carries a verdict; an ok read without one is not evidence.
KNOWN_VERDICT = (
    "json_extract(features_json,'$.goplus_status')='ok'"
    " AND json_type(features_json,'$.security_ok') IN ('true','false')"
)


async def calls_today(store: Store, now: str) -> int:
    day = canon_ts(now)[:10] + "T00:00:00Z"
    row = await (
        await store.conn.execute(
            "SELECT COALESCE(SUM(credits_used),0) FROM runs WHERE job='enrich' AND started_at>=?",
            (day,),
        )
    ).fetchone()
    return int(row[0])


async def enrich_queue(
    store: Store, settings: GeckoSettings, now: str
) -> list[tuple[str, str, str]]:
    stale = canon_ts(
        (parse_utc(now) - timedelta(hours=settings.gp_refresh_hours)).isoformat()
    )
    q = ",".join("?" * len(settings.chains))
    rows = await (
        await store.conn.execute(
            "SELECT t.token_id, t.chain, t.address, t.status, w.token_id IS NOT NULL, "
            "(SELECT MAX(observed_at) FROM observations o WHERE o.token_id=t.token_id "
            " AND o.source='enrich'), "
            "(SELECT COUNT(*) FROM observations o WHERE o.token_id=t.token_id "
            f" AND o.source='enrich' AND {KNOWN_VERDICT}), "
            "t.first_seen_at, "
            # 1 when the newest known verdict is a tax-only failure
            "(SELECT json_type(features_json,'$.security_ok')='false' AND "
            " json_type(features_json,'$.security_structural') IS NOT 'false' "
            " FROM observations o WHERE o.token_id=t.token_id AND o.source='enrich' "
            f" AND {KNOWN_VERDICT} ORDER BY observed_at DESC, id DESC LIMIT 1) "
            "FROM tokens t LEFT JOIN watchlist w ON w.token_id=t.token_id AND w.exited_at IS NULL "
            f"WHERE t.status IN ('candidate','active') AND t.chain IN ({q})",
            tuple(settings.chains),
        )
    ).fetchall()
    rows = [(*r[:8], r[3] == "candidate" and bool(r[8])) for r in rows]
    due = [
        r
        for r in rows
        if r[5] is None or (r[5] < stale and (r[3] == "active" or r[6] == 0 or r[8]))
    ]
    # never-enriched first, FIFO by arrival; then oldest last row; tax re-reads last
    due.sort(
        key=lambda r: (
            not r[4],
            r[3] != "active",
            r[5] is not None,
            r[8],
            r[5] or r[7],
            r[0],
        )
    )
    return [(r[0], r[1], r[2]) for r in due]


def _error_code(exc: Exception):
    """The code to mark a deterministic failure with, or None to retry next run.

    A GoPlus error code (not 1) or a 4xx other than 429 is deterministic; 429,
    5xx and transport errors (status 0) are transient.
    """
    if isinstance(exc, GoPlusError):
        return exc.code if exc.code != 1 else None
    status = getattr(exc, "status", 0)
    return status if 400 <= status <= 499 and status != 429 else None


async def _fetch(session, settings, chain, address):
    if chain in CHAIN_IDS:
        url = GOPLUS_EVM_URL.format(chain_id=CHAIN_IDS[chain])
    else:
        url = GOPLUS_SOL_URL
    body = await get_json(
        session, url, settings=settings, params={"contract_addresses": address}
    )
    return pick_result(chain, address, body)


async def enrich(
    store: Store,
    settings: GeckoSettings,
    session: aiohttp.ClientSession,
    now: str,
    *,
    sleep=asyncio.sleep,
    budget_now: str | None = None,
) -> JobResult:
    # The budget day is always the real one (budget_now is a test seam), same
    # ruling as the CoinGecko cap: an --as-of replay spends real calls today.
    budget = max(
        0, settings.gp_daily_cap - await calls_today(store, budget_now or utcnow())
    )
    queue = await enrich_queue(store, settings, now)
    todo = queue[: min(settings.gp_max_per_run, budget)]
    pacer = Pacer(settings.gp_min_interval_sec, sleep)
    calls = written = failed = disq = unknown = 0
    try:  # a crash (e.g. a lock timeout) still charges the calls already made
        for tid, chain, address in todo:
            await pacer.wait()
            calls += 1
            try:
                r = await _fetch(session, settings, chain, address)
            except (UpstreamError, GoPlusError) as exc:
                failed += 1
                log.warning(
                    "goplus_fetch_failed",
                    token_id=tid,
                    error=type(exc).__name__,
                    status=getattr(exc, "status", None),
                    code=getattr(exc, "code", None),
                )
                code = _error_code(exc)
                if code is not None:  # deterministic: retrying next run is waste
                    marker = {"goplus_status": "error", "code": code}
                    await store.add_observation(tid, "enrich", now, marker)
                continue
            except ValueError as exc:  # non-JSON body
                failed += 1
                log.warning(
                    "goplus_body_invalid", token_id=tid, error=type(exc).__name__
                )
                continue
            if r is None:
                await store.add_observation(tid, "enrich", now, dict(UNKNOWN))
                written, unknown = written + 1, unknown + 1
                continue
            f = goplus_features(chain, r)
            deployer = f.pop("deployer")
            if deployer:
                await store.set_deployer(tid, deployer)
            await store.add_observation(tid, "enrich", now, f)
            written, unknown = written + 1, unknown + (f["security_ok"] is None)
            if f["security_structural"] is False:
                await store.set_token_status(tid, "disqualified", "security_fail")
                log.info("token_security_fail", token_id=tid, flags=f["security_flags"])
                disq += 1
            elif f["security_ok"] is False:  # tax-only: re-read after refresh hours
                log.info("token_security_tax", token_id=tid, flags=f["security_flags"])
    except Exception as exc:  # noqa: BLE001
        raise PartialFailure(exc, credits_used=calls, rows_written=written) from exc

    log.info(
        "enrich_done",
        planned=len(todo),
        written=written,
        failed=failed,
        disqualified=disq,
        unknown_security=unknown,
        budget_left=budget - calls,
        enrich_queue_len=len(queue),
    )
    if todo and failed == len(todo):
        raise PartialFailure(
            RuntimeError("every GoPlus fetch failed"), credits_used=calls
        )
    return JobResult(rows_written=written, credits_used=calls)


@register("enrich")
async def enrich_job(settings: GeckoSettings, ns) -> int:
    now = canon_ts(ns.as_of) if ns.as_of else utcnow()

    async def body(store: Store, session: aiohttp.ClientSession) -> JobResult:
        return await enrich(store, settings, session, now)

    return await run_job("enrich", settings, body)
