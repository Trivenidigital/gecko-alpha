from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS tokens (
  token_id TEXT PRIMARY KEY, chain TEXT NOT NULL, address TEXT NOT NULL,
  symbol TEXT NOT NULL, name TEXT NOT NULL, cg_id TEXT, first_seen_at TEXT NOT NULL,
  first_source TEXT NOT NULL, pool_created_at TEXT, deployer TEXT,
  status TEXT NOT NULL DEFAULT 'candidate'
    CHECK (status IN ('candidate','active','disqualified','dead')),
  disqualify_reason TEXT, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS observations (
  id INTEGER PRIMARY KEY AUTOINCREMENT, token_id TEXT NOT NULL, source TEXT NOT NULL,
  observed_at TEXT NOT NULL, features_json TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_obs_token_time ON observations(token_id, observed_at);
CREATE INDEX IF NOT EXISTS idx_obs_time ON observations(observed_at);
CREATE TABLE IF NOT EXISTS source_calls (
  id INTEGER PRIMARY KEY AUTOINCREMENT, token_id TEXT, source_type TEXT NOT NULL
    CHECK (source_type IN ('telegram','x')), source_id TEXT NOT NULL,
  event_id TEXT NOT NULL UNIQUE, posted_at TEXT NOT NULL, text_hash TEXT NOT NULL,
  cashtags_json TEXT NOT NULL DEFAULT '[]', addresses_json TEXT NOT NULL DEFAULT '[]',
  resolved_at TEXT);
CREATE INDEX IF NOT EXISTS idx_calls_token ON source_calls(token_id, posted_at);
CREATE INDEX IF NOT EXISTS idx_calls_source_time ON source_calls(source_id, posted_at);
CREATE TABLE IF NOT EXISTS watchlist (
  token_id TEXT PRIMARY KEY, entered_at TEXT NOT NULL, rule_id TEXT NOT NULL,
  rank INTEGER NOT NULL, score REAL NOT NULL, features_at_entry_json TEXT NOT NULL,
  last_ranked_at TEXT NOT NULL, exited_at TEXT, exit_reason TEXT, entry_price REAL);
CREATE TABLE IF NOT EXISTS forward_returns (
  token_id TEXT NOT NULL, horizon_days INTEGER NOT NULL, due_at TEXT NOT NULL,
  entry_price REAL, price REAL, btc_entry REAL, btc_price REAL, ret REAL, ret_vs_btc REAL,
  attempts INTEGER NOT NULL DEFAULT 0, measured_at TEXT,
  PRIMARY KEY (token_id, horizon_days));
CREATE TABLE IF NOT EXISTS research_notes (
  id INTEGER PRIMARY KEY AUTOINCREMENT, token_id TEXT NOT NULL, created_at TEXT NOT NULL,
  model TEXT NOT NULL, schema_version INTEGER NOT NULL, note_json TEXT NOT NULL,
  cost_usd REAL NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT, job TEXT NOT NULL, started_at TEXT NOT NULL,
  finished_at TEXT NOT NULL, ok INTEGER NOT NULL, rows_written INTEGER NOT NULL DEFAULT 0,
  credits_used INTEGER NOT NULL DEFAULT 0, error TEXT);
CREATE INDEX IF NOT EXISTS idx_runs_job_time ON runs(job, finished_at);
"""

# Sources whose observations carry a market price (and maybe liquidity).
MARKET_SOURCES = ("dexscreener", "geckoterminal", "coingecko")
DEX_SOURCES = ("dexscreener", "geckoterminal")  # market sources with liquidity

TIME_COLUMN = {
    "tokens": "updated_at",
    "observations": "observed_at",
    "source_calls": "posted_at",
    "watchlist": "last_ranked_at",
    "forward_returns": "measured_at",
    "research_notes": "created_at",
    "runs": "finished_at",
}


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_utc(ts: str) -> datetime:
    dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def canon_ts(ts: str | None) -> str | None:
    """Canonical UTC ``%Y-%m-%dT%H:%M:%SZ`` form of any ISO-8601 string.

    Accepts a trailing ``Z``, ``+00:00`` or any other offset, and fractional
    seconds (truncated). A naive timestamp is taken as UTC. ``None`` passes
    through. Every stored timestamp and every comparison parameter goes
    through this so that lexical SQL comparisons are chronological.
    """
    if ts is None:
        return None
    return parse_utc(ts).strftime("%Y-%m-%dT%H:%M:%SZ")


def iso_plus(ts: str, days: int) -> str:
    return (parse_utc(ts) + timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")


class ReturnAlreadyMeasured(ValueError):
    """``set_return`` found no unmeasured row: missing, or already finalized."""


@dataclass
class Observation:
    token_id: str
    source: str
    observed_at: str
    features: dict


@dataclass
class WatchRow:
    token_id: str
    entered_at: str
    rule_id: str
    rank: int
    score: float
    features_at_entry: dict


@dataclass
class ReturnRow:
    token_id: str
    horizon_days: int
    due_at: str
    attempts: int


class Store:
    def __init__(self, conn: aiosqlite.Connection):
        self.conn = conn

    @classmethod
    async def open(cls, path: str) -> "Store":
        conn = await aiosqlite.connect(path)
        await conn.execute("PRAGMA journal_mode=WAL")
        await conn.execute("PRAGMA foreign_keys=ON")
        await conn.executescript(SCHEMA)
        cols = {
            r[1]
            for r in await (
                await conn.execute("PRAGMA table_info(watchlist)")
            ).fetchall()
        }
        if "entry_price" not in cols:  # databases created before the column existed
            await conn.execute("ALTER TABLE watchlist ADD COLUMN entry_price REAL")
        await conn.commit()
        return cls(conn)

    async def close(self) -> None:
        await self.conn.close()

    async def upsert_token(
        self,
        token_id,
        chain,
        address,
        symbol,
        name,
        first_seen_at,
        first_source,
        cg_id=None,
        pool_created_at=None,
    ) -> None:
        await self.conn.execute(
            "INSERT INTO tokens(token_id,chain,address,symbol,name,cg_id,first_seen_at,"
            "first_source,pool_created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(token_id) DO UPDATE SET "
            "cg_id=COALESCE(tokens.cg_id, excluded.cg_id), "
            # Earliest pool wins; canonical strings, so MIN is chronological.
            "pool_created_at=MIN(COALESCE(tokens.pool_created_at, excluded.pool_created_at),"
            " COALESCE(excluded.pool_created_at, tokens.pool_created_at)), "
            "updated_at=excluded.updated_at",
            (
                token_id,
                chain,
                address,
                symbol,
                name,
                cg_id,
                canon_ts(first_seen_at),
                first_source,
                canon_ts(pool_created_at),
                utcnow(),
            ),
        )
        await self.conn.commit()

    async def set_token_status(self, token_id, status, reason=None) -> None:
        await self.conn.execute(
            "UPDATE tokens SET status=?, disqualify_reason=?, updated_at=? WHERE token_id=?",
            (status, reason, utcnow(), token_id),
        )
        await self.conn.commit()

    async def add_observation(
        self, token_id, source, observed_at, features: dict
    ) -> int:
        cur = await self.conn.execute(
            "INSERT INTO observations(token_id,source,observed_at,features_json) VALUES(?,?,?,?)",
            (
                token_id,
                source,
                canon_ts(observed_at),
                json.dumps(features, sort_keys=True),
            ),
        )
        await self.conn.commit()
        return cur.lastrowid

    async def observations(
        self, token_id, *, as_of: str, source: str | None = None, limit: int = 500
    ) -> list[Observation]:
        sql = (
            "SELECT token_id,source,observed_at,features_json FROM observations "
            "WHERE token_id=? AND observed_at<=?"
        )
        args: list = [token_id, canon_ts(as_of)]
        if source is not None:
            sql += " AND source=?"
            args.append(source)
        sql += " ORDER BY observed_at DESC LIMIT ?"
        args.append(limit)
        rows = await (await self.conn.execute(sql, args)).fetchall()
        return [Observation(r[0], r[1], r[2], json.loads(r[3])) for r in rows]

    async def record_run(
        self,
        job,
        started_at,
        finished_at,
        ok,
        rows_written=0,
        credits_used=0,
        error=None,
    ) -> int:
        cur = await self.conn.execute(
            "INSERT INTO runs(job,started_at,finished_at,ok,rows_written,credits_used,error) "
            "VALUES(?,?,?,?,?,?,?)",
            (
                job,
                canon_ts(started_at),
                canon_ts(finished_at),
                1 if ok else 0,
                rows_written,
                credits_used,
                error,
            ),
        )
        await self.conn.commit()
        return cur.lastrowid

    async def last_write(self, table: str) -> str | None:
        col = TIME_COLUMN[table]
        row = await (
            await self.conn.execute(f"SELECT MAX({col}) FROM {table}")
        ).fetchone()
        return row[0]

    async def watchlist_upsert(
        self, token_id, entered_at, rule_id, rank, score, features_at_entry: dict
    ) -> None:
        entered_at = canon_ts(entered_at)
        await self.conn.execute(
            "INSERT INTO watchlist(token_id,entered_at,rule_id,rank,score,"
            "features_at_entry_json,last_ranked_at) VALUES(?,?,?,?,?,?,?) "
            "ON CONFLICT(token_id) DO UPDATE SET rank=excluded.rank, score=excluded.score, "
            "last_ranked_at=excluded.last_ranked_at",
            (
                token_id,
                entered_at,
                rule_id,
                rank,
                score,
                json.dumps(features_at_entry, sort_keys=True),
                entered_at,
            ),
        )
        await self.conn.commit()

    async def set_entry_price(self, token_id, price: float) -> None:
        """Freeze a watchlist entry's price; a stored one is never replaced."""
        await self.conn.execute(
            "UPDATE watchlist SET entry_price=? WHERE token_id=? AND entry_price IS NULL",
            (price, token_id),
        )
        await self.conn.commit()

    async def watchlist_exit(self, token_id, exited_at, reason) -> None:
        await self.conn.execute(
            "UPDATE watchlist SET exited_at=?, exit_reason=? WHERE token_id=? AND exited_at IS NULL",
            (canon_ts(exited_at), reason, token_id),
        )
        await self.conn.commit()

    async def active_watchlist(self) -> list[WatchRow]:
        rows = await (
            await self.conn.execute(
                "SELECT token_id,entered_at,rule_id,rank,score,features_at_entry_json "
                "FROM watchlist WHERE exited_at IS NULL ORDER BY rank"
            )
        ).fetchall()
        return [WatchRow(r[0], r[1], r[2], r[3], r[4], json.loads(r[5])) for r in rows]

    async def schedule_returns(
        self, token_id, entered_at, horizons=(1, 7, 30, 90, 180)
    ) -> None:
        await self.conn.executemany(
            "INSERT OR IGNORE INTO forward_returns(token_id,horizon_days,due_at) VALUES(?,?,?)",
            [(token_id, h, iso_plus(entered_at, h)) for h in horizons],
        )
        await self.conn.commit()

    async def due_returns(self, now: str) -> list[ReturnRow]:
        rows = await (
            await self.conn.execute(
                "SELECT token_id,horizon_days,due_at,attempts FROM forward_returns "
                "WHERE measured_at IS NULL AND due_at<=? ORDER BY due_at, horizon_days",
                (canon_ts(now),),
            )
        ).fetchall()
        return [ReturnRow(*r) for r in rows]

    async def set_return(
        self,
        token_id,
        horizon_days,
        entry_price,
        price,
        btc_entry,
        btc_price,
        measured_at,
    ) -> None:
        """Finalize one forward-return row. Write-once.

        Raises ``ValueError`` when ``price``, ``btc_entry`` or ``btc_price`` is
        missing (a ``btc_entry`` of 0 counts as missing): the measurement is
        retryable later, so the row must stay unmeasured. A missing or zero
        ``entry_price`` is allowed and finalizes the row with ``ret`` and
        ``ret_vs_btc`` NULL, because an entry price that never existed cannot
        appear later. Raises ``ReturnAlreadyMeasured`` (a ``ValueError``) if
        the row does not exist or was already measured.
        """
        if price is None or btc_entry is None or btc_price is None:
            raise ValueError(
                f"set_return {token_id} h={horizon_days}: "
                "price/btc_entry/btc_price required"
            )
        if btc_entry == 0:
            raise ValueError(f"set_return {token_id} h={horizon_days}: btc_entry is 0")
        if entry_price:
            ret = price / entry_price - 1.0
            ret_vs_btc = ret - (btc_price / btc_entry - 1.0)
        else:
            ret = None
            ret_vs_btc = None
        cur = await self.conn.execute(
            "UPDATE forward_returns SET entry_price=?,price=?,btc_entry=?,btc_price=?,ret=?,"
            "ret_vs_btc=?,measured_at=? WHERE token_id=? AND horizon_days=? "
            "AND measured_at IS NULL",
            (
                entry_price,
                price,
                btc_entry,
                btc_price,
                ret,
                ret_vs_btc,
                canon_ts(measured_at),
                token_id,
                horizon_days,
            ),
        )
        if cur.rowcount == 0:
            raise ReturnAlreadyMeasured(
                f"set_return {token_id} h={horizon_days}: row missing or already measured"
            )
        await self.conn.commit()

    async def bump_return_attempt(self, token_id, horizon_days) -> int:
        await self.conn.execute(
            "UPDATE forward_returns SET attempts=attempts+1 WHERE token_id=? AND horizon_days=?",
            (token_id, horizon_days),
        )
        await self.conn.commit()
        row = await (
            await self.conn.execute(
                "SELECT attempts FROM forward_returns WHERE token_id=? AND horizon_days=?",
                (token_id, horizon_days),
            )
        ).fetchone()
        return row[0]
