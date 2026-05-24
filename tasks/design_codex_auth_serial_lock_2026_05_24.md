**New primitives introduced:** `/usr/local/bin/codex-locked` shim (small bash wrapper), `/run/codex.auth.lock` shared flock file, `scripts/install_codex_auth_lock.sh` (operator-runnable installer).

# Design — codex serial lock (BL-NEW-CODEX-AUTH-LOCK)

Surfaced by PR #242 §Follow-up #2 / PR #243 closeout. Empirical
evidence: srilu-vps 2026-05-24T03:59:36Z autodev exited
2/INVALIDARGUMENT with `401 refresh_token_reused`; self-healed at
04:45Z. Recurrence rate is low but the race is real.

## Mechanism

The ChatGPT OAuth `refresh_token` stored in `~/.codex/auth.json` is
single-use. Each successful refresh rotates it; using the old copy
returns `401 refresh_token_reused`. Codex CLI invocations on srilu come
from at least three sources:

1. `codex-auth-guard.timer` (every 8 h) — runs `codex login status` +
   `codex exec --ephemeral` as smoke.
2. `codex-autonomous-dev-srilu.service` — ExecStartPre runs
   `codex-auth-guard`, ExecStart runs `codex exec --sandbox
   workspace-write`.
3. `codex-readonly-operator-brief.timer` (daily 06:19) — separate
   codex invocation.

Plus ad-hoc operator interactive sessions. Any two of these reading
`auth.json` near-simultaneously can race the refresh — the loser sees
`refresh_token_reused`.

## Proposed fix

Wrap every codex CLI invocation on the host in a shared flock at
`/run/codex.auth.lock` so they serialise.

```bash
# /usr/local/bin/codex-locked
exec flock --wait 60 /run/codex.auth.lock /usr/bin/codex "$@"
```

Then change the two long-running scripts to call `codex-locked`
instead of bare `codex`:

- `/usr/local/bin/codex-auth-guard` (3 codex call sites)
- `/usr/local/bin/codex-autonomous-dev-srilu` (1 codex call site at
  the long autodev exec)

`codex-readonly-operator-brief` (path tbd at runtime) likewise.

## Why a shim instead of patching wrappers in-place

The wrappers are operator-installed via separate install scripts. A
sed-edit on prod scripts is fragile if the operator later updates them
out-of-band. A shim is one line, idempotent, and trivially
verifiable: `which codex-locked && cat $(which codex-locked)`.

The remaining hand-edit step (s/codex /codex-locked / on the wrappers)
is left to operator with `install_codex_auth_lock.sh` printing the
diff plan rather than performing the edit. Operator visibility of
what got wrapped is more important than full automation here.

## Why not OS-level locking?

systemd `Conflicts=` doesn't extend across services from different
units. Doing a per-PID flock on `auth.json` directly via the codex CLI
would require upstream changes. A user-space flock around the entire
codex invocation is the simplest path and survives codex upgrades.

## Cost of the lock

- `codex-auth-guard` smoke takes ~5 s; serialising 4 codex consumers
  worst-case adds ~15 s to one of them per overlap. Acceptable.
- The long autodev exec can hold the lock for up to `MAX_SECONDS=3600`.
  This intentionally blocks operator-interactive codex during a run
  — exactly the property that prevents `refresh_token_reused`. The
  60 s `flock --wait 60` gives the auth-guard timer headroom to
  detect overlap and report meaningful error rather than crash.

## Verification plan after operator applies

1. After install + edit, capture a baseline:
   `flock --wait 0 /run/codex.auth.lock true` should exit 0.
2. Watch `journalctl -u codex-autonomous-dev-srilu --since "1 day ago"
   | grep refresh_token_reused` for one full week. Goal: zero hits.
3. Confirm no service activation hangs: `systemctl list-units --state=activating`.
4. Rollback path: `cp /usr/local/bin/<name>.bak-<TS> <name>`; remove
   `/usr/local/bin/codex-locked`; revert hand-edits.

## Risk register

- **R1 — flock deadlock if codex itself shells out to another codex**:
  no known case; flock is advisory and a single process can re-acquire
  via `-x`. Mitigation: explicit `--wait 60` floor on the shim, never
  hold without timeout.
- **R2 — operator forgets step 2 (the hand-edit)**: installer prints a
  WARN until both target scripts route through `codex-locked`.
  Verification step #2 above also catches this — a still-unwrapped
  consumer keeps producing `refresh_token_reused`.
- **R3 — `/run` cleared on boot**: flock auto-creates the file on
  first acquire; installer also pre-creates it with `install -m 0600`
  for visibility.

## Why this is shipped operator-applied, not auto-applied

The autonomous-dev wrapper allowlist (`ALLOW_PATH_RE`) does not
include `/usr/local/bin/*`. Even if it did, sed-editing production
scripts owned by the operator from a CI-equivalent autodev surface
is exactly the class of action that should require a human in the
loop. Installer is shipped as content under `scripts/`; operator
runs it manually on the VPS once they've reviewed the diff plan.
