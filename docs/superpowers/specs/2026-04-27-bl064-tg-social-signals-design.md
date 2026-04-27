# BL-064 — TG Social Signals (paper-trade + alert from curated TG channels)

**Status:** Design v2 (post 5-reviewer pass — applies all SHOWSTOPPERS + IMPORTANT + NITS)
**Date:** 2026-04-27
**Author:** Claude (with srilu)

## Revision history

- v1 — initial design after brainstorm. Module layout, A+B lifecycle, $300 sizing, separate slot quota, no mcap floor, Telethon user-session.
- **v2 (this) — applies 5-reviewer feedback in full.** 7 SHOWSTOPPERS fixed (TradingEngine seam, watermark transactionality, safety fail-closed, SQLite tz handling, FloodWait operator-page, alert trust-laundering UX, session-file secret hardening). 17 IMPORTANT items folded in (DLQ, channel-silence heartbeat, resolution state machine, per-message-exposure dedup, separate watermarks table, single CLI, etc.). All 16 NITS rolled into spec.

## Problem statement

Curated Telegram channels run by crypto influencers regularly post early-stage token calls. Motivating example: `@gem_detecter` posted `$RIV` (CA `2bpT3ksMdwdZ6DuHyq3FDUr7HDwvZ5DRZoT1fUPALJaH`) on 2026-04-06 at $3M mcap; the token sat at $60M mcap by this design (~20×). gecko-alpha currently has no input from these sources.

CoinGecko-derived signals (`first_signal`, `gainers_early`, `trending_catch`) reliably catch tokens AFTER they start moving on CG markets. The hypothesis is that watching 3-4 specific high-signal accounts captures lead time as actionable alerts and (when admission gates pass) paper trades.

X (Twitter) was considered and dropped: API costs $200+/month; many curators cross-post to TG; TG has a free real-time MTProto API.

## Goals

- **Primary:** capture token mentions from a curator-defined channel set with <30s post→alert latency.
- **Secondary:** open paper trades with `signal_type='tg_social'` when a CA is parseable and admission passes, so per-source hit rates are measurable in `combo_performance`.
- **Operational:** zero ongoing cost; one-time auth setup ~5 min; channels mutable at runtime.

## Non-goals

- X (Twitter) ingestion. Dropped per cost.
- Manual forward-to-bot mode. Auto-read user-session was chosen.
- Live (real-money) trade dispatch. `tg_social` is paper-only.
- ML message-of-interest classifier. v2 candidate.
- Co-occurrence boost (same token mentioned by 2+ channels in window → louder/larger trade). v2.
- Per-channel weighting beyond the boolean `trade_eligible`. v1 is otherwise equal-weight.
- TG group chats / replies. Scope is broadcast-style channels only.

## Approach

Telethon-based MTProto user-session listener subscribes to N channels (DB-driven). Each new message is parsed for cashtags, contract addresses, and DEX/explorer URLs. Resolved tokens are enriched, gated through TG-specific checks in `dispatcher.py`, then dispatched to the existing `TradingEngine.open_trade` (NOT `paper.execute_buy` directly — engine ownership preserved per architecture review). Alerts always fire with explicit two-tier provenance UX. Restart catchup uses `iter_messages(min_id=last_seen_msg_id)`. Telethon is the only viable MTProto library in 2026 (Pyrogram and Hydrogram both rejected — see §Prior art).

## Architecture

```
scout/social/telegram/
  __init__.py
  client.py          # Telethon TelegramClient wrapper, session load,
                     #   get_me() validity check at listener startup AND bootstrap
  cli.py             # subcommands: bootstrap, add, remove, set-trade, sync-channels
  listener.py        # async task: catchup → live event handler → FloodWait wrap.
                     # Exports `handle_new_message(event, deps)` as a free function
                     # for testability (no TelegramClient mock needed).
  parser.py          # pure: text → ParsedMessage{cashtags, contracts, urls}
  resolver.py        # pure-async: CA → CG/DexScreener; ticker → CG search.
                     # Also performs enrichment (mcap/price/vol/age) and
                     # safety check via scout.safety. Returns ResolutionResult
                     # with state ∈ {RESOLVED, UNRESOLVED_TRANSIENT, UNRESOLVED_TERMINAL}
                     # and safety_check_completed: bool (fail-closed discriminator).
  dispatcher.py      # TG-only gates ONLY (dedup-by-open, CA-required,
                     # channel.trade_eligible). Delegates trade open to
                     # `trading_engine.open_trade(signal_type='tg_social', ...)`
                     # to avoid duplicating BL-062 stacking gate / peak-fade /
                     # mcap cap / safety / junk filter that already live in engine.
  alerter.py         # Two-tier alert template (see §Alert UX).
  models.py          # Pydantic: ParsedMessage, ResolvedToken, ResolutionResult,
                     # AdmissionDecision.
```

Module count: **8 files** (down from v1's 9 — `enricher.py` collapsed into `resolver.py`; two CLIs merged into `cli.py`).

**Listener loop integration:** launched in `scout/main.py` alongside other long-running tasks (perp watcher, CryptoPanic poller, narrative agent). The listener task receives `trading_engine: TradingEngine` and `db: Database` as constructor args (DI seam — same pattern as `narrative_agent_loop`).

## Data flow

```
[Telegram MTProto user-session]
  │ NewMessage event from one of N watched channels
  ▼
parser.parse_message(text) → ParsedMessage{cashtags, contracts, urls}
  │
  ▼ ── BEGIN single aiosqlite transaction ──
  │
  │   1. INSERT into tg_social_messages (UNIQUE(channel_handle, msg_id))
  │   2. UPDATE tg_social_watermarks SET last_seen_msg_id=msg_id
  │
  │ ── COMMIT ──
  │
  │ Watermark advances HERE — before resolver/enricher/alert/trade.
  │ Crash after this point is safe: UNIQUE constraint makes replay
  │ idempotent and the watermark won't regress.
  │
  ▼ ParsedMessage in memory
  │
  ▼ if no cashtags AND no contracts → log no_signal, done
  │
resolver.resolve_and_enrich():
  │   For each contract first, then cashtag-only fallback:
  │     • CA: CG by-contract → DexScreener fallback
  │     • Ticker: CG search → top-3 candidates by mcap
  │   Multi-token tally rule: highest count wins; ties → all candidates listed
  │   Returns: ResolutionResult per token with state ∈
  │     RESOLVED                — fully enriched, safety pass, ready for gates
  │     UNRESOLVED_TRANSIENT    — CG/DexScreener miss; retry once after
  │                               TG_SOCIAL_RESOLUTION_RETRY_DELAY_SEC (60s)
  │     UNRESOLVED_TERMINAL     — no resolution after retry; alert-only
  │   safety_check_completed: bool
  │     False  — GoPlus 5xx/timeout; FAIL CLOSED — gate 3 rejects
  │     True   — GoPlus returned a verdict (pass or fail used in gate 3)
  │
  ▼
dispatcher TG-only gates (fail-closed, in order):
  1. dedup-by-OPEN-exposure: any tg_social_signals row whose linked
     paper_trades.status = 'open' for this token? → no trade. (Replaces
     v1's 24h-per-token rule; lets re-emphasis posts after first trade
     closes fire a new trade.)
  2. CA-resolved? (ticker-only → alert-only forever, never trade)
  3. channel.trade_eligible == 1?
  4. ResolutionResult.safety_check_completed AND scout.safety.is_safe verdict?
       (Explicit AND on completed=True closes the BL-063 fail-open)
  │
  │ If all TG-only gates pass:
  ▼
trading_engine.open_trade(
    signal_type='tg_social',
    signal_combo='tg_social',
    amount_usd=PAPER_TG_SOCIAL_TRADE_AMOUNT_USD,
    tg_social_quota_check=True,    # engine consults TG_SOCIAL_MAX_OPEN_TRADES
)
  │ Engine handles: junk filter, mcap upper cap, BL-062 stacking gate
  │   (skipped for tg_social — single-source signal_type), per-token cooldown,
  │   slot quota check (tg_social uses its own quota counter), and the actual
  │   `paper.execute_buy` call. Returns paper_trade_id or None (if engine
  │   gate rejected).
  ▼
ALWAYS alert via alerter.py with two-tier provenance template (see §Alert UX)
  │
  ▼
INSERT tg_social_signals (message_pk, token_id, ..., paper_trade_id, alert_sent_at)
```

**Catchup on restart:** before live handler attaches, for each row in `tg_social_channels` (where `removed_at IS NULL`), call `iter_messages(channel_handle, min_id=last_seen_msg_id, limit=TG_SOCIAL_CATCHUP_LIMIT)`. Per Telethon, `min_id` is exclusive (returns id > min_id). Replay through the same pipeline (transaction-safe via UNIQUE). If `iter_messages` returns exactly `TG_SOCIAL_CATCHUP_LIMIT` messages → emit `tg_social_catchup_truncated` Telegram alert + log so the operator knows messages may have been missed.

**Resolution retry:** if state = `UNRESOLVED_TRANSIENT` on first pass, schedule a follow-up resolver call after `TG_SOCIAL_RESOLUTION_RETRY_DELAY_SEC` (60s default). Brand-new tokens often need a minute for CG indexing to catch up. Second miss → `UNRESOLVED_TERMINAL`, alert-only.

**Channel-silence heartbeat:** a periodic check (every `TG_SOCIAL_CHANNEL_SILENCE_CHECK_INTERVAL_SEC`, default 1h) compares `tg_social_health.last_message_at` per channel against `now() - TG_SOCIAL_CHANNEL_SILENCE_ALERT_HOURS` (default 72h). One alert per silent channel until activity resumes — catches the "curator stopped posting AND we didn't get the kicked/banned event" failure.

## Schema (migration in `scout/db.py`)

```sql
-- 1. Channel registry (descriptive, mutable at runtime)
CREATE TABLE tg_social_channels (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  channel_handle  TEXT NOT NULL UNIQUE,
  display_name    TEXT NOT NULL,
  trade_eligible  INTEGER NOT NULL DEFAULT 1,
  added_at        TEXT NOT NULL,
  removed_at      TEXT
);

-- 2. Watermarks (separate from channels — processing state, not metadata)
CREATE TABLE tg_social_watermarks (
  channel_handle    TEXT PRIMARY KEY,
  last_seen_msg_id  INTEGER NOT NULL DEFAULT 0,
  updated_at        TEXT NOT NULL,
  FOREIGN KEY (channel_handle) REFERENCES tg_social_channels(channel_handle)
);

-- 3. Raw message log
CREATE TABLE tg_social_messages (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  channel_handle  TEXT NOT NULL,
  msg_id          INTEGER NOT NULL,
  posted_at       TEXT NOT NULL,
  sender          TEXT,
  text            TEXT,
  cashtags        TEXT,                            -- JSON
  contracts       TEXT,                            -- JSON
  urls            TEXT,
  parsed_at       TEXT NOT NULL,
  UNIQUE(channel_handle, msg_id)
);

-- 4. Resolved + dispatched signals
CREATE TABLE tg_social_signals (
  id                     INTEGER PRIMARY KEY AUTOINCREMENT,
  message_pk             INTEGER NOT NULL,
  token_id               TEXT NOT NULL,
  symbol                 TEXT NOT NULL,
  contract_address       TEXT,
  chain                  TEXT,
  mcap_at_sighting       REAL,
  resolution_state       TEXT NOT NULL,            -- RESOLVED, UNRESOLVED_TRANSIENT, UNRESOLVED_TERMINAL
  source_channel_handle  TEXT NOT NULL,
  alert_sent_at          TEXT,
  paper_trade_id         INTEGER,                  -- nullable if alert-only
  created_at             TEXT NOT NULL,
  FOREIGN KEY (message_pk) REFERENCES tg_social_messages(id)
);

-- 5. Listener health (FloodWait state, last activity per channel)
CREATE TABLE tg_social_health (
  component        TEXT PRIMARY KEY,                -- e.g., 'listener', 'channel:@gem_detecter'
  listener_state   TEXT NOT NULL,                   -- 'running', 'disabled_floodwait', 'auth_lost'
  last_message_at  TEXT,                            -- per-channel last received message
  updated_at       TEXT NOT NULL,
  detail           TEXT                             -- error string when disabled
);

-- 6. Dead-letter queue for failed message processing
CREATE TABLE tg_social_dlq (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  channel_handle  TEXT NOT NULL,
  msg_id          INTEGER NOT NULL,
  raw_text        TEXT,
  error_class     TEXT NOT NULL,
  error_text      TEXT NOT NULL,
  failed_at       TEXT NOT NULL,
  retried_at      TEXT
);
```

**Indexes (all in migration step, NOT `_create_tables` — BL-060 lesson):**

```sql
CREATE INDEX idx_tg_social_signals_token_created
  ON tg_social_signals(token_id, created_at);
CREATE INDEX idx_tg_social_signals_channel_created
  ON tg_social_signals(source_channel_handle, created_at);
CREATE INDEX idx_tg_social_signals_paper_trade_id
  ON tg_social_signals(paper_trade_id) WHERE paper_trade_id IS NOT NULL;
CREATE INDEX idx_tg_social_messages_channel_msgid
  ON tg_social_messages(channel_handle, msg_id);
CREATE INDEX idx_tg_social_dlq_failed_at
  ON tg_social_dlq(failed_at);
```

A `paper_migrations` row `bl064_tg_social` is inserted at migration time. **Migration post-assertion** (before commit, per BL-063 pattern at `scout/db.py:961-977`): assert all of `bl061_ladder`, `bl062_peak_fade`, `bl063_moonshot`, `bl064_tg_social` exist; raise + ROLLBACK if missing.

A/B comparison queries scope `opened_at >= cutover_ts` per BL-060 mid-flight migration lesson. Datetime comparisons use `datetime(opened_at) >= datetime(cutover_ts)` per `scout/trading/engine.py:182` (closes the 33cd47 SQLite tz footgun).

**`live_trades` FK contract (BL-055):** `live_trades.paper_trade_id REFERENCES paper_trades(id) ON DELETE RESTRICT`. `tg_social` paper trades inherit the existing append-only contract — **no DELETE path, no special handling**, even if a `tg_social_signals` row is later soft-removed.

## Configuration (`scout/config.py`)

```python
TG_SOCIAL_ENABLED: bool = False
TG_SOCIAL_API_ID: int = 0                                # my.telegram.org
TG_SOCIAL_API_HASH: SecretStr | None = None              # my.telegram.org
TG_SOCIAL_PHONE_NUMBER: str = ""                         # bootstrap only
TG_SOCIAL_SESSION_PATH: Path = Path("./tg_social.session")
TG_SOCIAL_CHANNELS_FILE: Path = Path("./channels.yml")   # version-controlled source-of-truth
TG_SOCIAL_MAX_OPEN_TRADES: int = 5                       # separate quota
PAPER_TG_SOCIAL_TRADE_AMOUNT_USD: float = 300.0
TG_SOCIAL_CATCHUP_LIMIT: int = 200                       # max msgs/channel on restart catchup
TG_SOCIAL_FLOOD_WAIT_MAX_SEC: int = 600                  # circuit-break cap (raised from v1's 300; first-join can hit 6h on private channels — operator-pages either way)
TG_SOCIAL_CHANNEL_RELOAD_INTERVAL_SEC: int = 300         # reload tg_social_channels for adds/removes
TG_SOCIAL_RESOLUTION_RETRY_DELAY_SEC: int = 60           # transient-resolve retry
TG_SOCIAL_CHANNEL_SILENCE_ALERT_HOURS: int = 72          # alert if zero msgs in this window
TG_SOCIAL_CHANNEL_SILENCE_CHECK_INTERVAL_SEC: int = 3600 # how often to evaluate silence
```

**Cross-field validator** (`@model_validator(mode='after')`, value-only — NO filesystem checks per conventions review #3): if `TG_SOCIAL_ENABLED=True` then `TG_SOCIAL_API_ID > 0` AND `TG_SOCIAL_API_HASH is not None`. The session-file existence check moves to `client.py` listener-startup with an actionable error message.

**Per-field validators**: `TG_SOCIAL_MAX_OPEN_TRADES >= 1`, `PAPER_TG_SOCIAL_TRADE_AMOUNT_USD > 0`, `TG_SOCIAL_FLOOD_WAIT_MAX_SEC > 0`, `TG_SOCIAL_CATCHUP_LIMIT >= 0`, `TG_SOCIAL_CHANNEL_RELOAD_INTERVAL_SEC >= 60`, `TG_SOCIAL_RESOLUTION_RETRY_DELAY_SEC >= 0`, `TG_SOCIAL_CHANNEL_SILENCE_ALERT_HOURS >= 1`.

**`.env.example` additions**: all 13 `TG_SOCIAL_*` keys with sensible commented-out defaults and links to my.telegram.org for API_ID/HASH acquisition. Documented as a checklist item.

**`channels.yml` source of truth** (version-controlled at repo root, gitignored if it contains real handles, otherwise checked-in template):

```yaml
# channels.yml — source of truth for TG_SOCIAL channels
# On startup, listener reconciles this with the tg_social_channels table.
# Adds/removes here propagate to DB. DB-side trade_eligible toggles persist.
channels:
  - handle: "@gem_detecter"
    display_name: "Gem Detector"
    trade_eligible: true
  - handle: "@example_alpha"
    display_name: "Example Alpha"
    trade_eligible: false   # alert-only
```

This solves the "disk-full event wipes channel definitions" pre-mortem case: even after a SQLite restore from old backup, `channels.yml` re-sync recovers the channel set.

## Authentication and secrets handling

**One-time bootstrap CLI** (`python -m scout.social.telegram.cli bootstrap`):

1. Reads `TG_SOCIAL_API_ID` + `TG_SOCIAL_API_HASH` from `.env`.
2. Prompts for phone (or reads `TG_SOCIAL_PHONE_NUMBER`).
3. Telegram sends auth code → user enters.
4. If 2FA enabled → password prompt.
5. Writes `TG_SOCIAL_SESSION_PATH` with **filesystem mode 0600** (`os.chmod` immediately after write).
6. Calls `client.get_me()` → prints `username` + `id` for confirmation.
7. **Idempotent**: if a valid session already exists at the path AND `get_me()` succeeds → skip phone prompt, print "session valid" + user info, exit.
8. Exits.

**Production secret hardening** (per devil's-advocate SHOWSTOPPER #2):

- Session file mode `0600`, owned by the gecko-pipeline user.
- Backup-exclusion: `/root/gecko-alpha/*.session` added to operator's tarball exclude glob in the runbook AND a comment in `.gitignore` documenting WHY (whoever has this file IS the user).
- Encryption-at-rest recommendation in the runbook (LUKS / disk-level encryption); enforcement is operator-side, not code-side.
- Periodic session-validity heartbeat: listener calls `get_me()` at startup AND every `TG_SOCIAL_CHANNEL_SILENCE_CHECK_INTERVAL_SEC`. On `AuthKeyError` / `SessionPasswordNeededError` → `tg_social_health.listener_state='auth_lost'` + Telegram alert with the bootstrap command + listener stops cleanly.

**Operator setup checklist** (also in runbook):

1. Visit https://my.telegram.org → API Development tools → create app → record `api_id` + `api_hash`.
2. Add to `.env`: `TG_SOCIAL_API_ID=...`, `TG_SOCIAL_API_HASH=...`.
3. `python -m scout.social.telegram.cli bootstrap` (one-time interactive).
4. Confirm session file mode is `600`: `stat -c '%a' tg_social.session`.
5. Edit `channels.yml` to list your watched channels.
6. `python -m scout.social.telegram.cli sync-channels` to push to DB.
7. Set `TG_SOCIAL_ENABLED=True` in `.env`.
8. Restart `gecko-pipeline`.

**`CLAUDE.md` "What NOT To Do" addition** (per conventions review #21): one-line entry — *"Do not auto-bump Telethon in dependabot PRs — the upstream is archived (Feb 2026); manual review required for every version change."*

## Alert UX (two-tier — closes trust-laundering risk)

The most important UX change v2 brings: **`tg_social` alerts must visually telegraph "curator-sourced, unverified by us"** so the operator doesn't manually mirror live based on the alert alone (devil's advocate SHOWSTOPPER #3).

**`tg_social` alert template:**

```
⚠️ [CURATOR SIGNAL — VERIFY BEFORE MANUAL ACTION] ⚠️
@gem_detecter posted $RIV
Resolved: RIV (solana, CA: 2bpT3ks...LJaH)
Mcap: $3.2M | 24h: +47% | Vol: $890K
Safety: ✅ verified (GoPlus)
[ TRADE_DISPATCHED paper id=1387 amount=$300 ]
🔗 https://t.me/gem_detecter/12345
─────
This is a single-curator signal, NOT a multi-source pipeline confirmation.
Independent verification required before any live action.
```

**For pipeline-sourced alerts** (existing): keep current format unchanged — they're already trusted as multi-source.

**`signal_type='tg_social'` in `combo_performance` rollup**: tagged separately so its win-rate can't pollute `first_signal` / `gainers_early` cohort statistics.

## Error handling

| Layer | Failure | Action |
|---|---|---|
| Auth (startup) | missing creds, invalid `.env` | Fail fast at startup. Error message includes `python -m scout.social.telegram.cli bootstrap`. Raise `TgSocialAuthError(channel=None, reason='missing_creds')`. |
| Auth (mid-flight) | `AuthKeyError` / `SessionPasswordNeededError` from Telethon (e.g., user logged out from another device) | Listener stops cleanly. `tg_social_health.listener_state = 'auth_lost'`. Telegram alert with bootstrap command. |
| Session validity (startup) | `client.get_me()` raises | Same as above — fail with bootstrap instruction. |
| MTProto | disconnect | Telethon auto-reconnects with backoff. Log each event. After 5 consecutive disconnects → emit heartbeat alert. |
| FloodWait | rate limited | Catch `FloodWaitError`. Sleep `min(.seconds + 1, TG_SOCIAL_FLOOD_WAIT_MAX_SEC)`. If cap exceeded → `tg_social_health.listener_state='disabled_floodwait'` + Telegram alert. Dashboard surfaces a banner consuming `tg_social_health` (operator-page guarantee). |
| Catchup | `iter_messages` returns exactly `TG_SOCIAL_CATCHUP_LIMIT` | Emit `tg_social_catchup_truncated` Telegram alert (one per channel) + log warning so operator knows N messages may have been missed. |
| Channel access | not-found / kicked / banned | Catch specific Telethon error class. Mark `tg_social_channels.removed_at = now()`. Telegram alert ONCE. Continue serving other channels. |
| Channel-silence | zero messages from channel in `TG_SOCIAL_CHANNEL_SILENCE_ALERT_HOURS` | Periodic check (every `_CHECK_INTERVAL_SEC`). One alert per silent channel until activity resumes. |
| Resolution | CA/ticker miss on first pass | `state=UNRESOLVED_TRANSIENT`. Schedule retry after `TG_SOCIAL_RESOLUTION_RETRY_DELAY_SEC`. |
| Resolution | CA/ticker miss after retry | `state=UNRESOLVED_TERMINAL`. Alert-only with `[unresolved]` badge. **No trade.** Persisted to `tg_social_signals` for analytics. |
| Safety | `is_safe()` raises (5xx / timeout) | `safety_check_completed=False`. Gate 4 fail-CLOSED — **never admit**. Log + alert with `[safety_unknown]` badge. (Closes BL-063 fail-open inheritance.) |
| Safety | `is_safe()` returns False | Gate 4 reject. Alert with `[safety_failed]` badge. No trade. |
| Junk filter / mcap cap (engine-side) | reject | `TradingEngine.open_trade` returns None with reason. Dispatcher logs `tg_social_admission_blocked` with `gate=...` field; alert STILL goes with badge "alert-only: blocked by gate". |
| Slot quota | engine sees `tg_social` open count >= TG_SOCIAL_MAX_OPEN_TRADES | Engine returns None with reason='quota'. Same alert-only handling. Quota check serialized in single transaction (no race). |
| DB write | error during message persist or signal insert | Append row to `tg_social_dlq` with error_class + error_text + raw_text. Listener continues with next message. Operator manually triages DLQ via `cli replay-dlq` subcommand. |

**Domain exceptions** in `scout/exceptions.py`:

```python
class TgSocialAuthError(ScoutError):
    def __init__(self, channel: str | None, reason: str) -> None:
        self.channel = channel
        self.reason = reason
        super().__init__(
            f"[tg_social_auth] channel={channel or '(none)'} reason={reason}"
        )

class TgSocialResolutionError(ScoutError):
    def __init__(self, identifier: str, source: str) -> None:
        self.identifier = identifier
        self.source = source
        super().__init__(
            f"[tg_social_resolution] could not resolve '{identifier}' from {source}"
        )
```

## Logging schema

Every log event is structured (snake_case past-tense events, fields not embedded in name — matches `ladder_leg_fired`, `moonshot_armed` precedent).

| Event name | Required fields |
|---|---|
| `tg_social_message_received` | `channel_handle`, `msg_id`, `sender`, `text_len` |
| `tg_social_message_persisted` | `channel_handle`, `msg_id`, `cashtag_count`, `contract_count` |
| `tg_social_resolution_succeeded` | `channel_handle`, `msg_id`, `token_id`, `symbol`, `chain`, `mcap` |
| `tg_social_resolution_retry_scheduled` | `channel_handle`, `msg_id`, `identifier`, `delay_sec` |
| `tg_social_resolution_failed` | `channel_handle`, `msg_id`, `identifier`, `final` (bool) |
| `tg_social_admission_blocked` | `trade_id_attempt` (None), `gate_name`, `reason`, `token_id` |
| `tg_social_trade_dispatched` | `paper_trade_id`, `token_id`, `symbol`, `amount_usd`, `channel_handle` |
| `tg_social_alert_sent` | `signal_id`, `template`, `provenance` ('curator'/'pipeline'), `paper_trade_id` |
| `tg_social_floodwait_circuit_break` | `seconds_requested`, `cap` |
| `tg_social_channel_silenced` | `channel_handle`, `last_message_at`, `silence_hours` |
| `tg_social_catchup_truncated` | `channel_handle`, `limit`, `last_seen_msg_id` |
| `tg_social_auth_lost` | `error_class`, `bootstrap_command` |
| `tg_social_dlq_appended` | `channel_handle`, `msg_id`, `error_class` |

## Testing strategy

**Unit (pure):**

- `tests/test_tg_social_parser.py` — cashtag regex, EVM 0x, Solana base58, URL extraction (dexscreener/birdeye/photon-sol), multi-token tally rule, edge cases (CA inside URL, multi-line, emoji-only, top-3 ambiguous handling)
- `tests/test_tg_social_resolver.py` — CA→CG hit, CA→DexScreener fallback, ticker→top-3 candidates, resolution state machine (RESOLVED, UNRESOLVED_TRANSIENT with mocked retry, UNRESOLVED_TERMINAL after retry exhausted), `safety_check_completed` flag fail-closed
- `tests/test_tg_social_dispatcher.py` — TG-only gates: dedup-by-OPEN-exposure (boundary at trade close), CA-required, channel.trade_eligible toggle, safety-fail-closed; assert TradingEngine.open_trade is called with correct args; assert engine-side rejections (quota, mcap, junk) are logged but alert still fires
- `tests/test_tg_social_alerter.py` — two-tier alert template with provenance badge, [unresolved], [safety_unknown], [safety_failed], [blocked_by_gate]
- `tests/test_tg_social_dedup_open_exposure.py` — explicit boundary tests: trade open → second post no trade; trade closed → second post fires new trade
- `tests/test_tg_social_ts_isoformat.py` — 33cd47 lesson: insert with `+00:00` suffix, query at 23h59m and 24h01m, assert correct boundary handling

**Integration:**

- `tests/test_tg_social_listener.py` — call `handle_new_message(event, deps)` as a free function (per architecture review — no TelegramClient mock, just SimpleNamespace event); end-to-end through pipeline → DB rows + alert + (when applicable) TradingEngine.open_trade call
- `tests/test_tg_social_catchup.py` — set `last_seen_msg_id`, mock `iter_messages(min_id=...)` returning an async iterator (real `__aiter__` generator, not naive AsyncMock), assert idempotency on UNIQUE collision
- `tests/test_tg_social_catchup_truncated.py` — limit returned messages to exactly `TG_SOCIAL_CATCHUP_LIMIT`, assert truncation alert fires
- `tests/test_tg_social_watermark_transactionality.py` — simulate crash between `tg_social_messages.INSERT` and `tg_social_signals.INSERT`; assert watermark ≤ persisted message_id; on restart, replay from watermark recovers cleanly
- `tests/test_tg_social_quota_race.py` — `asyncio.gather` two dispatcher calls when `tg_social` open count = max−1; assert exactly one dispatches
- `tests/test_tg_social_health_floodwait.py` — raise `FloodWaitError(seconds=cap+1)`; assert `tg_social_health.listener_state='disabled_floodwait'` + Telegram alert sent
- `tests/test_tg_social_channel_silence.py` — set `last_message_at` to >72h ago; run silence-check; assert one alert; run again immediately; assert NO duplicate alert
- `tests/test_tg_social_dlq.py` — force a DB write error mid-pipeline; assert `tg_social_dlq` row appended; assert listener continues with next message

**Migration:**

- `tests/test_db_migration_bl064.py` — six tables added, all indexes (including `paper_trade_id` partial index), `paper_migrations` row inserted; **post-assertion** asserts all four cutover rows exist; idempotent re-run

**Combo/regression gates:**

- `tests/test_trading_combo_refresh.py` — parametrized `test_refresh_counts_<status>_in_rollup_for_tg_social_signal_type` over the full `CLOSED_COUNTABLE_STATUSES` tuple; assert `tg_social` combo_key materializes a row and counts every closed status
- Existing `paper_trade` tests pass unchanged (regression gate). New `signal_type='tg_social'` adds a new combo_key; existing combos untouched.
- `tests/test_combo_performance_excludes_pre_cutover_tg_social_rows` — BL-060 lesson: insert a `tg_social` paper trade with `opened_at < cutover_ts`, run rollup, assert excluded.

**Settings:**

- `tests/test_config.py::test_tg_social_validator_enabled_without_creds_raises_with_bootstrap_command_in_message` — assert error string contains literal `python -m scout.social.telegram.cli bootstrap`
- `tests/test_config.py::test_tg_social_validator_pure_value_only` — pass `TG_SOCIAL_ENABLED=True` + valid creds; assert no FileNotFoundError raised even when session path doesn't exist (filesystem check moved out of validator)

**Bootstrap:**

- `tests/test_tg_social_bootstrap_idempotent.py` — pre-existing valid session → skip phone prompt; print "session valid"; exit 0

**Mocks:**

- Telethon `TelegramClient` — extracted `handle_new_message(event, deps)` is a free function so tests build `event = SimpleNamespace(message=..., chat=..., sender=...)` without touching the client
- `iter_messages` — return MagicMock with `__aiter__` returning a real async generator
- `aioresponses` for CG/DexScreener
- `tmp_path` for aiosqlite

## Operations

**Adding a channel** (preferred — channels.yml as source-of-truth):

```bash
# Edit channels.yml, commit, then sync to DB
python -m scout.social.telegram.cli sync-channels
```

**One-off interactive add** (mostly for testing):

```bash
python -m scout.social.telegram.cli add @gem_detecter "Gem Detector"
python -m scout.social.telegram.cli add --no-trade @noisy_channel "Noisy"
```

**Removing**:

```bash
python -m scout.social.telegram.cli remove @noisy_channel
```

**Toggling trade-eligibility (alerts continue)**:

```bash
python -m scout.social.telegram.cli set-trade @noisy_channel false
```

**Replaying DLQ items**:

```bash
python -m scout.social.telegram.cli replay-dlq            # all
python -m scout.social.telegram.cli replay-dlq --id 42    # one
```

The listener detects DB-side channel changes within `TG_SOCIAL_CHANNEL_RELOAD_INTERVAL_SEC` (default 5 min) without service restart.

## Rollout

1. Merge with `TG_SOCIAL_ENABLED=False`. Schema migrated; `bl064_tg_social` cutover stamped. Zero side-effect — listener task does not start.
2. Run bootstrap on prod: `python -m scout.social.telegram.cli bootstrap`. Confirm session at mode `0600`. Verify `get_me()` output.
3. Edit `channels.yml`, add 1 channel, `cli sync-channels`.
4. Set `TG_SOCIAL_ENABLED=True` in prod `.env`. Restart `gecko-pipeline`.
5. Monitor for 24h: `tg_social_messages` row count + `tg_social_signals` count + first `paper_trade_id` assigned. Verify alerts arrive with the two-tier provenance template. Verify `tg_social_health` row present with `listener_state='running'`.
6. Add remaining 2-3 channels.
7. Soak 14 days. Watch `combo_performance` row for `tg_social` combo_key.
8. **Decision gate** (per audit-before-architectural-reasoning lesson): query VPS DB+logs directly to verify the rollup; do NOT infer from rules alone. If `tg_social` 14d combo win-rate ≥ 40% AND avg pnl_pct > 0 → keep on. Else flip flag-off and re-evaluate per channel via per-channel `combo_performance` filter.

## Risks + mitigations

| Risk | Mitigation |
|---|---|
| Telethon archived (Feb 2026) bit-rots after MTProto schema bump | Pin to last v1 stable. CLAUDE.md note prevents auto-bump. Disconnect-heartbeat (5 consecutive) catches dropped events. Fallback path: fork Telethon at pinned version OR migrate to `mtproto-core`. |
| Session file leaked via cloud backup (devil's advocate) | mode 0600 + backup-exclusion glob in runbook + `.gitignore` rationale comment + encryption-at-rest recommendation |
| Curator gets compromised, posts malicious CA | Bounded loss = $300 (paper). Two-tier alert template prevents user-side trust-laundering: `[CURATOR SIGNAL — VERIFY BEFORE MANUAL ACTION]` is in-your-face. |
| Curator economics never materialize (devil's advocate SHOWSTOPPER #1) | Per-channel `trade_eligible` toggle for surgical dial-back. Decision gate at 14d. Separate combo_performance namespace prevents cross-pollution into `first_signal` / `gainers_early` stats. Flag-off is a single env change + restart. |
| Brand-new gems repeatedly hit "unresolved" | Resolution state machine: TRANSIENT retries after 60s; only TERMINAL flags as permanent unresolved. Alerts include the retry status so users aren't trained to ignore the [unresolved] badge. |
| 24h-per-token dedup eats re-emphasis posts | **Fixed in v2: dedup unit is per-OPEN-exposure**, not 24h-per-token. After first `tg_social` paper trade closes, a new post fires a new trade. |
| Sub-200K mcap rugs slip past GoPlus (devil's advocate) | Fail-CLOSED safety check (gate 4) + bounded loss $300/trade. NOT addressed by re-introducing a mcap floor — that defeats the curator-early-call use case. Operator can flag a noisy channel `trade_eligible=False` reactively. |
| FloodWait silently disrupts pipeline | `tg_social_health` table + dashboard banner + Telegram alert on `disabled_floodwait` ensure operator visibility. Cap raised to 600s (first-join private channels can hit higher; circuit-break is the right behavior). |
| Mid-flight migration breaks A/B integrity | `paper_migrations.bl064_tg_social` cutover row + queries scope `opened_at >= cutover_ts` (BL-060 lesson) + post-assertion check |
| Cross-chain ticker ambiguity (multiple `$WIF`) | **Fixed in v2: top-3 by mcap surfaced in alert; auto-trade ONLY when CA is in the message.** No magic 2× threshold. |
| `last_seen_msg_id` watermark race | **Fixed in v2: watermark advances in same `aiosqlite` transaction as message persist**, before resolver/alert. UNIQUE constraint plus transactional advancement makes catchup idempotent under any crash point. |
| DB write fails mid-pipeline | DLQ table + `cli replay-dlq` subcommand. Listener continues with next message. |
| SQLite tz datetime footgun on dedup | Explicit `datetime(col) >= datetime('now', '-N hours')` pattern; boundary unit test. |
| Channel goes silent without explicit error | `TG_SOCIAL_CHANNEL_SILENCE_ALERT_HOURS` heartbeat catches stalled subscriptions / curator account disabled |
| `channels.yml` source-of-truth diverges from DB | `cli sync-channels` is the canonical reconciliation; runbook documents that DB-side `trade_eligible` toggles persist across syncs (yaml only specifies adds/removes/initial trade_eligible) |

## Resolved review feedback

This v2 spec applies all findings from the 5-reviewer pass on v1:

**SHOWSTOPPERS (7/7 applied):**
- TradingEngine seam (architecture #1) — dispatcher delegates to `TradingEngine.open_trade`, not `paper.execute_buy`
- Watermark transactionality (silent-failure HIGH#2) — same-transaction advance with message persist
- Safety fail-closed (silent-failure HIGH#1, BL-063 lesson) — gate 4 requires `safety_check_completed=True`
- SQLite tz datetime (silent-failure HIGH#3, 33cd47 lesson) — explicit `datetime()` pattern + boundary tests
- FloodWait operator-page (silent-failure HIGH#4) — `tg_social_health` table + dashboard banner
- Trust-laundering UX (devil's advocate SHOWSTOPPER #3) — two-tier alert template
- Session-file as production secret (devil's advocate SHOWSTOPPER #2) — 0600 + backup-exclusion + encryption-at-rest recommendation

**IMPORTANT (17/17 applied):**
- TradingEngine duplication eliminated (architecture #2)
- 9→8 file split (architecture SMELL #1, enricher merged into resolver)
- `tg_social_watermarks` separate table (architecture SMELL #2)
- Single `cli.py` with subcommands (architecture SMELL #3 + conventions #11)
- Index on `tg_social_signals.paper_trade_id` (architecture #6)
- Migration post-assertion (conventions #1, BL-063 pattern)
- Validator value-only (conventions #3) — filesystem check at listener startup
- Resolution state machine RESOLVED/TRANSIENT/TERMINAL (silent-failure MEDIUM#1)
- Channel-silence heartbeat (silent-failure MEDIUM#2)
- Top-3 cashtag candidates replaces 2× cliff (silent-failure MEDIUM#3)
- `tg_social_dlq` table (silent-failure MEDIUM#4)
- Per-OPEN-exposure dedup replaces per-token-24h (devil's advocate IMPORTANT #6)
- `channels.yml` as version-controlled backup (devil's advocate IMPORTANT #7)
- Telethon CLAUDE.md "What NOT To Do" note (devil's advocate IMPORTANT #5)
- Parametrized CLOSED_COUNTABLE_STATUSES regression test (test#3)
- Slot quota race serialization (test#4)
- Catchup truncation operator-alert (silent-failure LOW#3 escalated)

**NITS (16/16 applied):**
- `handle_new_message` extracted as free function (test#10)
- `iter_messages` async-iter mock pattern documented (test#11)
- Logging schema subsection (silent-failure LOW#4 + conventions #7)
- AuthKeyError mid-flight (silent-failure LOW#1)
- `get_me()` at listener startup (silent-failure LOW#2)
- `.env.example` updated (conventions #10)
- live_trades FK contract acknowledged (conventions #2)
- `datetime(col)` helper pattern referenced (conventions #4)
- Audit-before-architectural-reasoning step in rollout 8 (conventions #5)
- `TgSocialAuthError(channel, reason)` descriptive `__init__` (conventions #8)
- Bootstrap idempotence (test#16)
- Cutover scoping test (test#15, BL-060 lesson)
- First-join FloodWait cap raised to 600s with operator-page (devil's advocate WORTH-NOTING #9)
- Mcap floor decision documented as "no re-introduction; safety + curator + bounded loss is the rug filter"
- Co-occurrence trust-laundering covered by two-tier alert template
- CLI naming consolidated under `cli.py` subcommands

## Prior art

- **LonamiWebs/Telethon** (MIT, archived Feb 2026, moved to Codeberg): primary library. Listener pattern, session, `iter_messages(min_id=...)`.
- **paulpierre/informer** (MIT, 1.6k ★, last commit Oct 2025): production reference for multi-channel monitoring + FloodWait at 500-channel scale. Pattern reference, not import.
- **DarkWebInformer/telegram-scraper** (MIT, 311 ★): SQLite per-channel `last_message_id` resume pattern. Generalized to `tg_social_watermarks` table.
- **Gregsayshi/Telegram_Streamer** (MIT): cashtag/hashtag tally + highest-count attribution rule. Adopted for multi-token messages.
- **Pyrogram (discontinued)** and **Hydrogram** (GPL, pre-1.0): considered and rejected as fallbacks.
