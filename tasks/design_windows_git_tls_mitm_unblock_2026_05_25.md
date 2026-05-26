**New primitives introduced:** Windows GitHub TLS MITM runbook and local CA-bundle helper script.

# Design - Windows GitHub TLS MITM Unblock - 2026-05-25

## Intent

Enable safe GitHub HTTPS Git operations from Windows when a local AV or
corporate proxy performs TLS inspection.

The design keeps TLS verification enabled and avoids persistent global Git
configuration changes.

## Hermes-First Analysis

| Domain | Hermes skill found? | Decision |
|---|---|---|
| Git/TLS MITM handling on Windows | none found | keep custom |
| Runbook and helper script | none found | keep custom |

Awesome-Hermes ecosystem verdict: this is local OS trust-store tooling; no
Hermes skill applies.

## Preferred Path: Schannel

Use the Windows trust store per command:

```powershell
git -c http.sslBackend=schannel fetch origin
```

If the MITM root is installed in Windows trusted roots, this should validate the
intercepted certificate without any custom CA file.

## Fallback Path: OpenSSL With Explicit Bundle

When `schannel` is unavailable or fails, use OpenSSL with an explicit combined
CA bundle:

1. Git-for-Windows default `ca-bundle.crt`.
2. The locally installed MITM root selected by thumbprint or subject regex.

Per-command invocation:

```powershell
git -c http.sslBackend=openssl -c http.sslCAInfo=<combined_bundle> fetch origin
```

## Helper Script

`scripts/windows/build_git_ca_bundle_for_tls_mitm.ps1`:

- Accepts `-Thumbprint <sha1>` or `-SubjectRegex <regex>`.
- Searches `Cert:\CurrentUser\Root`, `Cert:\LocalMachine\Root`,
  `Cert:\CurrentUser\CA`, and `Cert:\LocalMachine\CA`.
- Keeps only unexpired CA certificates that are self-issued roots.
- Fails closed on zero matches or multiple matches.
- Converts DER to PEM in pure PowerShell.
- Copies Git-for-Windows default CA bundle to `-OutFile`, then appends the MITM
  root PEM.
- Defaults `-OutFile` under `%TEMP%` so artifacts do not land in the repo.

## Safety Boundaries

- Do not set `http.sslVerify=false`.
- Do not set `GIT_SSL_NO_VERIFY=true`.
- Do not embed tokens in remote URLs.
- Do not write global Git configuration from the helper.
- Do not commit generated `.cer`, `.pem`, or `git_ca_bundle_plus_*.crt` files.

## Verification

- Parse helper script with PowerShell.
- Run no-match negative control and confirm it fails closed.
- Run `git -c http.sslBackend=schannel ls-remote
  https://github.com/Trivenidigital/gecko-alpha.git HEAD`.
- Confirm `git diff --check` is clean.

## Rollback

Revert files touched by this dev-hygiene PR. There is no production runtime
state to roll back.
