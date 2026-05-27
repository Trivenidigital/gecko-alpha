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

## Status

Phase 1 is `STAGED-FOR-DEPLOY 2026-05-27`.

The VPS file has not been replaced in this phase. Phase 2 may deploy only the
reviewed `run-scanner-cycle.after.py` artifact with checksum
`A9B0746455458C950124E28C2FD65AFC07201968E24FD0F99AD585BC8E229580`, after PR
review approves the staged diff.
