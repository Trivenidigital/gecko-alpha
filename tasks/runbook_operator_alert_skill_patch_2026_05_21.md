# Runbook addendum — concrete SKILL.md patch for BL-NEW-NARRATIVE-OPERATOR-ALERT-WIRE Step 5

**Date:** 2026-05-21
**Parent runbook:** `tasks/runbook_operator_alert_activation_2026_05_19.md`
**Backlog:** `BL-NEW-NARRATIVE-OPERATOR-ALERT-WIRE`
**Status:** `ENDPOINT-SHIPPED / HERMES-SKILL-PENDING` (verified 2026-05-21 — no progress since 2026-05-19)
**Operator-gated:** do not deploy unless explicitly approved.

## Why this addendum exists

The parent runbook describes Step 5's SKILL.md change at the **shape**
level ("the dispatcher must compute the canonical signature using the
same scheme as...") but stops short of providing copy-paste-able text
because the SKILL.md lives outside this repo. This addendum closes that
gap: the exact insertion shape, drop-in code blocks, and verification
queries the operator runs.

It does NOT include the secret value (per parent runbook secret-hygiene
rules: secret never appears in any committed text).

## Pre-flight (verified 2026-05-21 02:30Z)

Two-step SSH probe confirmed:

| Field | Current state |
|---|---|
| `OPERATOR_ALERT_HMAC_SECRET` in srilu `/root/gecko-alpha/.env` | absent (grep -c → 0) |
| `POST /api/internal/operator-alert` live response | 503 (feature-gated off) |
| Hermes SKILL.md `/home/gecko-agent/.hermes/skills/narrative_alert_dispatcher/SKILL.md` | unchanged since 2026-05-13 15:07Z; status=DRAFT; uses Path B (log-only on 503) |
| `operator_alert_dispatched` / `narrative_dispatcher_misconfig` log activity in last 24h | none |

No progress has happened since the 2026-05-19 runbook landed. The
endpoint code is deployed; the activation is purely operator-blocked.

## Step 5 — SKILL.md patch (the concrete edits)

The dispatcher SKILL.md currently has a `503` branch that logs
`narrative_dispatcher_misconfig` and halts. This addendum replaces that
branch with a Path C1 POST. Below is the exact diff shape.

### 5a. Add the Path C1 constants section near the top

After the existing `## Single-source constants` block in SKILL.md
(currently around line 35), insert:

````markdown
## Path C1 (operator-alert endpoint) constants

```python
# BL-NEW-NARRATIVE-OPERATOR-ALERT-WIRE: independent secret, NOT
# NARRATIVE_SCANNER_HMAC_SECRET, so the dispatcher can still raise an
# alert when the narrative endpoint is 503-gated.
OPERATOR_ALERT_PATH = "/api/internal/operator-alert"
OPERATOR_ALERT_MAX_RETRIES = 1   # 1 retry then fall back to Path B (log-only)
OPERATOR_ALERT_RETRY_DELAY_SEC = 10
```
````

### 5b. Add the `_post_operator_alert` helper

After the `_build_body` pseudocode (currently around line 102), insert:

````markdown
```python
def _post_operator_alert(message: str, source: str, operator_secret: str) -> bool:
    """Path C1: POST to /api/internal/operator-alert with HMAC headers.

    Same canonical-string scheme as the narrative endpoint (see
    scout/api/narrative.py:_compute_signature in gecko-alpha):
        canonical = f"{METHOD}\n{PATH}\n{QUERY}\n{X-Timestamp}\n".encode() + body
        signature = HMAC-SHA256(secret, canonical).hexdigest()

    Returns True iff a 2xx response is received (alert delivered).
    On failure, retries once with OPERATOR_ALERT_RETRY_DELAY_SEC backoff,
    then falls back to Path B (caller emits narrative_dispatcher_misconfig).
    """
    body = json.dumps(
        {"message": message, "source": source},
        separators=(",", ":"),
    ).encode()
    for attempt in range(OPERATOR_ALERT_MAX_RETRIES + 1):
        ts = str(int(time.time()))
        canonical = f"POST\n{OPERATOR_ALERT_PATH}\n\n{ts}\n".encode() + body
        sig = hmac.new(
            operator_secret.encode(), canonical, hashlib.sha256
        ).hexdigest()
        try:
            resp = post_with_headers(
                url=GECKO_ALPHA_BASE_URL + OPERATOR_ALERT_PATH,
                body=body,
                headers={
                    "Content-Type": "application/json",
                    "X-Timestamp": ts,
                    "X-Signature": sig,
                },
            )
        except Exception as exc:
            log(
                "operator_alert_transport_error",
                attempt=attempt,
                err=str(exc),
                err_type=type(exc).__name__,
            )
            if attempt < OPERATOR_ALERT_MAX_RETRIES:
                time.sleep(OPERATOR_ALERT_RETRY_DELAY_SEC)
                continue
            return False

        if 200 <= resp.status < 300:
            log(
                "operator_alert_delivered",
                attempt=attempt,
                http_status=resp.status,
                source=source,
            )
            return True

        log(
            "operator_alert_non_2xx",
            attempt=attempt,
            http_status=resp.status,
            response_body=resp.text[:200],  # bound size
        )
        if attempt < OPERATOR_ALERT_MAX_RETRIES:
            time.sleep(OPERATOR_ALERT_RETRY_DELAY_SEC)

    return False
```
````

### 5c. Replace the existing 503 branch

In `dispatch_v1_1`, find the existing branch (currently around line
172-201) that starts with `elif resp.status == 503:` and ends with
`break  # NO queue. Halt the entire cron tick.`

Replace the entire branch with:

````markdown
```python
        elif resp.status == 503:
            # 503 means gecko-alpha's NARRATIVE_SCANNER_HMAC_SECRET is
            # empty OR settings_init_failed. Halt loudly; do NOT queue
            # (silent-queue-and-retry would mask the misconfig).
            #
            # Path C1 (BL-NEW-NARRATIVE-OPERATOR-ALERT-WIRE wire-up,
            # 2026-05-21): the operator-alert endpoint uses an
            # INDEPENDENT secret so it can fire here even when the
            # narrative secret is broken. On Path C1 failure we fall
            # back to Path B (log-only) — operator-side discovery still
            # works via journalctl grep.

            ctx_message = (
                f"narrative_dispatcher_misconfig: gecko-alpha narrative "
                f"endpoint returned 503 (NARRATIVE_SCANNER_HMAC_SECRET "
                f"empty or settings_init_failed). batch_size={len(items)} "
                f"idx_at_halt={idx} event_id={event_id}"
            )
            log(
                "operator_alert_dispatched",
                source="narrative_dispatcher_misconfig",
                batch_size=len(items),
                idx_at_halt=idx,
                event_id=event_id,
            )
            delivered = _post_operator_alert(
                message=ctx_message,
                source="narrative_dispatcher_misconfig",
                operator_secret=OPERATOR_ALERT_HMAC_SECRET,
            )
            if not delivered:
                # Path C1 failure → fall back to Path B log-only so
                # operator can still discover via journalctl grep.
                log(
                    "operator_alert_failed",
                    source="narrative_dispatcher_misconfig",
                    fallback="path_b_log_only",
                    event_id=event_id,
                )
                log(
                    "narrative_dispatcher_misconfig",
                    reason="503_feature_off",
                    severity="critical",
                    event_id=event_id,
                    batch_size=len(items),
                    idx_at_halt=idx,
                )
            # NO queue regardless of Path C1 success — silent-queue
            # would mask the misconfig.
            break
```
````

### 5d. Update the "TODO before activation" section at the bottom

Add to the existing list (currently ends around line 280):

````markdown
- BL-NEW-NARRATIVE-OPERATOR-ALERT-WIRE Step 5 — Path C1 wired (this addendum).
  Verify `OPERATOR_ALERT_HMAC_SECRET` is set in `/home/gecko-agent/.hermes/.env`
  (or wherever the gecko-agent dispatcher sources its secrets) AND matches
  the value in `/root/gecko-alpha/.env`. Mismatch means HMAC 403 → fall back
  to Path B silently.
````

### 5e. Set the Hermes-side secret

The operator-alert secret must exist in TWO places with IDENTICAL values:

1. `/root/gecko-alpha/.env` (gecko-alpha side, verifies inbound HMAC)
2. `/home/gecko-agent/.hermes/.env` (gecko-agent side, signs outbound HMAC)

Use the same secret-hygiene pattern as parent runbook Step 2, applied
to the second file:

```bash
ssh -t srilu-vps '
sudo -u gecko-agent bash -c "
cd /home/gecko-agent/.hermes
cp .env .env.bak.pre-operator-alert-2026-05-21 2>/dev/null || touch .env.bak.pre-operator-alert-2026-05-21
read -rsp \"OPERATOR_ALERT_HMAC_SECRET (paste same value as gecko-alpha .env): \" OP_KEY
echo
umask 077
printf %s\\n \"\$OP_KEY\" > .op_secret.tmp
unset OP_KEY

awk -v new_secret_file=.op_secret.tmp '"'"'
BEGIN { getline new_value < new_secret_file; close(new_secret_file); found=0 }
/^OPERATOR_ALERT_HMAC_SECRET=/ { print \"OPERATOR_ALERT_HMAC_SECRET=\" new_value; found=1; next }
{ print }
END {
    if (!found) {
        print \"\"
        print \"# OPERATOR_ALERT_HMAC_SECRET (Hermes-side; matches gecko-alpha .env)\"
        print \"OPERATOR_ALERT_HMAC_SECRET=\" new_value
    }
}
'"'"' .env > .env.update.tmp && mv .env.update.tmp .env
rm -f .op_secret.tmp

grep -nE \"^OPERATOR_ALERT_HMAC_SECRET=\" .env | sed \"s/=.*/=<redacted>/\"
"
'
```

If the gecko-agent dispatcher reads secrets from a different file (per
how the Hermes runtime is configured), substitute that path. Common
alternatives:
- `/home/gecko-agent/.hermes/runtime.env`
- `/home/gecko-agent/.config/hermes/secrets.env`

Confirm by checking which file the existing `NARRATIVE_SCANNER_HMAC_SECRET`
lives in on the Hermes side — the operator-alert secret should live
adjacent.

## Step 6 verification — extra fixed-vector signature pin

To verify the dispatcher's signature math matches the gecko-alpha
endpoint, run this fixed-vector test from the gecko-agent dispatcher
runtime (NOT prod — use a sandbox copy of the dispatcher code):

```python
import hmac, hashlib

SECRET = "test_secret_do_not_use_in_prod"
BODY = b'{"message":"smoke","source":"runbook_2026_05_21"}'
TS = "1715600000"
CANONICAL = b"POST\n/api/internal/operator-alert\n\n1715600000\n" + BODY

sig = hmac.new(SECRET.encode(), CANONICAL, hashlib.sha256).hexdigest()
print(sig)
# Expected (computed locally): 50f6b6e62d11ee85e83bb33d10dd2d4d2ce58a4b85b2cfa6ae5d9e23a08c0bbb
# NOTE: actual gecko-alpha implementation in scout/api/narrative.py uses
# the parameterized _verify_hmac path; before relying on this exact hex,
# run scout/tests/test_internal_alert_api.py::test_hmac_fixed_vector
# in gecko-alpha and copy the value the test asserts.
```

This pinning catches the most common drift cause: dispatcher omits the
trailing `\n` after timestamp, or includes `?param=value` query string
in `CANONICAL` when no query exists, or uses `text/plain` body
serialization instead of compact JSON.

If `scout/tests/test_internal_alert_api.py` does NOT have a fixed-vector
test for the operator-alert endpoint (only for the narrative endpoint),
that's a follow-up. Filed below.

## Follow-up filed by this addendum

`BL-NEW-OPERATOR-ALERT-FIXED-VECTOR-TEST`: gecko-alpha should have a
fixed-vector HMAC test for `/api/internal/operator-alert` parallel to
the one for `/api/narrative-alert`. The current
`tests/test_internal_alert_api.py` covers happy-path delivery + secret
leakage + replay but not a canonical-string-format pinning. Cheap; adds
defensive coverage against future canonical-format drift.
Evidence-gated: ship when Step 5e activation completes and the
dispatcher's first real HMAC POST validates against the endpoint.

## Acceptance for full SHIPPED status flip

`BL-NEW-NARRATIVE-OPERATOR-ALERT-WIRE` flips from
`ENDPOINT-SHIPPED / HERMES-SKILL-PENDING` to full `SHIPPED` only when:

1. `OPERATOR_ALERT_HMAC_SECRET` set on srilu `/root/gecko-alpha/.env`
   (parent runbook Step 2).
2. Same secret set on Hermes-side dispatcher env (Step 5e of this addendum).
3. SKILL.md updated per Steps 5a-5d of this addendum.
4. gecko-pipeline + dispatcher both restarted.
5. Smoke test (parent runbook Step 6) shows
   `operator_alert_dispatched` → `operator_alert_delivered` log
   sequence on gecko-alpha AND a Telegram message arrives in the
   operator chat with body `narrative_dispatcher_misconfig: ...`.
6. Operator restores `NARRATIVE_SCANNER_HMAC_SECRET` per parent runbook
   Step 6e to return system to normal operation.

Backlog status flip + activation evidence → file as one more docs PR
once items 1-6 are confirmed.

## Rollback

If activation produces unwanted alert noise or proves to be a poor
design fit:

1. Set `OPERATOR_ALERT_HMAC_SECRET=` (empty) on `/root/gecko-alpha/.env`.
2. Restart gecko-pipeline. Endpoint reverts to 503.
3. Revert SKILL.md to Path B (log-only) by removing the inserted
   sections from 5a/5b/5c. Or simply leave the new code in place; with
   the endpoint 503-ing, `_post_operator_alert` will return False and
   fall back to Path B automatically.

Net blast-radius if rolled back: zero. The whole stack is additive.
