# Windows runbook - GitHub HTTPS fails under TLS inspection (Norton / corp proxy)

If GitHub HTTPS is intercepted by an AV / proxy that re-issues leaf certs from a local MITM root (example: `Norton Web/Mail Shield Root`), Git-for-Windows may fail `git fetch/push` with TLS errors.

This runbook gives a safe fix that keeps TLS verification enabled.

## DO NOT

- Do not set `http.sslVerify=false`.
- Do not set `GIT_SSL_NO_VERIFY=true`.
- Do not embed tokens in remote URLs (avoid `https://<token>@github.com/...`).

## Option A (preferred): use Windows trust store (`schannel`)

If the MITM root is installed in the Windows certificate store (typical), this is the cleanest option:

```powershell
git -c http.sslBackend=schannel ls-remote https://github.com/Trivenidigital/gecko-alpha.git HEAD
git -c http.sslBackend=schannel fetch origin
```

If these work, stop here.

## Option B (fallback): OpenSSL + explicit CA bundle

If `schannel` fails in your environment, generate a combined CA bundle that includes Git-for-Windows’ default CA bundle plus the MITM root cert.

### 1) Identify the MITM root thumbprint

Example for Norton:

```powershell
certutil -store Root "Norton Web/Mail Shield Root"
```

Copy the `Cert Hash(sha1)` thumbprint.

Note: if the cert is only in the current user store, use:

```powershell
certutil -user -store Root "Norton Web/Mail Shield Root"
```

### 2) Build a combined CA bundle

```powershell
$bundle = powershell -NoProfile -ExecutionPolicy Bypass -File scripts/windows/build_git_ca_bundle_for_tls_mitm.ps1 -Thumbprint <PASTE_THUMBPRINT>
echo $bundle
```

This writes the bundle under `%TEMP%` by default.

### 3) Use OpenSSL with the bundle (per command)

```powershell
git -c http.sslBackend=openssl -c http.sslCAInfo="$bundle" ls-remote https://github.com/Trivenidigital/gecko-alpha.git HEAD
git -c http.sslBackend=openssl -c http.sslCAInfo="$bundle" fetch origin
git -c http.sslBackend=openssl -c http.sslCAInfo="$bundle" push -u origin <your-branch>
```

## Troubleshooting

- If you see multiple matches from the script, re-run with the exact `-Thumbprint` you want.
- If you don’t know the thumbprint, you can use `-SubjectRegex` (more error-prone than thumbprint).
- If `schannel` fails with revocation-check errors, retry *per command* with: `-c http.schannelCheckRevoke=false` (security downgrade; use only when the error explicitly mentions revocation checking).
- Use the generated bundle *per command*; do not set it globally.
