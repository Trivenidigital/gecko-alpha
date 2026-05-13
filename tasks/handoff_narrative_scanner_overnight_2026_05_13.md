**New primitives introduced:** NONE — handoff/status doc summarizing the overnight autonomous build session. References artifacts committed via PR #110 + design doc commits ef1c905/d791ea0. No new code or schema.

# Overnight autonomous build — BL-NEW-NARRATIVE-SCANNER V1 Day 1

**Session date:** 2026-05-12 → 2026-05-13 (overnight autonomous run per operator authorization)

## What shipped (autonomous)

| Phase | Artifact | Status |
|---|---|---|
| Design | `tasks/design_crypto_narrative_scanner.md` (317 LOC, all §10 decisions resolved) | committed `ef1c905`, refined `d791ea0` |
| Design review (2-vector parallel) | Vector A (feasibility) + Vector B (strategic/heavy-lifting) | 3+3 critical/concern findings folded |
| Day 1 build | gecko-alpha-side endpoints + table + signal_type + 25 tests | PR #110 merged `39276bd` |
| PR review (3-vector parallel) | A code-structural / B security / C silent-failure | 1 CRITICAL + 10 IMPORTANT folded in `29fb29f` |
| Deploy | srilu-vps, disabled state (no HMAC secret set) | live as of 2026-05-13T03:03Z |
| Day 2 prep | 5 SKILL.md drafts + placeholder kol_list.yaml under `/home/gecko-agent/.hermes/skills/` on main-vps | drafts in place; activation pending operator inputs |

**Total: 9-step pipeline (Design → 2-review → fold → Build → PR → 3-review → fold → merge → deploy) plus Day 2 scaffolding, all autonomous overnight.**

## Current state by VPS

### srilu-vps (gecko-alpha home, 89.167.116.187)

- HEAD: `39276bd feat(narrative-scanner): Day 1 — gecko-alpha-side HMAC endpoints + migration + tests (#110)`
- Migration: `bl_narrative_scanner_v1` applied at `2026-05-13T03:03:30Z`
- New table: `narrative_alerts_inbound` (empty — feature off)
- New endpoints:
  - `GET /api/coin/lookup?ca=X&chain=Y` → **HTTP 503** (feature off, working as designed)
  - `POST /api/narrative-alert` → **HTTP 503**
- `NARRATIVE_SCANNER_HMAC_SECRET` in `.env`: **not set** (gated off; deploy-safe-by-default)
- Pipeline + dashboard services: both `active`
- No regression in adjacent tests (76/76 still pass)

### main-vps (Hermes home, 46.62.206.192)

- Hermes for `gecko-agent` user installed (per earlier this session): `/home/gecko-agent/.hermes/`
- Self-Evolution Kit installed at `/home/gecko-agent/hermes-agent-self-evolution/` (uv venv, not yet invoked)
- OpenRouter wired (key transferred from srilu, `moonshotai/kimi-k2-thinking` configured)
- **5 SKILL.md drafts placed** at `/home/gecko-agent/.hermes/skills/`:
  - `crypto_narrative_scanner/` (orchestrator + `kol_list.yaml` placeholder)
  - `kol_watcher/`
  - `narrative_classifier/`
  - `coin_resolver/`
  - `narrative_alert_dispatcher/`
- **No cron entry yet.** Skills are scaffolding; activation gated on operator inputs (below).

## What the operator must do before activation

Three blocking inputs the autonomous build couldn't supply:

### 1. Generate + place HMAC secret (~30 sec)
```bash
SECRET=$(python3 -c "import secrets; print(secrets.token_hex(32))")
ssh srilu-vps "echo 'NARRATIVE_SCANNER_HMAC_SECRET=$SECRET' >> /root/gecko-alpha/.env && chmod 600 /root/gecko-alpha/.env && systemctl restart gecko-pipeline gecko-dashboard"
ssh main-vps "sudo -u gecko-agent bash -c 'echo NARRATIVE_SCANNER_HMAC_SECRET=$SECRET >> /home/gecko-agent/.hermes/.env'"
```
Same secret on both VPSes. Settings validator enforces ≥32 chars (token_hex(32) = 64 chars hex — comfortably passes).

### 2. Curate the real KOL list (~10 min)
Edit `/home/gecko-agent/.hermes/skills/crypto_narrative_scanner/kol_list.yaml` on main-vps. The current file has 11 starter handles + 4 PLACEHOLDER entries you need to replace. Format documented in the file. Keep total = 15 to stay within the cost-budgeted X v2 Basic quota.

### 3. xurl OAuth-PKCE pairing (~5 min, interactive)
On main-vps as gecko-agent user:
```bash
sudo -u gecko-agent -i
# inside gecko-agent shell:
hermes install xurl    # installs the xurl skill
xurl auth              # opens browser flow; complete X dev account pairing
```
This requires an X dev account on the Basic tier ($200/mo) per the corrected cost arithmetic in design §6 (Vector A FCo-4 fold).

### 4. (Optional) Adjust cost knobs in `.env`
Defaults are conservative:
- `NARRATIVE_SCANNER_REPLAY_WINDOW_SEC=300` (5-min HMAC timestamp window)
- `NARRATIVE_SCANNER_MAX_BODY_BYTES=16384` (16KB request cap)

## After operator inputs — Day 2/3 activation steps

These were NOT autonomous because they need operator-provided credentials AND interactive X OAuth:

1. Operator completes (1)+(2)+(3) above
2. Smoke test: dry-run `crypto_narrative_scanner` skill once with `hermes run crypto_narrative_scanner --dry-run`
3. Tail journalctl on both VPSes for `narrative_*` events. Verify HMAC handshake works (no 403/401).
4. Wire cron: `sudo -u gecko-agent crontab -e` → add `*/30 * * * * /home/gecko-agent/.local/bin/hermes run crypto_narrative_scanner`
5. 24h shakedown — watch journalctl + check `narrative_alerts_inbound` rows accumulating
6. Day-3 add freshness watchdog (per design §8 + Vector C SFC-4) — ensure `narrative_alerts_inbound` getting rows in 24h windows; alert if empty

## Pre-registered evaluation window (per design §7)

- **Decision date:** writer-deployment + 28d = **2026-06-10**
- **Primary metrics (per-chain: Solana / Ethereum / Base):**
  - Latency reduction median ≥30 min
  - Coverage delta ≥3 pumps
  - Precision ≥15% (revised from 30% per Vector B TU-1)
  - Operator-manual-tag recall ≥40% (added per Vector B SC-2 — operator tags 3-5 pumps/week from other channels to detect curated-KOL ceiling)
- **n-gate**: 10 alerts per chain before per-chain verdict fires; ETH+BASE fold into "EVM" if individual chain n<10
- **Verdict matrix**: Strong-pattern / Redundant (precision passes + coverage<2) / Moderate / Coverage-Ceiling-Hit / Tracking / INSUFFICIENT_DATA

## Documented but NOT folded (Day 2/3 follow-ups)

- Vector B S2: `--workers=1` requirement — replay-LRU is in-process. Day 2 may move to SQLite-backed if multi-worker is needed.
- Vector B D2: Unicode hygiene on `tweet_text` (Class-3 silent-rendering risk per CLAUDE.md §12b precedent). Strip C0/C1 controls + bidi-override codepoints at ingest.
- Vector B D3: NTP requirement (300s window tight). Both VPSes should run `chrony`/`systemd-timesyncd`.
- Vector B D4: secret rotation procedure (`.env` swap + restart on both VPSes).
- Vector A I2: per-request `aiosqlite.connect` vs cached singleton — not blocking at 100 req/min, tracked in design.

## Cost expectations (corrected per Vector A FCo-4 + FCo-5)

| Line | Cost |
|---|---:|
| X v2 Basic tier (10k tweets/month cap) | $200/mo |
| LLM classification (kimi-k2 non-thinking via OpenRouter) | ~$3-15/mo |
| Hermes compute (main-vps already paid) | $0 |
| **Realistic V1 total** | **~$205-220/mo** |

Daily classifier spend cap `$3/day` enforced by orchestrator skill. X v2 quota throttle at 80% Basic cap (8k tweets) auto-throttles to 60-min cadence.

## Branches / commits to reference

- Design v0: `ef1c905`
- Design v1 (post-2-reviewer fold): `d791ea0`
- PR #110 build: `787cb0c`
- PR #110 fold (3-reviewer): `29fb29f`
- PR #110 squash merge: `39276bd`
- Branch `feat/narrative-scanner-day1-gecko-alpha`: deleted post-merge

## How to resume in a new session

```
# Read this handoff doc + design doc
cat tasks/handoff_narrative_scanner_overnight_2026_05_13.md
cat tasks/design_crypto_narrative_scanner.md

# Verify gecko-alpha-side deploy
ssh srilu-vps 'cd /root/gecko-alpha && git log -1 && curl -s -o /dev/null -w "lookup: %{http_code}\n" "http://localhost:8000/api/coin/lookup?ca=X&chain=solana"'

# Verify Hermes-side skill drafts
ssh main-vps 'ls -la /home/gecko-agent/.hermes/skills/{crypto_narrative_scanner,kol_watcher,narrative_classifier,coin_resolver,narrative_alert_dispatcher}/'

# Then: ask operator for KOL list + HMAC secret + xurl pairing → proceed to Day 2/3
```

## Memory updates required

Will fold into MEMORY.md index + per-entry files as part of this handoff:
- `project_narrative_scanner_day1_shipped_2026_05_13.md` — PR #110 ship state
- Update existing `project_dashboard_cohort_view_shipped_2026_05_12.md` cross-link if narrative_scanner findings affect that work

**End-state: Day 1 disabled-state deploy COMPLETE. Activation gated on 3 operator inputs (KOL list + HMAC secret + xurl OAuth). All scaffolding in place; ~30-45 min of operator work to activate.**
