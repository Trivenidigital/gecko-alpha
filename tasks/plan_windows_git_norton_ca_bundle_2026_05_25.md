# Plan - Windows GitHub TLS MITM (Norton/corp proxy) unblock + todo drift fix - 2026-05-25

## Goal

Unblock GitHub `fetch/push/PR` flows from the Codex Windows sandbox when an AV/corp TLS inspection layer MITMs HTTPS (e.g., leaf certs issued from `Norton Web/Mail Shield Root`). Also remove drift in `tasks/todo.md` where already-merged cockpit work still shows as “PR-READY / blocked”.

## Non-goals

- No changes to Norton/AV configuration.
- No secrets stored in repo config (no tokens in remotes, no credential files checked in).
- No product behavior changes to Gecko dashboards/endpoints beyond bookkeeping in `tasks/todo.md`.

## New primitives introduced

- `docs/runbooks/windows-git-github-tls-mitm.md` — runbook: schannel-first; CA-bundle fallback for intercepted TLS chains.
- `scripts/windows/build_git_ca_bundle_for_tls_mitm.ps1` — builds a combined CA bundle that includes Git-for-Windows’ default bundle plus a locally trusted MITM root cert (subject-regex or thumbprint driven).

## Drift-check (repo state)

- No existing runbook or helper for TLS-intercepting AV roots (grep for `sslBackend=openssl`, `sslCAInfo`, `Norton Web/Mail Shield Root` returned none).
- `tasks/todo.md` has two stale “Active Work” entries that are already merged on `origin/master` (verified after unblocking `git fetch`):
  - Trade Opportunity Inbox — merged (PR #273).
  - Live candidates determinism + contract delta — merged (PR #270).

## Hermes-first analysis

This is local dev tooling (git TLS trust) + repo bookkeeping; it is not a Hermes domain.

| Domain | Hermes skill found? | Decision |
|---|---|---|
| Git-for-Windows TLS intercept CA handling | none found | keep custom (tiny PowerShell helper + runbook) |
| Gecko backlog bookkeeping (`tasks/todo.md`) | none found | keep custom |

Checked: Hermes skill hub at `https://hermes-agent.nousresearch.com/docs/skills` (domains: git, TLS, certificates, Windows dev tooling) + awesome-hermes-agent. Verdict: no relevant skill expected for OS-specific git TLS trust bundling; proceed with KEEP_CUSTOM.

## Plan steps

1. Add `docs/runbooks/windows-git-github-tls-mitm.md` with:
   - Symptom: `schannel` errors or OpenSSL issuer failures when AV MITM is enabled.
   - Safe default: use `schannel` first (Windows trust store).
   - Fallback: use OpenSSL with a generated CA bundle *per command*, without touching global gitconfig.
   - A clear “DO NOT” box: `http.sslVerify=false`, `GIT_SSL_NO_VERIFY`, token-in-URL remotes.
2. Add `scripts/windows/build_git_ca_bundle_for_tls_mitm.ps1`:
   - Accept `-Thumbprint` (preferred) or `-SubjectRegex`.
   - Search both `Cert:\CurrentUser\Root` and `Cert:\LocalMachine\Root`.
   - Fail closed if 0 or multiple matches; print candidate thumbprints to resolve ambiguity.
   - Export PEM in pure PowerShell (no hard-coded OpenSSL path).
   - Default output under `$env:TEMP` so artifacts never land in the repo working tree.
3. Update `tasks/todo.md`:
   - Mark Trade Opportunity Inbox and Live candidates determinism entries as shipped/merged (PR #273 / #270).
   - Remove the stale “blocked: GitHub creds unavailable” note.
4. Verification:
   - Positive (schannel): `git -c http.sslBackend=schannel ls-remote https://github.com/Trivenidigital/gecko-alpha.git HEAD`.
   - Negative (openssl without CA): `git -c http.sslBackend=openssl ls-remote ...` fails in the MITM environment.
   - Positive (openssl with CA): `git -c http.sslBackend=openssl -c http.sslCAInfo=<combined> ls-remote ... HEAD` returns cleanly.
   - Positive (real workflow): `git -c http.sslBackend=openssl -c http.sslCAInfo=<combined> fetch --dry-run origin` succeeds.
   - `git status` clean and no secrets added to tracked files.

## Acceptance criteria

- A fresh sandbox can run GitHub `git fetch` successfully by following the runbook.
- `tasks/todo.md` no longer claims PR #270/#273 work is blocked or pending.
