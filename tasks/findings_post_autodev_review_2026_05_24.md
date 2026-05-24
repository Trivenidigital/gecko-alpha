**New primitives introduced:** NONE

# Findings — post-autodev-fleet review 2026-05-24

Closes review items 1–5 from the operator-requested 2026-05-24 review of the
Codex+Hermes+Claude Code fleet installed on srilu-vps 2026-05-22→23.

VPS host: `ubuntu-4gb-hel1-1` (89.167.116.187). Review captured via the
`ssh > file && Read file` two-step pattern per global CLAUDE.md.

## Summary

| Item | Verdict | Action |
|---|---|---|
| #1 17:22Z service "failure" | working as designed | follow-up: scout SIGTERM handler bug (real) |
| #2 dist/index.html dirty | VPS-side benign, Windows-side known | no action |
| #3 codex 03:59Z INVALIDARGUMENT | real OAuth refresh-token race | follow-up: codex-invocation global lock |
| #4 operator-approvals .env | misread (not a queue item) | no action |
| #5 gh CLI not on PATH | by design | no action |

---

## #1 — gecko-pipeline + gecko-dashboard 17:22:36Z "failure"

**Verdict: working as designed. Real bug surfaced as follow-up.**

The failure-alert chain fired correctly. The proximate trigger was an
operator SSH session from 172.59.64.254 at 17:21:05Z that issued
`systemctl stop` on both services. systemd sent SIGTERM at 17:21:06Z, then
SIGKILLed at 17:22:36Z after the default 90s `TimeoutStopSec` elapsed
without the process exiting. `Restart=always` brought the services back up
within 10s. Auto-remediate audit confirms `repaired / unit active after restart`.

Same pattern across all 5 pipeline / 9 dashboard "failures" in past 48h —
each preceded by an operator SSH login + manual `systemctl stop`/`restart`.
Pattern existed before yesterday's autodev install (e.g. 2026-05-23 03:02:46Z
dashboard SIGKILL, same shape, pre-autodev). The autodev install added the
Telegram-notification surface, not the underlying cause.

**Real bug surfaced:** `scout/main.py` SIGTERM handler logs
`"Shutdown signal received"` at 17:21:06Z but does NOT actually trigger
shutdown — `perp_watcher_stats` event keeps firing at 17:21:51Z (45s after
SIGTERM), and the heartbeat loop continues until SIGKILL. The signal handler
needs to set a shutdown event that the main async loop checks. This is a
pre-existing bug, not caused by the autodev fleet.

**Follow-up:** file `BL-NEW-PIPELINE-SIGTERM-HANDLER` — make scout main loop
actually exit on SIGTERM within `TimeoutStopSec`. Until that ships, every
operator-initiated restart will fire a false Telegram alert.

---

## #2 — dashboard/frontend/dist/index.html "perpetually dirty"

**Verdict: VPS-side benign. Windows-side is a known pattern.**

VPS state at 2026-05-24 17:47Z: clean. `git ls-files --eol` shows
`i/lf w/lf attr/text eol=lf`. `.gitattributes` correctly declares
`dashboard/frontend/dist/** text eol=lf`. The on-disk bytes are LF.

CRLF warnings observed in autodev journal (`warning: in the working copy of
'dashboard/frontend/dist/index.html', CRLF will be replaced by LF the next
time Git touches it`) come from the **throwaway worktree** at
`/root/codex-autodev/srilu-gecko-alpha/runs/$NOW/work`, not the main repo.
The wrapper does `git -C "$WORKTREE" config core.autocrlf false` + `git
reset --hard` + `git clean -fd` before each Codex invocation. Even if the
file became dirty in a worktree, the wrapper's `DENY_PATH_RE` includes
`dashboard/frontend/dist` so any diff to that path blocks PR creation
(blocker: `operator_gated_path_changed`). No risk of dirty index.html
reaching a PR via this path.

The Windows-side dirty file the operator sees on local checkout is the
pattern already documented in
`memory/feedback_vite_dist_index_html_commit_discipline.md` — vite rewrites
the script src on every build; canonical version is in git; commit
discipline solves it. Out of scope for VPS-side review.

**No action.**

---

## #3 — codex-autonomous-dev 2026-05-24 03:59:36Z exit 2/INVALIDARGUMENT

**Verdict: real OAuth `refresh_token_reused` race. Self-healed at 04:45Z. Follow-up filed.**

`codex-events.log` for the failed run shows:

```
ERROR codex_login::auth::manager: Failed to refresh token: 401 Unauthorized:
{"error": {"message": "Your refresh token has already been used to generate
a new access token. Please try signing in again.",
"code": "refresh_token_reused"}}
```

Mechanism: the ChatGPT OAuth refresh_token is single-use — a successful
refresh rotates it; using the old copy returns 401 `refresh_token_reused`.
Two near-simultaneous codex invocations on the same host racing on
`~/.codex/auth.json` will see one succeed and the other fail. The 03:55:43Z
run (5s, no diff produced) had likely just rotated the token; the 03:59:32Z
run started with a stale snapshot.

**Self-healing:** `codex-auth-guard.timer` (8h cadence) refreshes the token
on its next fire; the 04:45:35Z autodev run completed normally with
`OAuth/ChatGPT exec smoke passed`. Recurrence rate: 1 in N runs (where N
is governed by inter-run spacing). At current cadence, expect rare
single-run failures, each correctly notified via the OnFailure → Telegram
chain.

**Mitigation options (not yet implemented):**

1. Serialize all codex consumers on this host via a shared
   `/run/codex.auth.lock` flock — wrap `codex-auth-guard`, the autodev
   wrapper's codex call, the readonly-operator-brief, and any other
   codex callsite. Cleanest fix.
2. Accept the rate as acceptable noise (already alerts via Telegram).
3. Bump the autodev `RestartSec` / inter-run minimum interval beyond
   `RestartSec=10s` to give auth-guard's smoke test more headroom.

**Follow-up:** file `BL-NEW-CODEX-AUTH-LOCK` — wrap all codex invocations
on srilu in a shared flock at `/run/codex.auth.lock`. Operator territory
(touches `/usr/local/bin/codex-auth-guard` + the autodev wrapper); not
applied autonomously in this review.

---

## #4 — tasks/operator-approvals/github-production-push-automation.env

**Verdict: misread by the review. Not a pending-approval queue.**

The file is the **active approval artifact** required by the autodev
wrapper at line 40 of `/usr/local/bin/codex-autonomous-dev-srilu`:

```bash
[ -f "$APPROVAL" ] || add_blocker "approval_missing"
EXPIRY="$(grep '^expiry_utc=' "$APPROVAL" | cut -d= -f2-)"
if [ "$(date -u -d "$EXPIRY" +%s)" -lt "$(date -u +%s)" ]; then
  add_blocker "approval_expired"
fi
```

Contents are operator-authored scope/prohibition/rollback fields;
`expiry_utc=2026-07-18T00:00:00Z` is valid for ~55 days from today. The
file ENABLES the wrapper, it doesn't queue work for the operator.

Directory name `operator-approvals/` is plural-noun "approvals" (records),
not imperative "approve-these" (queue).

**No action.** Renaming the dir to something less ambiguous (e.g.
`operator-approval-artifacts/`) is a possible future cleanup but
touches the wrapper's `$APPROVAL` path constant — out of scope.

---

## #5 — `gh` CLI not on root's PATH

**Verdict: by design. Wrapper uses direct GitHub REST.**

The autodev wrapper does NOT shell out to `gh`. It POSTs directly to
`https://api.github.com/repos/Trivenidigital/gecko-alpha/pulls` via curl,
with the token parsed from `/root/.git-credentials`:

```bash
TOKEN="$(sed -n 's#https://x-access-token:\([^@]*\)@github.com.*#\1#p' \
  /root/.git-credentials | head -n 1)"
curl -fsS -X POST -H "Authorization: Bearer $TOKEN" \
  -H "Accept: application/vnd.github+json" \
  -H "X-GitHub-Api-Version: 2022-11-28" \
  https://api.github.com/repos/Trivenidigital/gecko-alpha/pulls \
  -d @"$RUN_DIR/pr-payload.json"
```

This is intentional: fewer moving parts, no dependency on a third-party
CLI's auth model. The `pr-response.json` artifact in each successful run
dir confirms this path works (e.g. PR #240 from
`/root/codex-autodev/srilu-gecko-alpha/runs/20260524T044535Z/pr-response.json`).

**No action.** If an operator needs manual `gh pr create` from the VPS,
they would need to install gh themselves; otherwise direct curl matches
the wrapper's idiom.

---

## Follow-up backlog seeds (not filed in this PR)

1. **BL-NEW-PIPELINE-SIGTERM-HANDLER** — make `scout/main.py` actually
   shutdown on SIGTERM within `TimeoutStopSec`. Today the handler logs
   but the async main loop doesn't observe the signal event. Operator-
   initiated restart will keep firing false Telegram alerts until shipped.

2. **BL-NEW-CODEX-AUTH-LOCK** — serialize codex invocations on srilu via
   shared `/run/codex.auth.lock`. Mitigates `refresh_token_reused` race
   between auth-guard + autodev + readonly-operator-brief + any operator
   interactive session.

Filing is deferred to operator-led backlog triage; this doc is the source
evidence for both.

## Verification commands used

```bash
# pipeline failure root cause
ssh srilu-vps 'journalctl -u gecko-pipeline.service --since "2026-05-24 17:20" --no-pager | head -60' > .ssh_out.txt
# read .ssh_out.txt — observe SIGTERM at 17:21:06Z, SIGKILL at 17:22:36Z

# codex 03:59 RCA
ssh srilu-vps 'cat /root/codex-autodev/srilu-gecko-alpha/runs/20260524T035932Z/codex-events.log' > .ssh_out.txt
# read .ssh_out.txt — observe refresh_token_reused error chain

# wrapper PR-creation path
ssh srilu-vps 'cat /usr/local/bin/codex-autonomous-dev-srilu' > .ssh_out.txt
# read .ssh_out.txt — confirm curl, no gh dependency
```
