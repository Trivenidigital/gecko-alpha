**New primitives introduced:** new Hermes skill `crypto_narrative_scanner` (orchestrator) + 4 sub-skills (`kol_watcher`, `narrative_classifier`, `coin_resolver`, `narrative_alert_dispatcher`) installed under `/home/gecko-agent/.hermes/skills/` on main-vps; new gecko-alpha HTTPS endpoint `GET /api/coin/lookup` on srilu-vps:8000 (thin pass-through to existing scout/ingestion machinery); new scout DB table `narrative_alerts_inbound` (records Hermes-emitted events for `narrative_prediction` signal to consume); new gecko-agent cron entry for the orchestrator skill; one new gecko-alpha Settings field `NARRATIVE_SCANNER_HMAC_SECRET` for endpoint auth.

# Design: crypto_narrative_scanner — pure-Hermes-skill narrative-pump detection

## Status

DESIGN v0 — drafted 2026-05-13 for review. Not yet approved. Pre-registered evaluation criteria in §7 are the load-bearing decision points; everything above them is implementation detail subject to revision.

## Context

Two strategic gaps named by operator 2026-05-13:
1. **Latency:** gecko-alpha + Minara catches on-chain manifestation of narrative pumps (e.g., `goblincoin` 2026-05-11T22:26, `chill-guy`, `troll-2`, `useless-3`), but only AFTER tokens appear on CoinGecko/DexScreener — minutes-to-hours behind the originating X post.
2. **Coverage:** gecko-alpha sees only narratives that produce on-chain volume above the CG-listing threshold. Pre-CG narratives (the actually-early ones) are structurally invisible.

**Canonical worked examples (multi-chain):**
- Solana: `$GOBLIN` (goblincoin) — caught by gecko-alpha+Minara on-chain at 2026-05-11T22:26Z, but the originating tweet preceded gecko's detection by an unknown delta. $HANTA hamster-virus narrative — similar shape.
- Ethereum: **`$ASTORID`** — operator reports ~**60,000% in a day**. Pure narrative-driven pump on ETH. Out of Minara M1.5c's Solana-only scope, so even when narrative-scanner alerts, the operator manually trades.
- Base: similar narrative-pump dynamic; included in V1 scope.

The Hermes-based scanner closes both gaps — earlier detection (tweet timestamp ≈ scanner trigger) AND broader coverage (tweets that haven't manifested on-chain yet) — **across Solana + ETH + BASE.**

**ASTORID walkthrough — honest framing of V1's structural ceiling (Vector B SC-1 fold):**

Whether V1 would have caught ASTORID depends entirely on whether ASTORID's originating tweet propagated through one of the 15 curated KOLs. The chain of custody is:

1. ASTORID-originating account tweets → Did a KOL on our list see it within ~30 min?
2. KOL retweets / quotes / mentions → `kol_watcher` catches it on next cron tick (max 30-min latency post-KOL-tweet)
3. `narrative_classifier` extracts the ETH CA (0x-hex) → confidence ≥ 0.6 → POST to gecko-alpha
4. gecko-alpha `coin_resolver` resolves the CA via `scout/ingestion/dexscreener.py` → returns canonical data
5. New `narrative_scanner` signal row → existing alerter → operator manually trades on ETH

**Where V1 structurally cannot catch ASTORID:** if the originating account is NOT on the curated KOL list AND no curated KOL retweets/quotes it within ~30 min, V1 misses it entirely. 60,000% pumps often originate from anon/low-follower accounts that go viral via algorithmic amplification, not KOL signal-boosting. The §7 "operator-manual-tagged comparison set" measures exactly this ceiling: how many pumps did the operator find through other channels that V1 didn't catch?

If at week 4 the manual-tag recall is <20% (V1 misses what operator finds elsewhere), the verdict is **Coverage-Ceiling-Hit → pivot to V2 discovery-based scope** (track high-engagement-velocity crypto tweets from any account, accept the sybil/spam complexity). V1 is the cheap version that proves the ARCHITECTURE works; V2 is the expensive version that solves COVERAGE.

## §0 Architectural reversal note (per BL-072 §7a discipline)

**This design reverses BL-072's stance** that "gecko-alpha does NOT run on Hermes." The reversal is *bounded*: only this narrative-scanner subsurface lives on Hermes (main-vps:`gecko-agent` user). The rest of gecko-alpha — pipeline, evaluator, dashboard, alerter, Minara emit, all 11 existing signals — stays vanilla async Python on srilu-vps. Cross-VPS handoff is read-only events flowing FROM Hermes TO gecko-alpha; gecko-alpha never depends on Hermes being up.

If Hermes side dies, gecko-alpha continues uninterrupted (just without the narrative-scanner ingestion source). If gecko-alpha side dies, Hermes scanner keeps logging but its outputs don't reach the trade pipeline.

**Bounded-reversal kill trigger (added per Vector B ORM-2):** if at any point a second gecko-alpha capability is proposed to live on Hermes BEFORE V1's 4-week evaluation completes, **halt the proposal and re-evaluate BL-072 wholesale.** "Bounded" without a concrete trigger is wishful framing; this is the trigger.

Acknowledged: this reversal should be formally recorded in `docs/gecko-alpha-alignment.md` Part 1 (Deployed patterns) as "Hermes-native subsurface: narrative_scanner only." Not in this PR; tracked as a fold-in.

## §1 What Hermes can/cannot deliver (honest framing)

Per the §7b Hermes-first analysis already run this session:

| Capability | Hermes-native? | How |
|---|---|---|
| Cron / scheduled triggers | ✅ Yes | Built-in `hermes gateway install` adds cron skill |
| X data ingestion (read tweets, search, user timelines) | ✅ Yes | `xurl` skill (official X v2 CLI, paid $5+/mo) OR `felo-x-search` skill (Felo proxy, alt pricing) |
| LLM classification | ✅ Yes | Within any skill via `hermes.llm` primitive — currently configured to OpenRouter → moonshotai/kimi-k2-thinking |
| Persistent memory across runs | ✅ Yes | `~/.hermes/memories/` — Hermes writes/reads structured memory |
| Solana on-chain cross-reference | 🟡 Partial | `hermes-blockchain-oracle` skill (community, MCP-based) covers Solana |
| ETH / BASE on-chain cross-reference | ❌ No Hermes skill | **Route via gecko-alpha's HTTPS endpoint** — existing `scout/ingestion/dexscreener.py` + `scout/ingestion/geckoterminal.py` already handle 17 chains incl. ETH/BASE; Hermes side stays chain-agnostic, just extracts CAs and lets gecko-alpha resolve |
| CoinGecko / DexScreener lookups | ❌ No skill exists | Use thin HTTPS endpoint on gecko-alpha (see §3); leverages existing scout/ingestion code |
| Structured TG alerts | ✅ Yes | Hermes gateway WhatsApp/TG; OR cross-VPS handoff to gecko-alpha's existing TG alerter |
| **Real-time tweet streaming** | ❌ No skill | X firehose requires paid X enterprise tier; out of scope |
| **Bot/sybil engagement filtering** | ❌ No skill | Hard NLP problem; deferred to V2 if at all |
| **Auto-tuning from PnL feedback** | 🟡 Self-Evolution Kit | Installed 2026-05-13; requires ≥30 eval-data points before useful; defer activation |

**What this means concretely:** V1 is a *KOL-polling scanner*, not a real-time firehose. Latency = cron interval (15-30 min poll → tweet-to-alert ≈ 15-45 min worst case). That's still 10-100× faster than the existing CG-ingestion path for narrative pumps but not "first 30-60 seconds" as one might hope.

## §2 V1 scope: known-KOL watcher

**The tractable v1, ship-first**, deliberately scoped narrow:

1. **Curated KOL list — 15 accounts (revised down from 27)** — operator-maintained in `crypto_narrative_scanner/kol_list.yaml`. NOT discovery-based ("track high-engagement crypto tweets from any account") — that's a V2 problem with major sybil/spam risk. **Reason for revision:** Vector A FCo-4 found X v2 Basic tier ($200/mo) caps at 10k tweets/month; 27 KOLs × 96 cycles/day = 78k tweets/month is 7.8× over budget. 15 KOLs × 48 cycles/day = 21.6k/month — still over Basic, requires X Pro ($5K/mo absurd) OR drop cadence further (see point 2).

2. **Cron-driven — every 30 min (revised down from 15 min)** — fires `crypto_narrative_scanner` 48×/day. With 15 KOLs that's ~21.6k user-timeline calls/month — fits within X v2 Basic ($200/mo) with operational margin. Latency budget: tweet-to-alert ~15-45 min worst case (vs 15-30 in v0). Trade-off: marginally slower detection in exchange for staying within stated cost ceiling.

   **Cadence/KOL trade matrix (Vector A FCo-4):**
   | KOLs | Cron (min) | Tweets/mo | X v2 tier needed |
   |---|---|---|---|
   | 15 | 30 | ~21.6k | Basic ($200/mo) — tight but workable |
   | 15 | 60 | ~10.8k | Basic ($200/mo) — comfortable |
   | 27 | 60 | ~19.4k | Basic ($200/mo) — tight |
   | 27 | 30 | ~38.9k | Basic insufficient → Pro $5K/mo |
   | 27 | 15 | ~77.8k | Pro $5K/mo |

   Operator can revisit cadence + KOL count after week-2 cost data.

3. **Per-tweet processing pipeline** (all within Hermes skills):
   - `kol_watcher` skill: polls each KOL's recent tweets via `xurl`, dedupe against memory, return new ones
   - `narrative_classifier` skill: LLM call (kimi-k2 non-thinking via OpenRouter — **revised from kimi-k2-thinking per Vector A FCo-5** to avoid reasoning-token surcharge; only fall back to thinking-SKU on ambiguous cases via confidence-driven retry) — *"Is this tweet about a specific crypto coin or narrative? If yes, extract: contract address(es) **for Solana (base58, 32-44 chars) OR Ethereum/Base (0x + 40 hex chars)**, optional cashtag, narrative theme (1-2 words), urgency signal (rumor/announcement/launch), confidence (0.0-1.0). For each CA, infer chain (solana, ethereum, base) from format + context."* Returns structured JSON. **Hermes-side confidence floor: skip POST to gecko-alpha if confidence < 0.6** (Vector B HLD-1 fold — confidence gating is a precision/recall business decision and lives on the heavy-lifting side, NOT gecko-alpha).
   - `coin_resolver` skill: **CA-only resolution in V1** (Vector A FC-1 fold — gecko-alpha has no `lookup_by_symbol(symbol, chain)` primitive; symbol-only lookup is structurally ambiguous due to multi-chain symbol collisions). For each CA extracted, calls gecko-alpha's `GET /api/coin/lookup?ca={ca}&chain={chain}` (HTTPS, HMAC-authed). Cashtag-only tweets get inserted into `narrative_alerts_inbound` with `resolved_coin_id=NULL` and a deferred resolution pass at next cycle (gives the CA time to materialize on DexScreener if the tweet was pre-launch).
   - `narrative_alert_dispatcher` skill: writes the structured event to gecko-alpha's `narrative_alerts_inbound` table via `POST /api/narrative-alert` (HMAC-authed). Does NOT directly send TG — gecko-alpha's existing alerter handles that. **Hermes-computed `event_id`** (sha256 of `tweet_id + tweet_text_hash + extracted_ca`) is the idempotency key (Vector B HLD-2 fold — gecko-alpha's UNIQUE constraint is `UNIQUE(event_id)`, not tied to Hermes's classifier output shape). **For Solana-resolved alerts, the existing M1.5c Minara emit fires downstream automatically; for ETH/BASE alerts, no Minara command (M1.5d EVM not shipped) — operator manually trades.**

4. **Memory:** each skill writes to `~/.hermes/memories/narrative_scanner/`:
   - `seen_tweets.jsonl` — appends `{tweet_id, author, ts, classified_as}` for dedupe and historical analysis
   - `kol_baselines.jsonl` — per-KOL tweet rate, classification hit rate
   - `narrative_outcomes.jsonl` — at T+24h, T+72h, lookup whether the surfaced coin pumped or faded (queries gecko-alpha's outcome data via same HTTPS endpoint)

## §3 Cross-VPS integration shape

Per (α) decision (this session, earlier): Hermes on main-vps + gecko-alpha on srilu-vps, talk over network. **Read-only handoff:**

```
main-vps (Hermes)               srilu-vps (gecko-alpha)
─────────────────               ─────────────────────────
narrative_alert_dispatcher  ──HTTPS POST──>  /api/narrative-alert     (HMAC-authed)
                                             └─> INSERT narrative_alerts_inbound
                                                 └─> existing narrative_prediction
                                                     signal consumes on next cycle

coin_resolver               ──HTTPS GET───>  /api/coin/lookup?symbol=X (HMAC-authed)
                                             └─> existing scout/ingestion machinery
                                             <── canonical CG + DexScreener payload
```

**Auth — concrete HMAC spec (Vector A FC-2 fold):**

```
canonical = f"{HTTP_METHOD}\n{REQUEST_PATH}\n{X-Timestamp-header}\n{REQUEST_BODY}"
signature = HMAC-SHA256(NARRATIVE_SCANNER_HMAC_SECRET, canonical)
client sends: X-Timestamp header (unix epoch seconds) + X-Signature header (hex)
```

**Server-side checks (gecko-alpha):**
1. Reject if `|now() - X-Timestamp| > 300s` (clock-skew + replay window)
2. Verify HMAC matches (constant-time compare)
3. Reject if `(X-Timestamp, X-Signature)` already seen in last 600s (in-process LRU cache, 10k entries; reset on uvicorn restart is acceptable since the 300s window self-clears)

**Rate limit (Vector A FC-3 fold):** uses `slowapi` dependency (added to pyproject.toml; in-process backend acceptable for single-uvicorn-worker deploy). **HMAC-keyed rate limit, not source-IP** — because both VPSes may share egress NAT, source-IP is bypassable; the HMAC-derived client identity is the real authorization unit. Limit: 100 req/min per HMAC secret (one secret = one client = the gecko-agent scanner).

**Reliability — file-backed queue (Vector A FCo-1 fold):** Hermes-side cron is NOT a daemon — each invocation is a fresh process; in-memory queue is fiction. Outbound failures append to `/home/gecko-agent/.hermes/memories/narrative_scanner/outbound_queue.jsonl` (atomic append: write to `.tmp` then `os.replace`). At each cron tick, scanner first drains the queue (oldest-first, up to 50 per tick) before processing new tweets. Queue durable across process restarts; bounded by disk (auto-prune > 1000 entries).

**Idempotency (Vector B HLD-2 fold):** gecko-alpha schema uses `UNIQUE(event_id)` where `event_id` is Hermes-computed `sha256(tweet_id + tweet_text_hash + extracted_ca)`. Classifier output drift on the same tweet → same event_id → duplicate insert rejected. Tweet edits (X allows 30-min edit) → different `tweet_text_hash` → different event_id → new row (intended; the operator should see the edited tweet).

**Failure mode:** if gecko-alpha endpoint down, Hermes scanner logs locally + appends to outbound queue. On recovery, drains queue oldest-first. No data loss bounded by main-vps disk.

## §4 Skills inventory (what gets installed)

Under `/home/gecko-agent/.hermes/skills/`:

| Skill | Custom (new) or existing | Purpose |
|---|---|---|
| `xurl` OR `felo-x-search` | existing (one of) | X API access |
| `hermes-blockchain-oracle` | existing (optional, V1.5) | Solana on-chain checks (rugcheck, dev wallet) |
| `crypto_narrative_scanner` | **NEW (orchestrator)** | Cron entry point; calls sub-skills in sequence |
| `kol_watcher` | **NEW** | Per-KOL tweet polling + dedupe |
| `narrative_classifier` | **NEW** | LLM classification of tweets → structured JSON |
| `coin_resolver` | **NEW** | HTTPS call to gecko-alpha for canonical coin data |
| `narrative_alert_dispatcher` | **NEW** | HTTPS POST to gecko-alpha narrative-alerts endpoint |

**Each new skill is a SKILL.md file + minimal embedded Python (or zero — many Hermes skills are pure-prompt).** No standalone bash scripts. No Python files outside `skills/<name>/` directories. The orchestrator skill `crypto_narrative_scanner` is the SINGLE thing cron invokes; it composes the sub-skills.

This is the "pure-skills" constraint operationalized. The proposal's `narrative_scanner.sh` + `scan_narratives.py` shape is explicitly NOT what this design ships.

## §5 What's NEW in gecko-alpha (minimum surface)

To support the cross-VPS handoff, gecko-alpha gets:

1. **Two new HTTPS endpoints** in `dashboard/api.py` (or new `scout/api/narrative.py` if dashboard is wrong home):
   - `GET /api/coin/lookup?symbol={X}&chain={Y}` — returns CG + DexScreener data; HMAC-authed
   - `POST /api/narrative-alert` — writes inbound event row; HMAC-authed; idempotent

2. **One new table** `narrative_alerts_inbound` (revised per Vector A N-2 + FC-2 + Vector B HLD-2):
   ```sql
   CREATE TABLE narrative_alerts_inbound (
       id INTEGER PRIMARY KEY,
       event_id TEXT NOT NULL UNIQUE,     -- Hermes-computed sha256(tweet_id+text_hash+ca)
       tweet_id TEXT NOT NULL,
       tweet_author TEXT NOT NULL,
       tweet_ts TEXT NOT NULL,
       tweet_text TEXT NOT NULL,
       tweet_text_hash TEXT NOT NULL,     -- sha256(tweet_text); detects edits
       extracted_cashtag TEXT,
       extracted_ca TEXT,                 -- base58 (Solana) OR 0x-hex (ETH/BASE); NULL if cashtag-only
       extracted_chain TEXT,              -- 'solana' | 'ethereum' | 'base' | NULL
       resolved_coin_id TEXT,             -- NULL when cashtag-only (deferred-resolution case)
       narrative_theme TEXT,
       urgency_signal TEXT,
       classifier_confidence REAL,        -- audited only; gating happens Hermes-side (Vector B HLD-1)
       classifier_version TEXT NOT NULL,  -- "kimi-k2:v1" — re-classification audit (Vector A FCo-2)
       received_at TEXT NOT NULL DEFAULT (datetime('now'))
   );
   CREATE INDEX idx_narrative_inbound_received ON narrative_alerts_inbound(received_at);
   CREATE INDEX idx_narrative_inbound_resolved ON narrative_alerts_inbound(resolved_coin_id);
   ```

3. **One new Settings field** `NARRATIVE_SCANNER_HMAC_SECRET: str = ""` in `scout/config.py`; if empty, endpoints return 503 (feature disabled).

4. **One new signal_type** `narrative_scanner` (or extends existing `narrative_prediction`) in the `signals.py` table — consumes `narrative_alerts_inbound` rows that resolved to known coins.

**That's it on the gecko-alpha side.** ~150 LOC max. Hermes side does the heavy lifting.

## §6 Cron + scheduling

Hermes-side cron entry (under gecko-agent):
```
*/30 * * * * /home/gecko-agent/.local/bin/hermes run crypto_narrative_scanner
```

48 cycles/day. Active hours 24/7. V2 might add KOL-specific cadence (Elon polled at 5 min, mid-tier KOLs at 60 min, etc.).

**Cost estimate (V1) — revised per Vector A FCo-4 + FCo-5 + Vector B TU-2.** The v0 estimate ($70-200/mo) was structurally wrong:

| Component | v0 (wrong) | V1 (corrected) | Source of correction |
|---|---|---|---|
| X API access | "$5/mo" | **~$200/mo (X v2 Basic tier)** | v0 cited keyless metering pricing; user-timeline polling requires Basic tier minimum. At 15 KOLs × 48 cycles/day × ~1.5 tweets/cycle (peak) ≈ 21.6k tweets/month. Basic cap is 10k tweets/month — **tight**, needs the slower cadence to fit. |
| LLM classification | "$60-150/mo" (kimi-k2-thinking + N=1 tweets) | **$20-60/mo** (kimi-k2 non-thinking, N=1-3 tweets) | v0 used thinking-SKU pricing; non-thinking is 3-10× cheaper. Active-KOL tweets-per-cycle is 1-3 (not 20-50). Confidence-driven fallback to thinking-SKU only for ambiguous cases (~5% of classifications). |
| Hermes runtime (compute on main-vps) | $0 | $0 (already paid) | Existing VPS rental |
| **Realistic V1 total** | "$70-200/mo" | **~$220-260/mo** | |

**Hard cost ceiling enforcement:**
- Daily classifier-spend cap in `crypto_narrative_scanner` orchestrator skill: skip remaining cycles if cumulative day spend > $3 (= ~$90/mo) AND emit operator alert
- Cumulative X v2 quota watch: if monthly polls exceed 8k (80% of Basic cap), throttle to 60-min cadence automatically + emit operator alert
- Operator can revisit cost ceiling at week-2 review; cheaper KOLs / fewer KOLs is the dominant lever

## §7 Pre-registered evaluation criteria (per BL-072 + Vector B/C discipline)

**Window:** 4 weeks from V1 ship date. Decision-locked at ship+28d.

**Primary metrics (tracked independently per chain, agreement required for "strong-pattern" verdict — per Vector C F-C1 fold from the dashboard PR):**

1. **Latency reduction** — for each narrative pump where BOTH the Hermes scanner alerted AND gecko-alpha alerted via existing CG-ingestion path, measure `gecko_alert_ts - hermes_alert_ts`. Track distribution, not just mean. Computed per-chain.
2. **Coverage delta** — count narrative pumps surfaced by Hermes scanner that gecko-alpha CG path NEVER caught (zero overlap). Pure-coverage wins. **Per-chain expectation:** ETH/BASE coverage delta is likely larger than Solana (because Minara-emit gives Solana a head-start; ETH/BASE have no equivalent shortcut). Worth tracking separately to validate.
3. **Precision** — fraction of Hermes-emitted alerts that resolved to a real pump (defined as: token had ≥+50% peak within 24h of alert). Avoids the "scanner spams every tweet" failure mode. **Threshold revised from 30% to 15%** (Vector B TU-1: base rate of "crypto-mentioning tweet → +50% in 24h" from curated KOLs is empirically 5-15%, not 30%; v0 threshold over-rewarded a noisy detector). Computed per-chain.

**Per-chain breakdown is load-bearing** BUT per-chain n<10 verdicts are exploratory-only (Vector B SC-3 fold): Solana likely 20+ alerts in 4 weeks, ETH plausibly 2-5, BASE plausibly 0-2. ETH+BASE folded into single "EVM" verdict if individual chain n<10 at week 4.

**Operator-manual-tagged comparison set (Vector B SC-2 fold) — load-bearing addition:**

Coverage delta as defined above measures "Hermes caught what gecko-alpha CG path missed." This is biased toward making V1 look good: it cannot detect narrative pumps that NEITHER caught because the originator wasn't in the curated KOL list. To measure V1's structural ceiling honestly, **operator manually tags 3-5 pumps/week observed elsewhere** (their own X scroll, Telegram channels, post-hoc CG Highlights, friends, etc.) and we check at week 4 what fraction of those V1 surfaced. This is the only way to detect the "we built a scanner whose curated KOL list misses the actually-interesting accounts" failure mode.

**Revised verdict matrix (with Redundant added per Vector B ORM-1):**
- **Strong-pattern (worth full V2 scope):** ALL of [latency_reduction_median ≥30min, coverage_delta ≥3, precision ≥15%, manual-tag-recall ≥40%]
- **Redundant — close project:** precision passes (≥15%) AND coverage_delta <2 (V1 produces high-quality alerts but they're already caught by gecko-alpha CG path — scanner is duplicative)
- **Moderate (worth narrow V2):** any 1 or 2 of [latency, coverage, precision] pass
- **Coverage-Ceiling-Hit — pivot to V2 discovery:** manual-tag-recall <20% (V1 misses what operator finds elsewhere — curated-KOL ceiling reached, need V2 discovery-based scope)
- **Tracking (kill V2):** none of the four pass
- **INSUFFICIENT_DATA:** Hermes-emitted alerts <10 in window — extend soak per-chain

**Verdict classification:**
- **Strong-pattern (worth full V2 scope):** latency reduction median ≥30 min AND coverage delta ≥3 pumps AND precision ≥30%
- **Moderate (worth narrow V2 — e.g., KOL list refinement, classifier prompt tuning):** any one of the three metrics passes
- **Tracking (kill V2):** none of the three pass
- **INSUFFICIENT_DATA:** fewer than 10 Hermes-emitted alerts in window — extend soak

**Operator-paste / actionability check (paired):** at week 2, week 3, week 4 the operator self-reports how many Hermes alerts they actually acted on. If acted-on rate <10%, the scanner produces noise regardless of metrics — UX problem, not detection problem.

**Excluded from V1 evaluation (NOT a measurement we can usefully make at this n):**
- Bot/sybil engagement filtering quality
- General narrative discovery (V2 only)
- Self-evolution gains (need V1 data first)
- PnL impact (Minara emit + paper trade already measure this downstream)

## §8 Risks + mitigations

| Risk | Mitigation |
|---|---|
| KOL list goes stale (tracked accounts stop tweeting useful signals) | Operator-editable YAML; revisit at week 2 |
| Classifier false positives flood gecko-alpha | Hermes-side confidence floor (0.6); precision metric ≥15% gate at week 4 |
| HMAC secret leak across VPSes | `.env` permissions 0600; rotation procedure documented in runbook; Day 0 generation procedure (see §11) |
| Hermes side cost overrun (X API + LLM) | Daily classifier-spend cap $3/day; X v2 quota watch at 80% Basic cap (8k tweets/mo); auto-throttle to 60-min cadence on quota approach |
| Endpoint DoS via spammed bad HMAC | `slowapi` rate-limit HMAC-keyed: 100 req/min per HMAC secret (Vector A FC-3) |
| Cross-VPS plumbing fails silently | Each Hermes skill logs to journalctl; gecko-alpha logs received events; **freshness SLO on `narrative_alerts_inbound`** (alert if 0 rows in 24h — Vector A DG-2 fold for OpenRouter / model-offline detection) |
| xurl OAuth token expiry silently breaks scanner (Vector A FCo-3) | Day 2 operator runs `xurl auth` interactively; token-expiry watchdog (alert on refresh failure) — Class-2 silent-failure pattern per CLAUDE.md §12b |
| shift-agent's Hermes on main-vps conflicts with gecko-agent's (Vector A DG-4) | Process isolation: gecko-agent uses separate home (`/home/gecko-agent/.hermes/`); shift-agent uses `/root/.hermes/` (currently quarantined per main-vps cleanup 2026-05-13) but if restored, both Hermes instances run under different users with no shared sockets/pid files |
| Self-Evolution Kit ran prematurely on noisy v1 data | Don't activate kit until week 4 evaluation passes AND n≥30 per dimension being tuned (per Vector B HFC-1: V1 scale of n≤50 is non-operational for evolution; this is roadmap not promise) |
| Reverses BL-072 without doc update | Fold into `docs/gecko-alpha-alignment.md` at ship — single line in Part 1. **Bounded-reversal kill trigger per §0**: second Hermes-side capability proposal before V1 4-week eval = halt |

## §9 What this does NOT close

- **V2 work** (general narrative discovery beyond curated KOL list, engagement-velocity tracking, bot filtering)
- **Real-time streaming** (X firehose; requires paid enterprise; out of scope)
- **Auto-trading** (this is decision-support only — feeds existing paper-trade pipeline)
- **Minara-on-ETH/BASE auto-emit** (M1.5d EVM is its own backlog item; V1 narrative scanner alerts for ETH/BASE pumps just don't include a Minara command line — operator manually trades on those alerts. This is acceptable per operator who already executes EVM manually.)
- **Self-evolution activation at V1 scale (Vector B HFC-1 fold — honest framing):** Self-Evolution Kit is installed but **non-operational at V1 scale (n≤50 alerts in 4 weeks).** DSPy + GEPA needs ≥30 eval data points PER DIMENSION being tuned to produce meaningful skill improvements. Naming Self-Evolution here is **roadmap not promise** — realistic activation needs V2 + 3-6 months of accumulated trace data. The operator's earlier "evolves at speed of light" framing is incorrect; the kit operates on weeks-to-months timescales, not seconds.
- **`narrative_prediction` signal merge (Vector B HFC-2 fold):** §10.6 resolved to a NEW `narrative_scanner` signal_type. Merging back into existing `narrative_prediction` later is NOT cheap — requires historical row migration, calibration re-baselining, auto-suspend state reconciliation. The "merge later is cheap" claim from earlier reasoning is unverified; revisit at week 4 only if metrics strongly correlate, and budget proper migration cost.
- **gecko-alpha-side calibration ownership (Vector B HLD-3):** the new signal_type creates a gecko-alpha-side calibration surface via existing `signal_params` machinery (auto-suspend, calibration, digest). This is acceptable as **consumption-side QC backstop** (gecko-alpha owns the trading-decision lifecycle); Hermes-side classifier confidence floor (0.6) is the **production-side QC** that filters before alerts reach gecko-alpha. Two-layer defense: Hermes-side floor (precision) + gecko-alpha auto-suspend (drawdown-driven fail-safe).
- **Hermes-side WhatsApp pairing** (alerts route through gecko-alpha's existing TG; no new WhatsApp identity)
- **Doc fold into `docs/gecko-alpha-alignment.md`** (tracked, deferred to ship PR)

## §10 Decisions log (resolved 2026-05-13)

1. ~~**xurl vs felo-x-search?**~~ **RESOLVED: xurl.** Official X API v2 CLI, paid $5+/mo minimum + per-call. Trade-off acknowledged: known cost + known auth path > unknown Felo pricing. Operator manages X dev account credentials.
2. ~~**Solana-only V1, or include EVM?**~~ **RESOLVED:** V1 covers **Solana + Ethereum + Base** per operator direction. ASTORID (~60K% in a day on ETH) is the canonical worked example. Minara M1.5c-on-Solana fires automatically for resolved Solana alerts; ETH/BASE alerts are decision-support-only (operator manually trades) — M1.5d EVM remains a separate backlog item.
3. ~~**KOL list size?**~~ **RESOLVED: 27.** Operator-curated, written to `crypto_narrative_scanner/kol_list.yaml` at install time. Revisit at week 2 review based on which KOLs produced actionable alerts.
4. ~~**Cron interval?**~~ **RESOLVED (sensible-default): 15 min.** Yields ~96 polling cycles/day. Adjustable at runtime via editing the cron entry. If LLM-classification cost runs hotter than $5/day, throttle to 30 min.
5. ~~**HTTPS endpoint home?**~~ **RESOLVED (sensible-default):** new module `scout/api/narrative.py` (NOT folded into `dashboard/api.py`). Clean separation — narrative endpoints are not dashboard endpoints; mounted on the same FastAPI app via `app.include_router`.
6. ~~**New vs extended signal_type?**~~ **RESOLVED (sensible-default):** new `signal_type = "narrative_scanner"`. Adds a row to `signal_params` so the existing auto-suspend / calibration / digest machinery automatically picks it up. If after 4 weeks the metrics correlate strongly with existing `narrative_prediction`, we merge them — separation now is cheap, merge later is also cheap.
7. ~~**Include `hermes-blockchain-oracle` in V1?**~~ **RESOLVED: yes, V1 includes it.** Solana rugcheck + dev wallet checks add precision-on-Solana before the alert reaches gecko-alpha. ETH/BASE has no equivalent Hermes skill in V1 — `coin_resolver` calls gecko-alpha for both metadata AND any rug-shape signals it can infer from on-chain data via the existing scout/safety.py machinery.

## §11 Implementation sequence (after this design is approved)

**Day 0 — operator prerequisites (Vector A DG-1 + DG-3 fold):**
- Operator generates 32-byte HMAC secret (e.g., `python3 -c "import secrets; print(secrets.token_hex(32))"`). Writes to srilu-vps `/root/gecko-alpha/.env` (`NARRATIVE_SCANNER_HMAC_SECRET=...`) AND to main-vps `/home/gecko-agent/.hermes/.env` (same value). chmod 600 both. **Without this, Day 1 endpoints respond 503 — feature off, no production impact.**
- Operator provides 15-handle KOL list (YAML format, one handle per line, optional weight). Stored at `/home/gecko-agent/.hermes/skills/crypto_narrative_scanner/kol_list.yaml`.
- Operator confirms X dev account tier (Basic ~$200/mo minimum) AND completes interactive xurl OAuth-PKCE pairing on main-vps as gecko-agent user. Cron-fired skills use the refresh token thereafter; manual re-auth needed only on token expiry / revocation.
- Operator confirms revised cost ceiling (~$220-260/mo per §6 corrected math).

1. **Day 1 (~4 hrs):** gecko-alpha-side endpoints + table migration + HMAC plumbing + `slowapi` rate-limit. Ship to srilu-vps. Independently testable with curl using the Day 0 HMAC secret. **Feature gated off: if `NARRATIVE_SCANNER_HMAC_SECRET=""` the endpoints return 503. Safe to deploy in disabled state.**
2. **Day 2 (~4 hrs):** Hermes skills (5 new SKILL.md files) under `/home/gecko-agent/.hermes/skills/`. xurl skill install (uses Day 0 OAuth). Manual single-cycle dry-run with operator's KOL list. Verify outbound queue + HMAC handshake against srilu-vps.
3. **Day 3 (~2 hrs):** Cron entry + 24h shakedown. Watch journalctl on main-vps + check `narrative_alerts_inbound` populates on srilu-vps. Verify cost-cap kicks in if exceeded. Confirm freshness SLO alarm path works (simulate by temporarily disabling cron).
4. **Day 4+ (4 weeks):** soak. Operator review at weeks 1, 2, 3, 4 — including manual-tag set updates per §7. **Operator manually tags 3-5 narrative pumps/week observed elsewhere (not through Hermes scanner) into `tasks/findings_narrative_manual_tags_<week>.md`.**
5. **Week 4:** evaluate against §7 criteria including manual-tag recall. Decide V2 scope, redundant-kill, coverage-ceiling-pivot, moderate-extend, or insufficient-data-extend.

## §12 Revert

- **Disable scanner:** `crontab -e` on main-vps under gecko-agent, comment out the cron line. No data loss; resumable.
- **Disable on gecko-alpha side:** set `NARRATIVE_SCANNER_HMAC_SECRET=""` in srilu-vps `.env` + restart pipeline. Endpoints 503. `narrative_alerts_inbound` rows preserved.
- **Full rollback:** quarantine `/home/gecko-agent/.hermes/skills/crypto_narrative_scanner/` (and the 4 sub-skill dirs) + drop the `narrative_alerts_inbound` table via migration-revert. Existing `narrative_prediction` signal unaffected.

---

**Operator decision points** — all §10 questions resolved 2026-05-13. Implicit acceptance (silence = consent) on:
- BL-072 reversal (narrative-scanner subsurface only)
- §7 pre-registered evaluation criteria (latency 30min / coverage 3 pumps / precision 30% per chain, n-gate 10)
- Cost ceiling (~$70-200/month)

If any of those need revisiting, flag before Day 1 implementation starts.

This doc is now source of truth for the ship sequence in §11. Per BL-072 convention, all subsequent PRs reference back to this design in their commit messages.
