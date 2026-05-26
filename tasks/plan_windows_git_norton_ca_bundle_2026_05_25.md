**New primitives introduced:** Windows GitHub TLS MITM runbook and local CA-bundle helper script.

# Plan - Windows GitHub TLS MITM Unblock - 2026-05-25

## Goal

Unblock GitHub `fetch`, `push`, and PR flows from the Codex Windows sandbox
when AV or corporate TLS inspection re-issues GitHub leaf certificates from a
locally trusted root, such as `Norton Web/Mail Shield Root`.

## Non-Goals

- No changes to Norton, AV, or proxy configuration.
- No secrets stored in repo config.
- No token-in-URL remotes.
- No `http.sslVerify=false` or `GIT_SSL_NO_VERIFY`.
- No product/runtime behavior changes.
- No stale `tasks/todo.md` rewrite from old PR #274; current master already
  has newer backlog state.

## Drift Check

- No in-tree runbook or helper currently covers Git-for-Windows under local TLS
  inspection.
- PR #274 contains the useful helper/runbook but is dirty against current
  master because its old `tasks/todo.md` bookkeeping conflicts with newer
  backlog work.
- This refresh keeps only the durable dev-hygiene pieces: `.gitignore`, the
  runbook, helper script, and normalized plan/design docs.

## Hermes-First Analysis

This is local Git/TLS trust tooling, not a Hermes runtime domain.

| Domain | Hermes skill found? | Decision |
|---|---|---|
| Git-for-Windows TLS intercept CA handling | none found | keep custom helper and runbook |
| Local certificate bundle generation | none found | keep custom PowerShell helper |

Awesome-Hermes ecosystem verdict: no relevant Hermes skill is expected for
OS-specific Git TLS trust bundling.

## Plan Steps

1. Add `docs/runbooks/windows-git-github-tls-mitm.md` with:
   - symptom pattern;
   - preferred `schannel` path using Windows trust store;
   - fallback OpenSSL CA bundle path;
   - explicit "DO NOT" guidance for TLS verification bypasses.
2. Add `scripts/windows/build_git_ca_bundle_for_tls_mitm.ps1` with:
   - `-Thumbprint` preferred selection;
   - `-SubjectRegex` fallback selection;
   - fail-closed behavior for zero or multiple matching roots;
   - pure PowerShell PEM output;
   - default output under `%TEMP%`.
3. Add `.gitignore` entries for local cert/bundle artifacts.
4. Verify:
   - PowerShell parses the helper script.
   - Negative no-match invocation fails closed.
   - `git -c http.sslBackend=schannel ls-remote ... HEAD` succeeds in this
     sandbox.
   - `git diff --check` is clean.

## Acceptance Criteria

- A fresh Windows sandbox can follow the runbook to fetch/push without disabling
  TLS verification.
- Generated certificate artifacts are ignored and not tracked.
