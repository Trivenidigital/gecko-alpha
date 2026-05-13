**New primitives introduced:** new Hermes skill `crypto_narrative_scanner` (orchestrator) + 4 sub-skills (`kol_watcher`, `narrative_classifier`, `coin_resolver`, `narrative_alert_dispatcher`) installed under `/home/gecko-agent/.hermes/skills/` on main-vps; new gecko-alpha HTTPS endpoint `GET /api/coin/lookup` on srilu-vps:8000 (thin pass-through to existing scout/ingestion machinery); new scout DB table `narrative_alerts_inbound` (records Hermes-emitted events for `narrative_prediction` signal to consume); new gecko-agent cron entry for the orchestrator skill; one new gecko-alpha Settings field `NARRATIVE_SCANNER_HMAC_SECRET` for endpoint auth.

# Design: crypto_narrative_scanner — pure-Hermes-skill narrative-pump detection

## Status

DESIGN v0 — drafted 2026-05-13 for review. Not yet approved. Pre-registered evaluation criteria in §7 are the load-bearing decision points; everything above them is implementation detail subject to revision.

## Context

Two strategic gaps named by operator 2026-05-13:
1. **Latency:** gecko-alpha + Minara catches on-chain manifestation of narrative pumps (e.g., `goblincoin` 2026-05-11T22:26, `chill-guy`, `troll-2`, `useless-3`), but only AFTER tokens appear on CoinGecko/DexScreener — minutes-to-hours behind the originating X post.
2. **Coverage:** gecko-alpha sees only narratives that produce on-chain volume above the CG-listing threshold. Pre-CG narratives (the actually-early ones) are structurally invisible.

**Canonical worked examples (multi-chain):**
- Solana: `$GOBLIN` (goblincoin) — caught by gecko-alpha+Minara on-chain, but the originating tweet preceded gecko's detection by an unknown delta. $HANTA hamster-virus narrative — similar shape.
- Ethereum: **`$ASTORID`** — operator reports ~**60,000% in a day**. Pure narrative-driven pump on ETH. Out of Minara M1.5c's Solana-only scope, so even when narrative-scanner alerts, the operator manually trades. This is the canonical ETH-side case V1 must catch.
- Base: similar narrative-pump dynamic; included in V1 scope.

The Hermes-based scanner closes both gaps — earlier detection (tweet timestamp ≈ scanner trigger) AND broader coverage (tweets that haven't manifested on-chain yet) — **across Solana + ETH + BASE.**

## §0 Architectural reversal note (per BL-072 §7a discipline)

**This design reverses BL-072's stance** that "gecko-alpha does NOT run on Hermes." The reversal is *bounded*: only this narrative-scanner subsurface lives on Hermes (main-vps:`gecko-agent` user). The rest of gecko-alpha — pipeline, evaluator, dashboard, alerter, Minara emit, all 11 existing signals — stays vanilla async Python on srilu-vps. Cross-VPS handoff is read-only events flowing FROM Hermes TO gecko-alpha; gecko-alpha never depends on Hermes being up.

If Hermes side dies, gecko-alpha continues uninterrupted (just without the narrative-scanner ingestion source). If gecko-alpha side dies, Hermes scanner keeps logging but its outputs don't reach the trade pipeline.

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

1. **Curated KOL list** — operator-maintained list in `crypto_narrative_scanner/kol_list.yaml` (~20-50 hand-picked accounts: Elon, top crypto Twitter, Solana ecosystem founders). NOT discovery-based ("track high-engagement crypto tweets from any account") — that's a V2 problem with major sybil/spam risk.

2. **Cron-driven** — Hermes cron skill fires `crypto_narrative_scanner` every 15 min. Adjustable. No real-time streaming.

3. **Per-tweet processing pipeline** (all within Hermes skills):
   - `kol_watcher` skill: polls each KOL's recent tweets via `xurl` (or `felo-x-search`), dedupe against memory, return new ones
   - `narrative_classifier` skill: LLM call (kimi-k2-thinking via OpenRouter) — *"Is this tweet about a specific crypto coin or narrative? If yes, extract: cashtag(s), contract address(es) **for Solana (base58, 32-44 chars) OR Ethereum/Base (0x + 40 hex chars)**, narrative theme (1-2 words), urgency signal (rumor/announcement/launch). For each CA, infer chain (solana, ethereum, base) from format + context."* Returns structured JSON.
   - `coin_resolver` skill: for each cashtag/CA extracted, calls gecko-alpha's `GET /api/coin/lookup?symbol=X&chain=Y` (HTTPS, HMAC-authed) which returns canonical CoinGecko + DexScreener data. Multi-chain pass-through — gecko-alpha already supports Solana/ETH/BASE/+ via existing ingestion. NO direct DEX scraping from Hermes side.
   - `narrative_alert_dispatcher` skill: writes the structured event to gecko-alpha's `narrative_alerts_inbound` table via the same HTTPS endpoint (`POST /api/narrative-alert`, HMAC-authed). Does NOT directly send TG — gecko-alpha's existing alerter handles that. **For Solana-resolved alerts, the existing M1.5c Minara emit fires downstream automatically; for ETH/BASE alerts, no Minara command (M1.5d EVM not shipped) — operator manually trades.**

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

**Auth:** HMAC over request body + timestamp using `NARRATIVE_SCANNER_HMAC_SECRET` from `.env` on both sides. NOT plain API key — minimizes exposure if either VPS leaks.

**Reliability:** Hermes side retries 3× with exponential backoff. gecko-alpha side accepts duplicate events idempotently (UNIQUE on `(tweet_id, signal_type)`).

**Failure mode:** if gecko-alpha endpoint down, Hermes scanner logs locally + queues. On recovery, drains queue. No data loss bounded by main-vps disk.

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

2. **One new table** `narrative_alerts_inbound`:
   ```sql
   CREATE TABLE narrative_alerts_inbound (
       id INTEGER PRIMARY KEY,
       tweet_id TEXT NOT NULL,
       tweet_author TEXT NOT NULL,
       tweet_ts TEXT NOT NULL,
       tweet_text TEXT NOT NULL,
       extracted_cashtag TEXT,
       extracted_ca TEXT,         -- base58 (Solana) OR 0x-hex (ETH/BASE)
       extracted_chain TEXT,      -- 'solana' | 'ethereum' | 'base' | NULL if cashtag-only
       resolved_coin_id TEXT,
       narrative_theme TEXT,
       urgency_signal TEXT,
       classifier_confidence REAL,
       received_at TEXT NOT NULL DEFAULT (datetime('now')),
       UNIQUE(tweet_id, extracted_cashtag, extracted_chain)
   );
   ```

3. **One new Settings field** `NARRATIVE_SCANNER_HMAC_SECRET: str = ""` in `scout/config.py`; if empty, endpoints return 503 (feature disabled).

4. **One new signal_type** `narrative_scanner` (or extends existing `narrative_prediction`) in the `signals.py` table — consumes `narrative_alerts_inbound` rows that resolved to known coins.

**That's it on the gecko-alpha side.** ~150 LOC max. Hermes side does the heavy lifting.

## §6 Cron + scheduling

Hermes-side cron entry (under gecko-agent):
```
*/15 * * * * /home/gecko-agent/.local/bin/hermes run crypto_narrative_scanner
```

Active hours adjustable. V1 runs 24/7 at 15-min intervals. V2 might add KOL-specific cadence (Elon polled at 5 min, mid-tier KOLs at 30 min, etc.).

**Cost estimate (V1):**
- xurl: $5/mo X API minimum
- LLM classification: ~20-50 tweets per cycle × 96 cycles/day × ~$0.001/tweet = ~$2-5/day = ~$60-150/month
- Total: ~$70-200/month operating cost, OpenRouter-billed (your account)

## §7 Pre-registered evaluation criteria (per BL-072 + Vector B/C discipline)

**Window:** 4 weeks from V1 ship date. Decision-locked at ship+28d.

**Primary metrics (tracked independently per chain, agreement required for "strong-pattern" verdict — per Vector C F-C1 fold from the dashboard PR):**

1. **Latency reduction** — for each narrative pump where BOTH the Hermes scanner alerted AND gecko-alpha alerted via existing CG-ingestion path, measure `gecko_alert_ts - hermes_alert_ts`. Track distribution, not just mean. Computed per-chain.
2. **Coverage delta** — count narrative pumps surfaced by Hermes scanner that gecko-alpha CG path NEVER caught (zero overlap). Pure-coverage wins. **Per-chain expectation:** ETH/BASE coverage delta is likely larger than Solana (because Minara-emit gives Solana a head-start; ETH/BASE have no equivalent shortcut). Worth tracking separately to validate.
3. **Precision** — fraction of Hermes-emitted alerts that resolved to a real pump (defined as: token had ≥+50% peak within 24h of alert). Avoids the "scanner spams every tweet" failure mode. Computed per-chain.

**Per-chain breakdown is load-bearing:** Solana, ETH, BASE may behave very differently — ETH alerts may have lower volume but higher per-alert magnitude (ASTORID-shape); Solana alerts may have higher volume and lower precision. A single blended verdict obscures this. Per-chain verdict avoids the chain_completed-degenerate trap from the dashboard PR.

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
| Classifier false positives flood gecko-alpha | Precision metric ≥30% gate; classifier confidence threshold gate |
| HMAC secret leak across VPSes | `.env` permissions 0600; rotation procedure documented in runbook |
| Hermes side cost overrun (X API + LLM) | Daily cost ceiling alert; OpenRouter dashboard monitoring |
| Endpoint DoS via spammed bad HMAC | Rate-limit on gecko-alpha side: 100 req/min per source IP |
| Cross-VPS plumbing fails silently | Each Hermes skill logs to journalctl; gecko-alpha logs received events; explicit drift-check at week 1 |
| Self-Evolution Kit ran prematurely on noisy v1 data | Don't activate kit until week 4 evaluation passes |
| Reverses BL-072 without doc update | Fold into `docs/gecko-alpha-alignment.md` at ship — single line in Part 1 |

## §9 What this does NOT close

- **V2 work** (general narrative discovery beyond curated KOL list, engagement-velocity tracking, bot filtering)
- **Real-time streaming** (X firehose; requires paid enterprise; out of scope)
- **Auto-trading** (this is decision-support only — feeds existing paper-trade pipeline)
- **Minara-on-ETH/BASE auto-emit** (M1.5d EVM is its own backlog item; V1 narrative scanner alerts for ETH/BASE pumps just don't include a Minara command line — operator manually trades on those alerts. This is acceptable per operator who already executes EVM manually.)
- **Self-evolution activation** (kit installed but not invoked; wait for V1 eval data)
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

1. **Day 1 (~4 hrs):** gecko-alpha-side endpoints + table migration + HMAC plumbing. Ship to srilu-vps. Independently testable with curl.
2. **Day 2 (~4 hrs):** Hermes skills (5 new SKILL.md files) under gecko-agent. xurl/felo-x-search skill install. Manual single-cycle test.
3. **Day 3 (~2 hrs):** Cron entry + 24h shakedown. Watch journalctl + logs both sides.
4. **Day 4+ (4 weeks):** soak. Operator review at weeks 1, 2, 3, 4.
5. **Week 4:** evaluate against §7 criteria. Decide V2 scope, kill, or extend.

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
