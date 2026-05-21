# Operator-Alert Activation Checklist

**Date:** 2026-05-21 (verified state)
**BL:** `BL-NEW-NARRATIVE-OPERATOR-ALERT-WIRE`
**Status:** `ENDPOINT-SHIPPED / HERMES-SKILL-PENDING` (endpoint deployed PR #176; SKILL.md patch documented PR #212; activation purely operator-blocked).

## Why this exists

One-page sequential checklist that distills the two long runbooks into the exact 5 operator actions, in order. Each step references the detail runbook for full hygiene + verification commands.

## Verified prerequisites (2026-05-21 18:08Z)

| Check | State | Action implied |
|---|---|---|
| Endpoint deployed | ✅ `POST /api/internal/operator-alert` returns 503 (feature-gated off) | Step 1 below |
| `OPERATOR_ALERT_HMAC_SECRET` in `/root/gecko-alpha/.env` | ❌ absent (grep -c → 0) | Step 1 |
| `/home/gecko-agent/.hermes/.env` has same secret | ❌ absent (assumed; not verified to avoid stat without need) | Step 2 |
| `SKILL.md` Path C1 patch applied | ❌ unchanged since 2026-05-13 15:07Z (Path B log-only) | Step 3 |
| Dispatch activity (last 24h) | none | will fire after Step 5 |

## Activation steps (in order)

### Step 1 — Set the secret on `/root/gecko-alpha/.env` (srilu-vps)
**Runbook:** `tasks/runbook_operator_alert_activation_2026_05_19.md` §Step 1-3.

1. Generate locally (do NOT paste into chat or commit): `python3 -c "import secrets; print(secrets.token_hex(32))"`
2. SSH with TTY: `ssh -t srilu-vps` then follow runbook §Step 2 (idempotent `awk` in-place edit; secret never appears on any command line).
3. Restart `gecko-pipeline`: `systemctl restart gecko-pipeline && systemctl is-active gecko-pipeline`.
4. **Verify:** endpoint must now return 401 (HMAC required), not 503: `curl -s -o /dev/null -w "%{http_code}\n" -X POST http://localhost:8000/api/internal/operator-alert -H "Content-Type: application/json" -d '{"message":"smoke","source":"runbook"}'`. If still 503, the secret didn't load (re-check Step 1.2 verify).

### Step 2 — Set the SAME secret on `/home/gecko-agent/.hermes/.env`
**Runbook:** `tasks/runbook_operator_alert_skill_patch_2026_05_21.md` §Step 5e.

1. Same hygiene pattern, but as `gecko-agent` user (`sudo -u gecko-agent bash -c '...'`).
2. The Hermes dispatcher signs outbound HMAC with this value; gecko-alpha verifies the inbound HMAC with the value from Step 1. They MUST be identical, or the dispatcher gets 403 and silently falls back to Path B (log-only).
3. Verify: `grep -c "^OPERATOR_ALERT_HMAC_SECRET=." /home/gecko-agent/.hermes/.env` returns 1.

### Step 3 — Apply the SKILL.md patch
**Runbook:** `tasks/runbook_operator_alert_skill_patch_2026_05_21.md` §Step 5a-5d.

Copy 4 sections into `/home/gecko-agent/.hermes/skills/narrative_alert_dispatcher/SKILL.md`:
- 5a — Path C1 constants block (after existing `## Single-source constants`).
- 5b — `_post_operator_alert` helper.
- 5c — Replace existing `elif resp.status == 503:` branch (log-only) with Path C1 POST + Path B fallback.
- 5d — Append to "TODO before activation" list at bottom.

Restart whatever process hosts the Hermes dispatcher (per the gecko-agent runtime config; not detailed here — operator knowledge).

### Step 4 — Smoke test (independent-gating proof)
**Runbook:** `tasks/runbook_operator_alert_activation_2026_05_19.md` §Step 6a-6e.

1. Back up the narrative secret (Step 6a).
2. Unset `NARRATIVE_SCANNER_HMAC_SECRET` (Step 6b).
3. Restart gecko-pipeline; record timestamp (Step 6c).
4. **Within ~2 minutes:** dispatcher should detect narrative-side 503 + POST to operator-alert + log `operator_alert_dispatched` → `operator_alert_delivered` triplet. A Telegram message should land in the operator chat with body `narrative_dispatcher_misconfig: ...`.
5. **Restore the narrative secret** (Step 6e). This is non-optional — leaving it unset disables the narrative pipeline.

### Step 5 — Mark SHIPPED
After Step 4 evidence confirms (operator_alert_delivered log + Telegram message):
- Update `BL-NEW-NARRATIVE-OPERATOR-ALERT-WIRE` status in `backlog.md`: `ENDPOINT-SHIPPED / HERMES-SKILL-PENDING` → `SHIPPED`.
- File a small docs PR.

## Rollback

If activation produces unwanted alert noise or proves to be a poor design fit:

1. Set `OPERATOR_ALERT_HMAC_SECRET=` (empty) on both `.env` files. Restart `gecko-pipeline`.
2. Endpoint reverts to 503. Dispatcher's Path C1 POST returns 503 → `_post_operator_alert` returns False → falls back to Path B (log-only). No code revert needed.

Net blast-radius if rolled back: zero (additive only).

## What this checklist DOES NOT do

- Does not generate or print secrets.
- Does not modify `.env` files on srilu-vps.
- Does not edit the Hermes SKILL.md.
- Does not run the smoke test.

All operator-driven. The two underlying runbooks have the verbatim commands.
