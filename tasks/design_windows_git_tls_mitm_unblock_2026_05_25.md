# Design - Windows GitHub TLS MITM unblock (schannel-first + CA bundle fallback) - 2026-05-25

## Intent

Enable `git fetch/push` to GitHub from the Codex Windows sandbox even when an AV/corp proxy performs TLS inspection and re-issues leaf certs from a locally installed MITM root (e.g., `Norton Web/Mail Shield Root`).

Keep this safe:
- do not disable TLS verification
- do not persist global git config changes
- do not write cert artifacts into the repo working tree

## Approach

### Path A (preferred): `schannel`

Use the Windows trust store via:

- `git -c http.sslBackend=schannel <cmd>`

Rationale: if the MITM root is installed in Windows’ trusted roots (typical), `schannel` will validate successfully without any custom CA files.

### Path B (fallback): OpenSSL + generated bundle

When `schannel` is unavailable or fails (rare in this sandbox), use OpenSSL with an explicit CA bundle that includes:

1. Git-for-Windows default `ca-bundle.crt`
2. the locally installed MITM root (selected by thumbprint or subject regex)

Per-command invocation:

- `git -c http.sslBackend=openssl -c http.sslCAInfo=<combined_bundle> <cmd>`

### Bundle generation

`scripts/windows/build_git_ca_bundle_for_tls_mitm.ps1`:

- Inputs:
  - `-Thumbprint <sha1>` (preferred) OR `-SubjectRegex <regex>`
  - optional `-OutFile <path>` (defaults to `$env:TEMP\\git_ca_bundle_plus_mitm_<timestamp>.crt`)
- Cert search:
  - probe `Cert:\CurrentUser\Root` and `Cert:\LocalMachine\Root`
  - fail closed if 0 matches or >1 matches
  - (for >1 matches) print thumbprints + subjects for operator selection
- PEM conversion:
  - avoid hard-coded OpenSSL path
  - emit PEM by base64 encoding the DER `RawData` with the standard header/footer
- Output:
  - copy Git’s default `ca-bundle.crt` to `OutFile`
  - append a newline + the PEM cert

## Runbook

`docs/runbooks/windows-git-github-tls-mitm.md` documents:

- symptom patterns
- safe-first schannel usage
- safe fallback bundle generation + per-command OpenSSL usage
- explicit “DO NOT” footguns:
  - `http.sslVerify=false`
  - `GIT_SSL_NO_VERIFY`
  - token-in-URL remotes

## Verification

We consider this shipped when all are true:

1. `git -c http.sslBackend=schannel ls-remote https://github.com/Trivenidigital/gecko-alpha.git HEAD` succeeds (if it does, stop here).
2. Negative control: `git -c http.sslBackend=openssl ls-remote ...` fails in the intercepted environment.
3. Generate bundle and retry: `git -c http.sslBackend=openssl -c http.sslCAInfo=<bundle> ls-remote ... HEAD` succeeds.
4. Realistic: `git -c http.sslBackend=openssl -c http.sslCAInfo=<bundle> fetch --dry-run origin` succeeds.
5. `git status` is clean; no cert artifacts are tracked.

## Rollback

Revert commits touching only:

- `docs/runbooks/windows-git-github-tls-mitm.md`
- `scripts/windows/build_git_ca_bundle_for_tls_mitm.ps1`
- `tasks/todo.md` bookkeeping lines

No runtime / prod impact.

## Hermes-first analysis

This is OS/local git TLS trust and repo bookkeeping, not a Hermes domain.

| Domain | Hermes skill found? | Decision |
|---|---|---|
| Git/TLS MITM handling (Windows) | none found | keep custom |
| Runbook + helper script | none found | keep custom |

Verdict: KEEP_CUSTOM (developer tooling only).
