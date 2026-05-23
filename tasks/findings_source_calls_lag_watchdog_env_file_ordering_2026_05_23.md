# Findings — source-calls lag watchdog `--env-file` ordering — 2026-05-23

**New primitives introduced:** NONE (findings-only)

## Finding

`scripts/source-calls-lag-watchdog.sh` sources `.env` before parsing CLI args, so passing `--env-file <path>` cannot affect the sourced env file for that invocation.

## Evidence

- In `scripts/source-calls-lag-watchdog.sh`, `ENV_FILE=...` is set and sourced before the `while [[ $# -gt 0 ]]` arg parser handles `--env-file`.
- This makes the `--env-file` flag a no-op for env sourcing (it only changes the `ENV_FILE` variable after sourcing has already happened).

## Impact

- Any operator/cron invocation that intends to point the watchdog at an alternate env file via `--env-file` will not take effect.
- This can silently mask misconfiguration because the script still runs and may still send alerts, but based on the wrong `.env`.

## Suggested fix (safe shape)

Parse `--env-file` first (or in a pre-pass), then source that env file, then parse the remaining args.

## Operator-only / runtime-state notes

- This is a production script; verify the deployed cron/systemd invocation patterns before changing behavior.
- Any change should preserve the existing “source early” behavior for `WRITER_HEARTBEAT_FILE` / Telegram creds and the DB_PATH clobber avoidance notes in the script header.

