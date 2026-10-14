"""Social calls: parse a message, store it once, map it to a known token.

Only contract addresses resolve a call. Cashtags are stored for later
analysis but never resolve (a symbol names many tokens).
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import timedelta

from gecko.identity import EVM_CHAINS, is_valid_address, norm_address
from gecko.settings import GeckoSettings
from gecko.store import Store, canon_ts, parse_utc

_CASHTAG = re.compile(r"\$([A-Za-z][A-Za-z0-9_]{1,11})\b")
_EVM = re.compile(r"\b0x[a-fA-F0-9]{40}\b")
_SOL = re.compile(r"\b[1-9A-HJ-NP-Za-km-z]{32,44}\b")
SEED_HOURS = 24


def parse_call(text: str | None) -> tuple[list[str], list[str]]:
    if not text:
        return [], []
    tags = sorted({m.group(1).upper() for m in _CASHTAG.finditer(text)})
    found: list[tuple[int, str]] = []
    for m in _EVM.finditer(text):
        found.append((m.start(), m.group(0).lower()))
    for m in _SOL.finditer(text):
        if is_valid_address("solana", m.group(0)):
            found.append((m.start(), m.group(0)))
    addrs: list[str] = []
    for _, a in sorted(found):
        if a not in addrs:
            addrs.append(a)
    return tags, addrs


def event_id(handle: str, msg_id: int) -> str:
    return f"tg:{handle.lower()}:{msg_id}"


async def insert_call(
    store: Store, *, source_type, source_id, event_id, posted_at, text
) -> bool:
    tags, addrs = parse_call(text)
    if not tags and not addrs:
        return False
    cur = await store.conn.execute(
        "INSERT OR IGNORE INTO source_calls(source_type,source_id,event_id,posted_at,"
        "text_hash,cashtags_json,addresses_json) VALUES(?,?,?,?,?,?,?)",
        (
            source_type,
            source_id,
            event_id,
            canon_ts(posted_at),
            hashlib.sha256((text or "").encode()).hexdigest(),
            json.dumps(tags),
            json.dumps(addrs),
        ),
    )
    await store.conn.commit()
    return cur.rowcount == 1


async def _matches(store: Store, addr: str) -> set[str]:
    if addr.startswith("0x"):
        q = ",".join("?" * len(EVM_CHAINS))
        sql, args = f"SELECT token_id FROM tokens WHERE address=? AND chain IN ({q})", (
            addr,
            *sorted(EVM_CHAINS),
        )
    else:
        sql, args = (
            "SELECT token_id FROM tokens WHERE address=? AND chain='solana'",
            (addr,),
        )
    return {r[0] for r in await (await store.conn.execute(sql, args)).fetchall()}


async def resolve_calls(store: Store, now: str, *, since: str | None) -> int:
    sql = "SELECT id, addresses_json FROM source_calls WHERE token_id IS NULL AND addresses_json!='[]'"
    args: tuple = ()
    if since is not None:
        sql += " AND posted_at>=?"
        args = (canon_ts(since),)
    resolved = 0
    for cid, aj in await (await store.conn.execute(sql, args)).fetchall():
        tids: set[str] = set()
        for a in json.loads(aj):
            tids |= await _matches(store, a)
        if len(tids) != 1:
            continue  # unknown yet, or ambiguous across chains
        cur = await store.conn.execute(
            "UPDATE source_calls SET token_id=?, resolved_at=? WHERE id=? AND token_id IS NULL",
            (tids.pop(), canon_ts(now), cid),
        )
        resolved += cur.rowcount
    await store.conn.commit()
    return resolved


async def call_seeds(
    store: Store, settings: GeckoSettings, now: str
) -> dict[str, set[str]]:
    since = canon_ts((parse_utc(now) - timedelta(hours=SEED_HOURS)).isoformat())
    rows = await (
        await store.conn.execute(
            "SELECT addresses_json FROM source_calls WHERE token_id IS NULL "
            "AND addresses_json!='[]' AND posted_at>=? AND posted_at<=? ORDER BY posted_at DESC",
            (since, canon_ts(now)),
        )
    ).fetchall()
    addrs: list[str] = []
    for (aj,) in rows:
        for a in json.loads(aj):
            if a not in addrs:
                addrs.append(a)
    seeds: dict[str, set[str]] = {}
    for a in addrs[: settings.tg_seed_max]:
        chains = (
            [c for c in settings.chains if c in EVM_CHAINS]
            if a.startswith("0x")
            else (["solana"] if "solana" in settings.chains else [])
        )
        for c in chains:
            seeds.setdefault(c, set()).add(norm_address(c, a))
    return seeds
