# BL-064 — TG Social Signals (paper-trade + alert from curated TG channels)

**Status:** Design (approved by srilu 2026-04-27, pending 5-reviewer pass)
**Date:** 2026-04-27
**Author:** Claude (with srilu)

## Problem statement

Curated Telegram channels run by crypto influencers regularly post early-stage token calls. The example that motivated this work: `@gem_detecter` posted `$RIV` (CA `2bpT3ksMdwdZ6DuHyq3FDUr7HDwvZ5DRZoT1fUPALJaH`) on 2026-04-06 at $3M mcap; the token sat at $60M mcap by the time of this design (~20×). gecko-alpha currently has no input from these sources — they live entirely outside the pipeline.

CoinGecko-derived signals (`first_signal`, `gainers_early`, `trending_catch`) reliably catch tokens AFTER they start moving on CG markets. By that point, the curated TG calls are typically hours-to-days ahead. The hypothesis is that watching 3-4 specific high-signal accounts can capture some of that lead time as actionable alerts and paper trades.

X (Twitter) was considered and dropped: API costs $200+/month for basic access; many of the same curators cross-post to TG; TG has a free real-time MTProto API.

## Goals

- **Primary:** capture token mentions from a curator-defined set of TG channels with <30s latency from post → alert.
- **Secondary:** open paper trades (`signal_type='tg_social'`) when a contract address is parseable and admission gates pass, so the signal source's hit rate is measurable in `combo_performance` over time.
- **Operational:** zero ongoing cost; one-time auth setup (~5 min); channels are mutable at runtime via small CLI without restart.

## Non-goals

- X (Twitter) ingestion. Dropped per cost.
- Manual forward-to-bot mode. The user chose auto-read via user-session.
- Live (real-money) trade dispatch. `tg_social` is paper-only, like every other signal type. BL-055 path is independent.
- ML message-of-interest classifier. v2 candidate after we see noise rates per channel.
- Co-occurrence boost (same token mentioned by 2+ channels in window → louder/larger trade). v2.
- Per-channel weighting. v1 is equal-weight; the schema includes `trade_eligible BOOLEAN` so a noisy channel can be flagged alert-only without being removed.
- TG channels we run ourselves (group chats, replies). Scope is broadcast-style channel posts.

## Approach

Telethon-based MTProto user-session listener subscribes to N channels (mutable list in DB). Each new message is parsed for cashtags, hashtags, contract addresses, and known DEX/explorer URLs. Resolved tokens are enriched, gated, and either alerted-only or alerted-plus-paper-traded. Restart catchup uses Telethon's `iter_messages(min_id=last_seen_msg_id)`. The pattern is validated against the broader open-source landscape — no clean drop-in alternative exists; Telethon is the only actively-deployable MTProto library in 2026 (Pyrogram and Hydrogram are abandoned/early/GPL respectively).

## Architecture

```
scout/social/telegram/
  __init__.py
  client.py          # Telethon TelegramClient wrapper, session load, get_me check
  bootstrap.py       # one-shot CLI: phone + code + 2FA → session file
  add_channel.py     # one-shot CLI to insert a row into tg_social_channels
  listener.py        # async task: catchup pass + live NewMessage handler + FloodWait wrap
  parser.py          # pure: text → ParsedMessage(cashtags, contracts, urls)
  resolver.py        # pure-async: CA → CG/DexScreener; ticker → CG search (alert-only)
  enricher.py        # mcap, price, vol, age via existing scout.ingestion clients +
                     # safety check via scout.safety
  dispatcher.py      # admission gates → optional paper-trade + always alert
  alerter.py         # Telegram alert formatting (uses scout.alerter.send_telegram)
  models.py          # Pydantic: ParsedMessage, ResolvedToken, AdmissionDecision
```

Module structure mirrors `scout/social/lunarcrush/` (the project's established pattern for research-style alerters). Each unit has one purpose and is independently testable.

**Listener loop integration:** launched alongside other long-running tasks in `scout/main.py` (same pattern as the perp watcher and CryptoPanic poller). One asyncio task; cancellable via standard signal handlers.

## Data flow

```
[Telegram MTProto user-session]
     │ NewMessage event (one of N watched channels)
     ▼
parser.parse_message(text) → ParsedMessage{cashtags, contracts, urls}
     │
     ├── empty → log no_signal_in_message; persist raw to tg_social_messages; done
     │
     ▼ persist (UNIQUE(channel_handle, msg_id) → idempotent on catchup re-runs)
     │
resolver:
   • for each contract: CG by-contract → DexScreener fallback → ResolvedToken
   • if no contracts, for each cashtag: CG search by-ticker → highest-mcap match → ResolvedToken (with `trade_eligible=False` because ticker-only)
   • multi-token tally rule (Telegram_Streamer pattern): tally cashtag+contract counts per resolved token; highest wins; ties → ALL listed in alert, no auto-trade
     │
     ▼ ResolvedToken[]
enricher: mcap/price/vol/age via existing CG client + scout.safety.is_safe()
     │
dispatcher admission gates (fail-closed, in order):
   1. dedup    : tg_social_signals row in last 24h for token? → no trade
   2. CA-resolved? (ticker-only → alert only, never trade)
   3. safety   : is_safe() raises or returns False → no trade
   4. junk     : existing util (wrapped/bridged blocked) → no trade
   5. mcap_max : current mcap < PAPER_MAX_MCAP ($500M) → no trade
   6. channel  : tg_social_channels.trade_eligible == 1 → no trade
   7. quota    : open `tg_social` trades < TG_SOCIAL_MAX_OPEN_TRADES (5) → no trade
     │
     ▼
ALWAYS alert (Telegram card with trade-vs-alert-only badge)
IF all 7 gates pass → paper.execute_buy(signal_type='tg_social', amount_usd=300)
     │
     ▼
persist tg_social_signals row (paper_trade_id NULL if alert-only or filtered)
update tg_social_channels.last_seen_msg_id
```

**Catchup on restart:** before the live event handler attaches, for each row in `tg_social_channels` (where `removed_at IS NULL`), call `iter_messages(channel_handle, min_id=last_seen_msg_id, limit=TG_SOCIAL_CATCHUP_LIMIT)`. Per Telethon, `min_id` is exclusive (returns messages with `id > min_id`), so passing the stored watermark fetches only newer messages. Replay each missed message through the same pipeline. UNIQUE constraint on `(channel_handle, msg_id)` makes this safe to re-run if catchup itself crashes mid-pass.

## Schema (migration in `scout/db.py`)

```sql
CREATE TABLE tg_social_channels (
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  channel_handle    TEXT NOT NULL UNIQUE,
  display_name      TEXT NOT NULL,
  trade_eligible    INTEGER NOT NULL DEFAULT 1,
  last_seen_msg_id  INTEGER NOT NULL DEFAULT 0,
  added_at          TEXT NOT NULL,
  removed_at        TEXT
);

CREATE TABLE tg_social_messages (
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  channel_handle    TEXT NOT NULL,
  msg_id            INTEGER NOT NULL,
  posted_at         TEXT NOT NULL,
  sender            TEXT,
  text              TEXT,
  cashtags          TEXT,                    -- JSON: ["$RIV"]
  contracts         TEXT,                    -- JSON: [{"chain":"solana","address":"..."}]
  urls              TEXT,
  parsed_at         TEXT NOT NULL,
  UNIQUE(channel_handle, msg_id)
);

CREATE TABLE tg_social_signals (
  id                     INTEGER PRIMARY KEY AUTOINCREMENT,
  message_pk             INTEGER NOT NULL,
  token_id               TEXT NOT NULL,
  symbol                 TEXT NOT NULL,
  contract_address       TEXT,
  chain                  TEXT,
  mcap_at_sighting       REAL,
  source_channel_handle  TEXT NOT NULL,
  alert_sent_at          TEXT,
  paper_trade_id         INTEGER,
  created_at             TEXT NOT NULL,
  FOREIGN KEY (message_pk) REFERENCES tg_social_messages(id)
);
```

Indexes (all in the migration step, NOT in `_create_tables` per BL-060 lesson):

```sql
CREATE INDEX idx_tg_social_signals_token_created
  ON tg_social_signals(token_id, created_at);
CREATE INDEX idx_tg_social_signals_channel_created
  ON tg_social_signals(source_channel_handle, created_at);
CREATE INDEX idx_tg_social_messages_channel_msgid
  ON tg_social_messages(channel_handle, msg_id);
```

A `paper_migrations` row `bl064_tg_social` is inserted at migration time. A/B comparisons must scope `opened_at >= cutover_ts`, NOT row age (per BL-060 mid-flight migration lesson).

## Configuration (`scout/config.py`)

```python
TG_SOCIAL_ENABLED: bool = False
TG_SOCIAL_API_ID: int = 0                                # my.telegram.org
TG_SOCIAL_API_HASH: SecretStr | None = None              # my.telegram.org
TG_SOCIAL_PHONE_NUMBER: str = ""                         # bootstrap only, not runtime
TG_SOCIAL_SESSION_PATH: Path = Path("./tg_social.session")
TG_SOCIAL_DEDUP_HOURS: int = 24
TG_SOCIAL_MAX_OPEN_TRADES: int = 5                       # separate quota
PAPER_TG_SOCIAL_TRADE_AMOUNT_USD: float = 300.0
TG_SOCIAL_CATCHUP_LIMIT: int = 200                       # max msgs/channel on restart
TG_SOCIAL_FLOOD_WAIT_MAX_SEC: int = 300                  # circuit-break cap
TG_SOCIAL_CHANNEL_RELOAD_INTERVAL_SEC: int = 300         # how often listener re-reads
                                                         # tg_social_channels for adds/removes
```

**Cross-field validator** (`@model_validator(mode='after')`): if `TG_SOCIAL_ENABLED=True` then `TG_SOCIAL_API_ID > 0` AND `TG_SOCIAL_API_HASH` is non-None AND `TG_SOCIAL_SESSION_PATH.exists()`. Failure raises with the exact bootstrap command in the error string.

**Per-field validators**: `TG_SOCIAL_DEDUP_HOURS > 0`, `TG_SOCIAL_MAX_OPEN_TRADES >= 1`, `PAPER_TG_SOCIAL_TRADE_AMOUNT_USD > 0`, `TG_SOCIAL_FLOOD_WAIT_MAX_SEC > 0`, `TG_SOCIAL_CATCHUP_LIMIT >= 0`, `TG_SOCIAL_CHANNEL_RELOAD_INTERVAL_SEC >= 60` (60 second floor to prevent thrash).

Channel list lives in the `tg_social_channels` DB table, not env. Adds/removes are picked up by the listener within `TG_SOCIAL_CHANNEL_RELOAD_INTERVAL_SEC` (default 5 min) without service restart — see §Operations.

## Authentication bootstrap (one-time)

CLI: `python -m scout.social.telegram.bootstrap`

1. Reads `TG_SOCIAL_API_ID` + `TG_SOCIAL_API_HASH` from `.env`
2. Prompts for phone (or reads `TG_SOCIAL_PHONE_NUMBER`)
3. Telegram sends auth code via the Telegram app → user enters
4. If 2FA enabled → password prompt
5. Writes `TG_SOCIAL_SESSION_PATH`
6. Calls `client.get_me()` → prints `username`, `id` for confirmation
7. Exits

User-facing setup checklist (also in operator runbook):
1. Visit https://my.telegram.org → API Development tools → create app → record `api_id` + `api_hash`
2. Add to `.env`: `TG_SOCIAL_API_ID=...`, `TG_SOCIAL_API_HASH=...`
3. `python -m scout.social.telegram.bootstrap` (one-time interactive)
4. Add channels: `python -m scout.social.telegram.add_channel @gem_detecter "Gem Detector"`
5. Set `TG_SOCIAL_ENABLED=True` in `.env`
6. Restart `gecko-pipeline`

## Error handling (layer-by-layer)

| Layer | Failure | Action |
|---|---|---|
| Auth | missing creds / session file | Fail fast at startup; error message includes the exact `python -m scout.social.telegram.bootstrap` command. Raise `TgSocialAuthError`. |
| MTProto | disconnect | Telethon auto-reconnects with backoff; we log each disconnect. After N consecutive failures (threshold = 5), emit a heartbeat-style monitor event for operator visibility. |
| FloodWait | rate-limited by Telegram | Catch `FloodWaitError`; sleep `min(.seconds + 1, TG_SOCIAL_FLOOD_WAIT_MAX_SEC)`; if cap exceeded → circuit-break (stop listener until next service restart) + Telegram alert via `scout.alerter`. |
| Channel access | not-found / kicked / banned | Catch the specific Telethon error class for that channel only; mark `tg_social_channels.removed_at = now()`; alert user once via Telegram; continue serving the remaining channels. |
| Resolution | CA/ticker doesn't resolve via CG or DexScreener | Log `tg_social_resolution_failed` with the unresolvable string; persist message; emit alert tagged `[unresolved]`; **do NOT dispatch trade**. Raise `TgSocialResolutionError` upstream of the alert path so the alert surfaces it. |
| Safety | `scout.safety.is_safe` raises (network blip, GoPlus 5xx) | Log + alert + **skip trade dispatch**. Per BL-063 review feedback: never fail-open. |
| Junk filter / mcap cap / dedup | reject | Log specific gate name (`tg_social_admission_blocked_junk`, etc.); alert STILL goes (with badge "alert-only: blocked by gate"); no trade. |
| DB write | error | Propagate to listener loop; listener catches, logs, retries the next message rather than crashing the loop. |

New domain exceptions in `scout/exceptions.py`:
- `TgSocialAuthError(ScoutError)` — missing creds, missing session, invalid 2FA
- `TgSocialResolutionError(ScoutError)` — CA/ticker not resolvable

## Testing strategy

**Unit (pure):**
- `tests/test_tg_social_parser.py` — cashtag regex, EVM `0x` regex, Solana base58 regex (32-44 chars, alphabet-restricted), URL extraction (dexscreener.com/birdeye.so/photon-sol.tinyastro.io), multi-token tally rule, edge cases (CA inside URL, multi-line message, emoji-only message, message with no signal-tokens at all)
- `tests/test_tg_social_resolver.py` — CA→CG by-contract hit, CA→DexScreener fallback when CG misses, ticker→CG search ambiguous handling, ticker-only resolution returns `trade_eligible=False`
- `tests/test_tg_social_dispatcher.py` — dedup window (24h boundary tests), slot quota (own pool), `channel.trade_eligible` toggle, mcap upper cap, junk filter, safety fail-closed, alert-always-trade-conditional invariant
- `tests/test_tg_social_alerter.py` — alert format with trade-dispatched badge vs alert-only badge vs unresolved badge

**Integration:**
- `tests/test_tg_social_listener.py` — mock Telethon `NewMessage` event → end-to-end through parser/resolver/enricher/dispatcher → assert DB rows + `scout.alerter.send_telegram` call + `paper.execute_buy` call (when applicable)
- `tests/test_tg_social_catchup.py` — set `last_seen_msg_id`, mock `iter_messages(min_id=...)`, replay through pipeline, assert UNIQUE-constraint idempotency on a re-run

**Migration:**
- `tests/test_db_migration_bl064.py` — three new tables, indexes, `paper_migrations` row inserted, idempotent re-run

**Mocks:**
- `unittest.mock.AsyncMock` for `TelegramClient` (events, `iter_messages`, `get_me`)
- `aioresponses` for CG/DexScreener
- `tmp_path` for aiosqlite fixture (existing project pattern)

**Regression gate:** all existing `tests/test_paper_*.py` and `tests/test_db_*.py` pass unchanged. The new `signal_type='tg_social'` adds a new `combo_key` to `combo_performance` rollups (must include `closed_moonshot_trail`, `closed_tp`, etc. via `CLOSED_COUNTABLE_STATUSES`).

## Operations

**Adding a channel at runtime** (no restart needed):
```bash
python -m scout.social.telegram.add_channel @gem_detecter "Gem Detector"
# Or with trade_eligible=False for noisy channels:
python -m scout.social.telegram.add_channel @noisy_channel "Noisy" --alert-only
```

The listener detects the new row on its next 5-minute reload-channels heartbeat (configurable). On detection, it joins the channel via Telethon and runs catchup with `last_seen_msg_id=0` (which means "fetch the most recent `TG_SOCIAL_CATCHUP_LIMIT` messages") then attaches the live handler.

**Removing a channel:**
```bash
python -m scout.social.telegram.add_channel --remove @noisy_channel
```
Sets `removed_at = now()` (soft-delete). Listener stops processing on next heartbeat.

**Disabling a channel for trade dispatch only** (alerts continue):
```bash
python -m scout.social.telegram.add_channel --no-trade @noisy_channel
```
Toggles `trade_eligible = 0`.

## Rollout

1. Merge with `TG_SOCIAL_ENABLED=False` (current default). Schema migrated on prod via existing migration pipeline. Flag-off means no Telethon connection, no listener task; zero side-effect.
2. Run bootstrap on prod: `python -m scout.social.telegram.bootstrap`. Confirms session works; prints `get_me()` for verification.
3. Add 1 channel: `python -m scout.social.telegram.add_channel @<first_channel> "<display>"`.
4. Set `TG_SOCIAL_ENABLED=True` in prod `.env`. Restart `gecko-pipeline`.
5. Monitor for 24h: watch `tg_social_messages` row count + `tg_social_signals` count + first paper_trade_id assigned. Verify alerts arrive in Telegram.
6. Add remaining 2-3 channels.
7. Soak 14 days. Review `combo_performance` for `tg_social` combo_key vs other signals.
8. Decision gate: if `tg_social` combo win-rate >= 40% and avg pnl_pct > 0 over 14d → keep on; else flag-off and re-evaluate per channel.

## Risks + mitigations

| Risk | Mitigation |
|---|---|
| Telethon archived (Feb 2026), might bit-rot | Pin to last v1 stable in `pyproject.toml`. Document the archive risk. Fallback path (NOT Hydrogram — GPL + pre-1.0) is to fork Telethon at the pinned version or migrate to `mtproto-core`. |
| Channel deletes / curator turns hostile | Soft-delete via `removed_at`. No hard channel state in code; everything is DB-driven. |
| MTProto user-session ToS gray-zone | Low-rate listener (3-4 channels, no spam) is well within accepted use; widely deployed pattern (informer has 1.6k stars doing this). Bot accounts can't read most channels, so user-session is the only viable path. |
| Curator posts a CA that's a honeypot or scam | `scout.safety.is_safe` (GoPlus) is the same check existing paper trading uses. Wrong CAs slip through occasionally; bounded loss = $300 (single paper trade) per occurrence. |
| Rate spam from one channel fills the slot pool | Separate quota (`TG_SOCIAL_MAX_OPEN_TRADES = 5`) + 24h dedup per token + can flag channel `trade_eligible=False` reactively without removing it. |
| Resolution picks the wrong "WIF" on ticker-only posts | Ticker-only never dispatches a trade (gate 2 in dispatcher). Alert-only with the resolved-best-mcap candidate flagged "ticker-only verify". Cross-chain ambiguity (same ticker on multiple chains) is handled by surfacing all candidates in the alert when the by-mcap top-2 are within 2× of each other; never auto-trades in that case. |
| Pre-cutover paper trades mixed into A/B | `paper_migrations.bl064_tg_social` row + scope `combo_performance` queries to `opened_at >= cutover_ts` (BL-060 lesson). |
| FloodWait disrupts pipeline | Wrapper catches `FloodWaitError`, sleeps with cap; circuit-breaks if cap exceeded so the rest of `gecko-pipeline` doesn't stall. |
| Session file accidentally committed to git | Add `*.session` to `.gitignore` in this PR. Document in operator runbook that the session file authenticates as the user — treat like `.env`. |

## Resolved open questions

1. **Ingestion path**: Telethon user-session, NOT bot account (bots can't read most channels) and NOT manual forward (user chose auto-read).
2. **Source**: TG only. X dropped per cost.
3. **Lifecycle**: A+B — alert always, paper trade when CA-resolved + gates pass.
4. **CA required for trade dispatch**: yes. Ticker-only mentions are alert-only.
5. **Mcap floor**: skipped (the user's curators specifically pick early-stage gems; the existing safety check is the rug filter).
6. **Sizing**: $300/trade (smaller than standard $1000; untested signal source).
7. **Slot pool**: separate quota of 5 trades, on top of the existing 10-slot main pool.
8. **Per-channel weighting**: equal-weight v1; `trade_eligible` boolean for runtime curation.
9. **Co-occurrence boost**: surface in alert text only ("also mentioned by @other_channel 4h ago"); no special behavior. v2 adds louder/larger trades.
10. **Rate limit**: none v1; dedup + slot quota + small sizing bound the blast radius.

## Prior-art references

- **LonamiWebs/Telethon** (MIT, archived Feb 2026, moved to Codeberg): the MTProto library we use directly. Listener pattern, session management, catchup via `iter_messages(min_id=...)`.
- **paulpierre/informer** (MIT, 1.6k ★, last commit Oct 2025): production reference for multi-channel monitoring + FloodWait handling at the 500-channel scale; we don't import, we read for patterns.
- **DarkWebInformer/telegram-scraper** (MIT, 311 ★): SQLite per-channel `last_message_id` resume pattern; we adopt this directly via `tg_social_channels.last_seen_msg_id`.
- **Gregsayshi/Telegram_Streamer** (MIT): cashtag/hashtag tally + highest-count attribution rule; we adopt this for multi-token messages.
- **Pyrogram (discontinued)** and **Hydrogram** (GPL, pre-1.0): considered and rejected as fallbacks.
