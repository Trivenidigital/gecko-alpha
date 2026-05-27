**New primitives introduced:** NONE

## Goal

Finish the eligible scanner hygiene backlog items without changing signal,
ranking, alert qualification, paper-trade, or live-trading behavior.

## Scope

Target file on srilu:

`/home/gecko-agent/run-scanner-cycle.py`

Backlog rows staged:

- `BL-NEW-SCANNER-EXISTING-EXCEPTION-BOUNDING`
- `BL-NEW-SCANNER-DATETIME-UTCNOW-DEPRECATION`
- `BL-NEW-SCANNER-PRINT-TO-LOG-CONSISTENCY`

## Runtime Baseline

Fetched current VPS script into:

`artifacts/scanner_hygiene_2026_05_27/run-scanner-cycle.before.py`

Remote stat and checksum before staging:

```text
1000 1000 gecko-agent gecko-agent 640 41948 /home/gecko-agent/run-scanner-cycle.py
ff66878f451eb781cd3ddb0d1e877b07cb637caef11d6e894be3407912a9786e  /home/gecko-agent/run-scanner-cycle.py
```

Local artifact checksums:

```text
run-scanner-cycle.before.py  FF66878F451EB781CD3DDB0D1E877B07CB637CAEF11D6E894BE3407912A9786E
run-scanner-cycle.after.py   A9B0746455458C950124E28C2FD65AFC07201968E24FD0F99AD585BC8E229580
run-scanner-cycle.diff       5F0C83E7D4AE44A0B4115611521CDE10A9B6986022CE5CB99FEA9D6FE6A0F3EF
```

## Pre-Edit Drift

The old backlog note said only `state.start_time` needed the `utcnow` swap.
The fetched runtime file has three same-class `datetime.utcnow()` sites:

- `state.start_time`
- `log(...)` timestamp prefix
- final-report duration calculation

All three were staged. Replacing only `state.start_time` would mix aware and
naive datetimes in the duration calculation.

Current pre-edit hygiene counts:

- `datetime.utcnow()` sites: 3
- targeted unbounded scanner exception logs: 6
- human-readable final-report raw `print(...)` sites: 13
- structured JSON `print(json.dumps(...))` emits: preserved
- `SCANNER_CYCLE:` wrapper-grep summary: preserved as raw colored `print(...)`

## Staged Candidate

Staged artifact:

`artifacts/scanner_hygiene_2026_05_27/run-scanner-cycle.after.py`

Diff:

`artifacts/scanner_hygiene_2026_05_27/run-scanner-cycle.diff`

Changes staged:

- Replaced all three scanner-local `datetime.utcnow()` calls with
  `datetime.now(timezone.utc)`.
- Bounded the six targeted scanner exception log sites with
  `type(e).__name__` and `str(e)[:120]`.
- Converted human-readable final-report `print(...)` calls to the existing
  `log(...)` wrapper, except the wrapper-dependent `SCANNER_CYCLE:` summary
  print.
- Preserved structured JSON report emits and wrapper-dependent `SCANNER_CYCLE:`
  summary content.

## Validation

Local syntax validation:

```text
python -m py_compile artifacts/scanner_hygiene_2026_05_27/run-scanner-cycle.after.py
exit 0
```

Pattern validation:

```text
rg -n "datetime\.utcnow|log\(f.*\{e\}" artifacts/scanner_hygiene_2026_05_27/run-scanner-cycle.after.py
no matches
```

Scoped final-report validation:

```text
violations= []
```

The scoped final-report checker ignores the intentionally preserved
`print(json.dumps(...))` structured cycle summary and the wrapper-dependent
`SCANNER_CYCLE:` summary print.

## Deploy Evidence

Phase 2 deployed the reviewed artifact to srilu on 2026-05-27.

Baseline pre-deploy:

```text
1000 1000 gecko-agent gecko-agent 640 41948 /home/gecko-agent/run-scanner-cycle.py
ff66878f451eb781cd3ddb0d1e877b07cb637caef11d6e894be3407912a9786e  /home/gecko-agent/run-scanner-cycle.py
```

Backup:

```text
backup=/home/gecko-agent/backups/run-scanner-cycle.py.20260527T010403Z
1000 1000 gecko-agent gecko-agent 640 41948 /home/gecko-agent/backups/run-scanner-cycle.py.20260527T010403Z
backup_sha=ff66878f451eb781cd3ddb0d1e877b07cb637caef11d6e894be3407912a9786e
```

Post-deploy validation:

```text
1000 1000 gecko-agent gecko-agent 640 42082 /home/gecko-agent/run-scanner-cycle.py
final_sha=a9b0746455458c950124e28c2fd65afc07201968e24fd0f99ad585bc8e229580
syntax-ok /home/gecko-agent/run-scanner-cycle.py
923:        print(f"{Colors.CYAN}{Colors.BOLD}{summary}{Colors.END}")
```

Negative post-deploy greps returned no matches for:

- `datetime\.utcnow(`
- `log(f.*{e}`
- `state.blockers.append(f.*{e}`
- `log("\n`
- `log(f"\n`

The candidate temp file was removed after install.

## Status

`SHIPPED 2026-05-27`.
