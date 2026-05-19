# Runbook — activate operator-alert endpoint (BL-NEW-NARRATIVE-OPERATOR-ALERT-WIRE final step)

Date: 2026-05-19
Backlog: BL-NEW-NARRATIVE-OPERATOR-ALERT-WIRE (status: ENDPOINT-SHIPPED / HERMES-SKILL-PENDING)
Operator-gated: do not run unless explicitly approved.

## Why this runbook exists

PR #176 shipped the gecko-alpha endpoint `POST /api/internal/operator-alert`
(merged `012e67c`, deployed to srilu on 2026-05-19T00:20:36Z as part of
`ec4f35c`). The endpoint is feature-gated 503 until `OPERATOR_ALERT_HMAC_SECRET`
is set on srilu `.env`, AND the Hermes-side dispatcher SKILL.md is updated
to POST with the same secret. This runbook captures the remaining
out-of-repo steps and the smoke test that flips the backlog from
`ENDPOINT-SHIPPED / HERMES-SKILL-PENDING` to full `SHIPPED`.

## Pre-flight verification

Confirm prod state before changing anything. Two-step SSH pattern per
Windows constraints (Bash redirect → file → Read tool).

```bash
ssh root@srilu-vps '
cd /root/gecko-alpha
echo "===VPS_HEAD==="
git rev-parse --short HEAD   # expect ec4f35c or later
echo
echo "===INTERNAL_ALERT_PRESENT==="
test -f scout/api/internal_alert.py && echo "ok" || echo "MISSING — abort"
echo
echo "===NARRATIVE_SECRET_STATUS==="
grep -E "^NARRATIVE_SCANNER_HMAC_SECRET=" .env | sed "s/=.*/=<redacted>/"
echo
echo "===OPERATOR_SECRET_STATUS==="
grep -E "^OPERATOR_ALERT_HMAC_SECRET=" .env | sed "s/=.*/=<redacted>/" \
  || echo "OPERATOR_ALERT_HMAC_SECRET=<not set>"
echo
echo "===SERVICE==="
systemctl is-active gecko-pipeline
' > .ssh_preflight_operator_alert.txt 2>&1
```

Then Read `.ssh_preflight_operator_alert.txt`. Required state:
- `VPS_HEAD` is `ec4f35c` or later (master tip when this runbook was written).
- `INTERNAL_ALERT_PRESENT` is `ok`.
- `NARRATIVE_SCANNER_HMAC_SECRET` is set (`<redacted>` shown).
- `OPERATOR_ALERT_HMAC_SECRET` is NOT set — this runbook is what sets it.
- Service is `active`.

If `OPERATOR_ALERT_HMAC_SECRET` is already set, stop. Either it's an old
config from a previous attempt, or someone else has already run this
runbook. Investigate before continuing.

## Step 1 — generate the secret

Generate locally (do NOT use a shared shell that records history):

```bash
python3 -c "import secrets; print(secrets.token_hex(32))"
```

This produces a 64-char hex string (32 bytes). Keep it in memory only.
Do NOT paste it into chat, commit, scratch file, or PR description.
Do NOT echo or print it again after this step.

## Step 2 — set the secret on srilu `.env`

SSH with TTY so `read -rsp` can prompt silently. Same secret-hygiene
pattern as `tasks/runbook_cg_demo_api_key_2026_05_18.md`, with two
additions:

1. **Idempotent.** If `OPERATOR_ALERT_HMAC_SECRET=` already exists in
   `.env` (e.g., a prior failed attempt), the line is replaced. If
   absent, it is appended. No risk of duplicate rows that Pydantic would
   pick the wrong one of.
2. **Secret never appears on any command line.** The pasted value is
   written to a `umask 077` tmpfile and read by awk via filename — no
   shell-variable interpolation into the awk command line, no `set` /
   `ps` exposure beyond the brief `read -rsp` window.

```bash
ssh -t root@srilu-vps '
cd /root/gecko-alpha
cp .env .env.bak.pre-operator-alert-2026-05-19

# Read the secret silently into a shell variable and immediately write
# it to a 0600-mode tmpfile so the rest of the flow can pass it via
# filename, never via shell interpolation.
read -rsp "OPERATOR_ALERT_HMAC_SECRET (paste, will not echo): " OP_KEY
echo
umask 077
printf "%s\n" "$OP_KEY" > .op_secret.tmp
unset OP_KEY

# Idempotent in-place edit. awk reads the new value from
# .op_secret.tmp (filename only on the command line). If the key
# already exists in .env, the line is replaced; otherwise append a
# comment block + the line.
awk -v new_secret_file=".op_secret.tmp" '"'"'
BEGIN {
    getline new_value < new_secret_file
    close(new_secret_file)
    found = 0
}
/^OPERATOR_ALERT_HMAC_SECRET=/ {
    print "OPERATOR_ALERT_HMAC_SECRET=" new_value
    found = 1
    next
}
{ print }
END {
    if (!found) {
        print ""
        print "# OPERATOR_ALERT_HMAC_SECRET (BL-NEW-NARRATIVE-OPERATOR-ALERT-WIRE)"
        print "OPERATOR_ALERT_HMAC_SECRET=" new_value
    }
}
'"'"' .env > .env.update.tmp && mv .env.update.tmp .env
rm -f .op_secret.tmp

echo "===VERIFY==="
grep -nE "^OPERATOR_ALERT_HMAC_SECRET=" .env | sed "s/=.*/=<redacted>/"
'
```

The verify line should show **exactly one** row, `<redacted>` after the
`=`. If two rows appear, the idempotent logic failed; investigate
before continuing. If zero rows appear, the tmpfile was empty or
unreadable.

## Step 3 — restart gecko-pipeline so Pydantic picks up the new secret

```bash
ssh root@srilu-vps '
date -u +"restart_at=%Y-%m-%dT%H:%M:%SZ"
systemctl restart gecko-pipeline
sleep 3
systemctl is-active gecko-pipeline
systemctl show gecko-pipeline -p ActiveEnterTimestamp -p MainPID --value
'
```

Record the `restart_at` timestamp. The new secret loads at process start
(Pydantic `BaseSettings` reads `.env` at construction).

## Step 4 — verify the endpoint is no longer 503

Quick sanity check: without HMAC headers, the endpoint should now return
**401** (missing headers), not 503 (disabled). 503 would mean the secret
didn't load.

```bash
ssh root@srilu-vps '
curl -s -o /dev/null -w "%{http_code}\n" \
  -X POST http://127.0.0.1:8000/api/internal/operator-alert \
  -H "Content-Type: application/json" \
  -d "{\"message\": \"\", \"source\": \"\"}"
' > .ssh_endpoint_probe.txt 2>&1
```

Expected: `401`. If `503`, the secret didn't load — re-check Step 2's
verify output and the restart timestamp.

## Step 5 — update the Hermes dispatcher SKILL.md

Switch the dispatcher from Path B (log-only `narrative_dispatcher_misconfig`)
to Path C1 (POST `/api/internal/operator-alert`). The SKILL.md lives at
`/home/gecko-agent/.hermes/skills/narrative_alert_dispatcher/SKILL.md`
on srilu under the `gecko-agent` user — different from `root`.

Required SKILL changes (shape, not literal):

1. Add the secret to the gecko-agent environment in a place the
   dispatcher can read. The exact location depends on how the Hermes
   dispatcher is configured to source credentials; the most likely path
   is `/home/gecko-agent/.hermes/.env` (per the same pattern as
   `NARRATIVE_SCANNER_HMAC_SECRET` on the Hermes side).
2. The dispatcher must compute the canonical signature using the same
   scheme as `scout/api/narrative.py:_compute_signature`:
   ```
   canonical = f"{METHOD}\n{PATH}\n{QUERY}\n{X-Timestamp}\n".encode() + body
   signature = HMAC-SHA256(secret, canonical).hexdigest()
   ```
   For the operator-alert endpoint:
   - METHOD = `POST`
   - PATH = `/api/internal/operator-alert`
   - QUERY = `` (empty — endpoint takes no query params)
   - X-Timestamp = current unix seconds
   - body = JSON-encoded `{"message": "...", "source": "..."}`
3. POST headers must include `X-Timestamp` (unix seconds string) and
   `X-Signature` (hex HMAC).
4. Replace the current 503-on-narrative-side handler that emits
   `narrative_dispatcher_misconfig` with a path that:
   - Builds a `message` string capturing the failure context
     (e.g., `"narrative_dispatcher_misconfig: NARRATIVE_SCANNER_HMAC_SECRET unset"`)
   - POSTs to `https://<gecko-alpha-host>/api/internal/operator-alert`
     with the HMAC headers above
   - Treats a 200 response as alert-delivered; logs the response body
     (which is `{"status": "delivered", "source": ...}`).
   - Treats a non-2xx (e.g., 502 if Telegram fails on gecko-alpha)
     as a delivery failure — log + retry once with a 10s delay, then
     fall back to the prior log-only behavior if both attempts fail.

The exact SKILL.md edit is operator-driven because (a) the Hermes
dispatcher format and Path C1 contract are owned outside this repo, and
(b) the secret value must not appear in any text under `gecko-agent`'s
shell history.

## Step 6 — smoke test (the documented Reviewer 1 P2 acceptance gate)

The smoke test confirms the **independent gating** that the P1 fold
introduced: the dispatcher can raise a Telegram alert even when
`NARRATIVE_SCANNER_HMAC_SECRET` is missing/broken on gecko-alpha.

### Step 6a — back up the narrative secret

```bash
ssh root@srilu-vps '
cd /root/gecko-alpha
grep -E "^NARRATIVE_SCANNER_HMAC_SECRET=" .env > .narrative_secret_backup.tmp
chmod 600 .narrative_secret_backup.tmp
echo "narrative secret backed up to .narrative_secret_backup.tmp"
'
```

### Step 6b — unset the narrative secret to simulate the failure mode

```bash
ssh root@srilu-vps '
cd /root/gecko-alpha
sed -i "s/^NARRATIVE_SCANNER_HMAC_SECRET=.*/NARRATIVE_SCANNER_HMAC_SECRET=/" .env
echo "===VERIFY==="
grep -nE "^NARRATIVE_SCANNER_HMAC_SECRET=" .env | sed "s/=.*/=<empty>/"
'
```

Expected: `<empty>`.

### Step 6c — restart so the empty narrative secret takes effect

Write the restart timestamp to a tmpfile on the VPS so Step 6d can
read it without manual copy-paste of a placeholder.

```bash
ssh root@srilu-vps '
date -u +"%Y-%m-%dT%H:%M:%SZ" > /tmp/operator_alert_smoke_test_start.txt
echo "restart_at=$(cat /tmp/operator_alert_smoke_test_start.txt)"
systemctl restart gecko-pipeline
sleep 3
systemctl is-active gecko-pipeline
'
```

The `restart_at` line is echoed back for your terminal record;
`/tmp/operator_alert_smoke_test_start.txt` is the authoritative copy
that Step 6d reads.

### Step 6d — observe the dispatcher fire its operator alert

Within ~2 minutes (one Hermes-side dispatcher cycle), the dispatcher
should detect that POSTs to `/api/narrative-alert` are returning 503
(feature gated off by empty secret) and POST to
`/api/internal/operator-alert`. On gecko-alpha:

```bash
ssh root@srilu-vps '
SINCE=$(cat /tmp/operator_alert_smoke_test_start.txt)
echo "===WINDOW_START==="
echo "since=$SINCE"
echo
echo "===OPERATOR_ALERT_DISPATCHED==="
journalctl -u gecko-pipeline --since "$SINCE" --no-pager 2>/dev/null \
  | grep -E "\"event\": \"operator_alert_(dispatched|delivered|failed)\""
echo
echo "===NARRATIVE_503==="
journalctl -u gecko-pipeline --since "$SINCE" --no-pager 2>/dev/null \
  | grep -E "narrative_scanner_request_rejected.*disabled" | wc -l
' > .ssh_smoke_test_evidence.txt 2>&1
```

Required evidence:
- At least one `operator_alert_dispatched` event followed by
  `operator_alert_delivered` (in that order, §12b ordering).
- Non-zero `narrative_scanner_request_rejected.*disabled` count — proves
  the dispatcher's POSTs to the narrative endpoint are being correctly
  503'd while it falls through to the operator-alert path.
- A Telegram message lands in the operator's configured TG chat with a
  body containing the source string the dispatcher tagged (likely
  `narrative_dispatcher_misconfig` or similar).

If `operator_alert_failed` appears, the gecko-alpha → Telegram leg has a
problem; check `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` on srilu `.env`
or for outbound network blocks.

If neither dispatched nor failed appears within 2 minutes, the
dispatcher isn't calling the endpoint — check the Hermes SKILL.md change
from Step 5.

### Step 6e — restore the narrative secret

This step is **mandatory and must run regardless of smoke-test outcome**.

The restore uses awk reading the backup file directly — the secret
never enters a shell variable or a sed substitution string, so there's
no shell-history / `set` / `ps`-line exposure of the secret, and no
risk of sed mis-handling regex special characters in the value.

```bash
ssh root@srilu-vps '
cd /root/gecko-alpha

# awk reads .narrative_secret_backup.tmp (containing the full
# "NARRATIVE_SCANNER_HMAC_SECRET=<secret>" line from Step 6a) and the
# current .env. For each .env line matching NARRATIVE_SCANNER_HMAC_SECRET=,
# print the backup line as-is; otherwise pass through. The secret never
# enters a shell variable or sed substitution string.
awk '"'"'
    NR==FNR { backup = $0; next }
    /^NARRATIVE_SCANNER_HMAC_SECRET=/ { print backup; next }
    { print }
'"'"' .narrative_secret_backup.tmp .env > .env.restore.tmp \
  && mv .env.restore.tmp .env

rm -f .narrative_secret_backup.tmp /tmp/operator_alert_smoke_test_start.txt

echo "===VERIFY==="
grep -nE "^NARRATIVE_SCANNER_HMAC_SECRET=" .env | sed "s/=.*/=<redacted>/"

systemctl restart gecko-pipeline
sleep 3
systemctl is-active gecko-pipeline
'
```

Expected: `<redacted>` shown, service `active`. Also clears the
`/tmp/operator_alert_smoke_test_start.txt` timestamp file from Step 6c.

## Step 7 — flip backlog to full SHIPPED

After Step 6 passes end-to-end (dispatched → delivered → Telegram lands),
file a small docs PR flipping the backlog status:

```
BL-NEW-NARRATIVE-OPERATOR-ALERT-WIRE
- Status: ENDPOINT-SHIPPED / HERMES-SKILL-PENDING → SHIPPED 2026-05-19
- Evidence: <ISO timestamp of smoke-test operator_alert_dispatched log>
- Hermes SKILL.md: updated; OPERATOR_ALERT_HMAC_SECRET set on srilu .env
```

Mirrors the chain-anchor PR #175 pattern — bookkeeping flips happen as
small docs PRs, not deferred indefinitely.

## Rollback

If anything regresses at any step, restore the pre-runbook state:

```bash
ssh root@srilu-vps '
cd /root/gecko-alpha
cp .env.bak.pre-operator-alert-2026-05-19 .env
systemctl restart gecko-pipeline
date -u +"rollback_at=%Y-%m-%dT%H:%M:%SZ"
systemctl is-active gecko-pipeline
echo
echo "===VERIFY==="
grep -nE "^(OPERATOR_ALERT_HMAC_SECRET|NARRATIVE_SCANNER_HMAC_SECRET)=" .env \
  | sed "s/=.*/=<redacted_or_empty>/"
'
```

This restores the byte-identical pre-runbook `.env` (before
`OPERATOR_ALERT_HMAC_SECRET` was added AND before the narrative secret
was unset in the smoke test). The Hermes dispatcher will fall back to
Path B (log-only) because its HMAC POSTs will now 503 against the
gecko-alpha endpoint.

After rollback, file a follow-up findings doc capturing the regression
evidence before attempting Step 5+ again.

## Operational hygiene

- Do NOT `cat .env`, raw-grep `OPERATOR_ALERT_HMAC_SECRET` without the
  `sed` redaction, or paste the secret into any chat / commit / PR.
- The secret should live in exactly two places: `/root/gecko-alpha/.env`
  on srilu (mode 0600) AND wherever the gecko-agent user's Hermes
  dispatcher reads its credentials. Anywhere else is a leak.
- If you accidentally echo the secret to your terminal, treat it as
  compromised — rotate via Step 1+2 with a new value.

## What this runbook is NOT

- Not authorization to run any step. Operator-gated.
- Not a code change. The endpoint shipped via PR #176; this runbook
  only documents activation.
- Not a Hermes SKILL.md write. The SKILL.md edit lives under the
  `gecko-agent` user on srilu and must be done by the operator with
  visibility into the current Hermes dispatcher format.
- Not a Telegram credential setup. Assumes `TELEGRAM_BOT_TOKEN` /
  `TELEGRAM_CHAT_ID` already work on gecko-alpha (verified by the prior
  `parse_mode=None` work and held-position WARN deliveries).
