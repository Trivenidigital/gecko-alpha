# Runbook — enable CoinGecko Demo API key (BL-NEW-CG-FREE-TIER-DEMO-API-KEY)

Date: 2026-05-18
Backlog: BL-NEW-CG-FREE-TIER-DEMO-API-KEY (`backlog.md`)
Operator-gated: do not register or apply unless explicitly approved.

## Why

Post-#170 deploy evidence shows the lane-reorder fix lifts held_position
success rate from ~10% to ~55% but cannot lift the binding CG IP-rate-limit
ceiling. Free-tier CG without an API key shares a noisy IP pool. The Demo
API key gives a stable per-key allocation (nominally 30 req/min) and is
expected to materially drop 429 pressure. See
`tasks/findings_cg_budget_attribution_2026_05_18.md` for the underlying
diagnosis.

## Drift-check (2026-05-18 against master `5fcff85`)

The key is threaded through 16 call sites across 8 modules. **No code
change is required** — all sites already read `settings.COINGECKO_API_KEY`
conditionally. Setting the env var enables every consumer at once.

### Production call sites that auth with the key

| Module | Line(s) | Auth form |
|---|---|---|
| `scout/ingestion/coingecko.py` | 98-99, 190-191, 227-228, 318-319, 440-441 | query param `x_cg_demo_api_key` |
| `scout/ingestion/held_position_prices.py` | 182-183 | query param `x_cg_demo_api_key` |
| `scout/trending/tracker.py` | 88 | query param `x_cg_demo_api_key` |
| `scout/secondwave/detector.py` | 155-156 | **HTTP header** `x-cg-demo-api-key` |
| `scout/narrative/agent.py` | 97, 129, 206, 260, 483, 590 | indirect (passes key into module helpers) |
| `scout/briefing/collector.py` | 403 | indirect (getattr) |
| `scout/trading/minara_alert.py` | 156 | indirect (getattr) |

Both `x_cg_demo_api_key` query param and `x-cg-demo-api-key` HTTP header are
valid CG Demo auth methods. The mixed-form usage is benign — both authenticate
the same way upstream. No reconciliation needed for this runbook; flagged for
awareness only.

### Config field

`scout/config.py:68` — `COINGECKO_API_KEY: str = ""` (Pydantic BaseSettings;
loads from VPS `.env` automatically on service restart).

`.env.example:19` already documents the line shape:
```
COINGECKO_API_KEY=              # Optional: CoinGecko Demo API key (free tier)
```

## Hermes-first (fresh check 2026-05-18)

Operator-credential registration. Not a code-shape problem.

| Domain | Hermes skill found 2026-05-18? | Decision |
|---|---|---|
| API-credential vault / secret-injection skill that owns CG free-tier registration | No (checked installed VPS skills `/home/gecko-agent/.hermes/skills/`, Hermes optional-skills catalog, awesome-hermes-agent) | Operator registers directly at coingecko.com/en/api |
| CoinGecko market-data integration | Yes (Hermes optional blockchain/finance skills reference CG for pricing) | Those skills consume CG but do not supply a key for gecko-alpha's process |

Verdict: operator-only credential registration; no Hermes path replaces the
.env edit.

## Pre-flight (do these before changing anything)

Capture the pre-key baseline so post-key validation has a comparison anchor.
Run on srilu-vps using the Windows SSH two-step pattern (redirect-to-file,
then Read).

```bash
# Step 1 — Bash tool: SSH with redirect
ssh root@srilu-vps '
echo "===CURRENT_KEY_STATUS==="
if grep -q "^COINGECKO_API_KEY=." /root/gecko-alpha/.env; then
  echo "key already set (will not overwrite without confirmation)"
else
  echo "key empty — runbook applicable"
fi
echo
echo "===PRE_KEY_BASELINE_2H==="
SINCE="2 hours ago"
echo "cg_429_backoff count:"
journalctl -u gecko-pipeline --since "$SINCE" --no-pager 2>/dev/null | grep -cE "cg_429_backoff"
echo
echo "held_position_refresh_summary count + success rate:"
journalctl -u gecko-pipeline --since "$SINCE" --no-pager 2>/dev/null | grep -E "\"event\": \"held_position_refresh_summary\"" | python3 -c "
import sys, json
total = 0
success = 0
for line in sys.stdin:
    ix = line.find(\"{\")
    if ix < 0: continue
    try:
        d = json.loads(line[ix:])
        total += 1
        if d.get(\"refreshed_count\", 0) > 0: success += 1
    except: pass
print(f\"total_cycles={total} successful_cycles={success} success_rate={success/total*100 if total else 0:.1f}%\")
"
echo
echo "coingecko_lanes_stopped_for_backoff distribution:"
journalctl -u gecko-pipeline --since "$SINCE" --no-pager 2>/dev/null | grep -E "\"event\": \"coingecko_lanes_stopped_for_backoff\"" | python3 -c "
import sys, json
from collections import Counter
c = Counter()
for line in sys.stdin:
    ix = line.find(\"{\")
    if ix < 0: continue
    try:
        d = json.loads(line[ix:])
        c[d.get(\"after\", \"?\")] += 1
    except: pass
for k, v in c.most_common(): print(f\"  {v} {k}\")
print(f\"  total={sum(c.values())}\")
"
' > .ssh_pre_key_baseline.txt 2>&1

# Step 2 — Read tool: read the file
# (use Claude Code Read or `Get-Content .ssh_pre_key_baseline.txt` from PowerShell)
```

Record the baseline output in a follow-up findings doc named
`tasks/findings_cg_demo_api_key_validation_2026_05_18.md` BEFORE applying
the key. Without a baseline, the 2h post-key window can't be evaluated.

## Step 1 — register the Demo key

1. Open `https://www.coingecko.com/en/api/pricing` in a browser.
2. Click "Demo plan — Get API key" (free tier; no credit card).
3. Sign in or create a CoinGecko account.
4. Generate a Demo API key from the dashboard.
5. Treat the key as secret. Do NOT paste it into any committed file, PR
   description, or Slack/Discord channel. Move directly to step 2 on the VPS.

## Step 2 — set the key on srilu-vps

SSH into the VPS with a TTY allocated so `read -rsp` can prompt for the
key without echoing it back. Take a backup first, then write the key via
the prompt — the key value never appears in your command, shell history,
or terminal scrollback.

```bash
ssh -t root@srilu-vps '
cd /root/gecko-alpha
cp .env .env.bak.pre-cg-demo-key-2026-05-18
printf "\n# CoinGecko Demo API key (BL-NEW-CG-FREE-TIER-DEMO-API-KEY)\n" >> .env
# read -rsp: -r preserves backslashes, -s suppresses echo, -p shows prompt
read -rsp "CoinGecko Demo API key: " CG_KEY
printf "\nCOINGECKO_API_KEY=%s\n" "$CG_KEY" >> .env
unset CG_KEY
echo
echo "===VERIFY==="
# Show line numbers + redact the value so the key is never printed back.
grep -nE "^COINGECKO_API_KEY=" .env | sed "s/=.*/=<redacted>/"
'
```

`-t` forces a TTY so the interactive prompt works through SSH. `read -rsp`
reads the line silently — nothing appears on screen as you paste, and the
key never lands in any history file because it's only ever held in the
shell variable `CG_KEY`, which `unset` discards before SSH exits.

Confirm via the verify block:
- Exactly one `COINGECKO_API_KEY=` line is present.
- The value column shows `<redacted>`, not the actual key.
- No duplicate `COINGECKO_API_KEY=` rows exist. An earlier empty default may
  shadow the new one; if a duplicate appears, edit `.env` to delete the
  empty line (use an editor that doesn't echo file contents back into your
  scrollback — `vi /root/gecko-alpha/.env`, locate the empty line, remove
  it, save).

**Operational hygiene:**
- Do NOT `cat .env`, `tail .env`, or `grep COINGECKO_API_KEY .env` without
  the `sed` redaction. The raw grep prints the key.
- Do NOT paste the key into any chat / commit / PR / scratch file. The
  only place it should live is `/root/gecko-alpha/.env` (mode 0600 by
  convention; verify with `ls -la /root/gecko-alpha/.env`).
- If you accidentally echo the key to your terminal, treat it as compromised
  — rotate via the CoinGecko dashboard and re-run Step 2 with the new key.

## Step 3 — restart the pipeline

```bash
ssh root@srilu-vps '
date -u +"restart_at=%Y-%m-%dT%H:%M:%SZ"
systemctl restart gecko-pipeline
sleep 3
systemctl is-active gecko-pipeline
systemctl show gecko-pipeline -p ActiveEnterTimestamp -p MainPID --value
'
```

Record `restart_at` — it anchors the 2h validation window in step 5.

Note: this restart resets the in-memory cycle counter for the held-position
lane and clears any in-flight 429 cooldown timer. The fresh 2h window
should start cleanly.

## Step 4 — rollback (if anything regresses)

If post-key behavior worsens (e.g., new error class, increased failure
rate, or unexpected 401/403 from CG):

```bash
ssh root@srilu-vps '
cd /root/gecko-alpha
cp .env.bak.pre-cg-demo-key-2026-05-18 .env
systemctl restart gecko-pipeline
date -u +"rollback_at=%Y-%m-%dT%H:%M:%SZ"
systemctl is-active gecko-pipeline
'
```

The backup is a byte-identical pre-key copy. Restart picks up the empty
`COINGECKO_API_KEY` and reverts to anonymous free-tier behavior.

If the rollback is needed, file a follow-up findings doc capturing the
regression evidence before re-attempting.

## Step 5 — 2h validation

Wait at least 2h after the restart in step 3, then run the same baseline
query for the post-key window. The cycle cadence is ~2-3min/cycle, so 2h
captures ~30-60 cycles — well above the 10-cycle floor required.

```bash
ssh root@srilu-vps '
SINCE="<restart_at from step 3>"
echo "===POST_KEY_2H==="
echo "cg_429_backoff count:"
journalctl -u gecko-pipeline --since "$SINCE" --no-pager 2>/dev/null | grep -cE "cg_429_backoff"
echo
echo "held_position_refresh_summary success rate + ≥10-consecutive-clean check:"
journalctl -u gecko-pipeline --since "$SINCE" --no-pager 2>/dev/null | grep -E "\"event\": \"held_position_refresh_summary\"" | python3 -c "
import sys, json
total = 0
success = 0
max_streak = 0
streak = 0
for line in sys.stdin:
    ix = line.find(\"{\")
    if ix < 0: continue
    try:
        d = json.loads(line[ix:])
        total += 1
        if d.get(\"refreshed_count\", 0) > 0:
            success += 1
            streak += 1
            if streak > max_streak: max_streak = streak
        else:
            streak = 0
    except: pass
print(f\"total_cycles={total} successful={success} success_rate={success/total*100 if total else 0:.1f}% longest_consecutive_clean_streak={max_streak}\")
"
echo
echo "coingecko_lanes_stopped_for_backoff distribution post-key:"
journalctl -u gecko-pipeline --since "$SINCE" --no-pager 2>/dev/null | grep -E "\"event\": \"coingecko_lanes_stopped_for_backoff\"" | python3 -c "
import sys, json
from collections import Counter
c = Counter()
for line in sys.stdin:
    ix = line.find(\"{\")
    if ix < 0: continue
    try:
        d = json.loads(line[ix:])
        c[d.get(\"after\", \"?\")] += 1
    except: pass
for k, v in c.most_common(): print(f\"  {v} {k}\")
print(f\"  total={sum(c.values())}\")
"
' > .ssh_post_key_validation.txt 2>&1
```

Then Read `.ssh_post_key_validation.txt`.

## Success criteria

All three required, evaluated against the pre-key baseline captured in
step 0:

1. **`cg_429_backoff` drops ≥50%.** Compare the count in the post-key 2h
   window against the pre-key 2h baseline. If pre-key was 42, post-key
   must be ≤21.
2. **held-position refresh succeeds for ≥10 consecutive cycles** at any
   point in the 2h window. The post-key script reports
   `longest_consecutive_clean_streak`; that value must be ≥10.
3. **No scanner lane chronically starved.** The
   `coingecko_lanes_stopped_for_backoff` distribution should not show any
   single scanner with a share materially higher than its proportional
   call-budget weight (top_movers ≤6, by_volume ≤8, midcap_gainers ≤4 in
   a 2h window are reasonable upper bounds based on per-cycle call counts).

If 1 and 2 pass but 3 shows a scanner consuming an outsized share, file a
narrower follow-up rather than rolling back — the Demo key win is real
and a scanner-specific tuning is the next layer.

## Explicit note: #158 24h validation remains separate

The Demo API key validation is its own 2h window. It does NOT close out
the BL-NEW-HELD-POSITION-REFRESH-RATE-GAP 24h soak — that gate requires
extended journal evidence outside sustained 429 windows and is graded
against the pre-#170-deploy stale cohort. Treat them as independent.

## Worked-example success shape (for sanity reference)

If the key works as PR #129's deploy notes predicted, the post-key 2h
window should look approximately like:

```
cg_429_backoff count: <20 (target: ≥50% drop from baseline)
total_cycles=~30-50  successful=~30-50  success_rate=>95%
longest_consecutive_clean_streak=>20  (well above the 10-cycle floor)
coingecko_lanes_stopped_for_backoff distribution:
  <5 events total
```

If the post-key window looks more like the pre-key window, suspect:
- Key not loaded. Primary signal: `cg_candidates_returned.has_api_key`
  field in the journal should be `true` post-key. If it's `false`, the
  Pydantic Settings load missed the line. Re-verify the `.env` presence
  with the redacted grep from Step 2 (`grep ... | sed "s/=.*/=<redacted>/"`),
  never the raw grep — the key must not be printed back.
- Key invalid (CG returns 401/403; look for `cg_http_error` events).
- IP-rate-limit issue persists despite the key (escalate; this would
  indicate the binding constraint is not the per-IP free pool).

## What this runbook is NOT

- Not authorization to register the key. Operator-gated.
- Not a code change. All sites already conditionally use the key; no in-tree
  edit is required.
- Not a #158 24h validation closure path. See note above.
- Not a fallback-design retirement. PR #163's `/coins/{id}` fallback design
  stays on file pending evidence outside 429 windows.
