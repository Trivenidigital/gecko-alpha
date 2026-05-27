**New primitives introduced:** NONE

## Hermes-first analysis

| Domain | Hermes skill found? | Decision |
|---|---|---|
| VPS Python script hygiene | none found for maintaining `/home/gecko-agent/run-scanner-cycle.py` | Stage a reviewed diff and deploy with backup/stat/syntax evidence. |
| Backlog status | none needed | Update repo Markdown as the durable record. |

Awesome-hermes-agent ecosystem check: no relevant skill for this VPS-local scanner script cleanup. Verdict: custom staged patch plus repo record is justified.

## Goal

Finish the three eligible scanner hygiene backlog items while keeping gated signal-quality work untouched:

- `BL-NEW-SCANNER-EXISTING-EXCEPTION-BOUNDING`
- `BL-NEW-SCANNER-DATETIME-UTCNOW-DEPRECATION`
- `BL-NEW-SCANNER-PRINT-TO-LOG-CONSISTENCY`

## Two-Phase Staging Rule

This is not an emergency hotfix.

Phase 1 stages the candidate scanner diff for review. The VPS file is not replaced in Phase 1. Backlog rows may only move to `STAGED-FOR-DEPLOY`.

Phase 2 runs only after PR review approves the staged diff. It deploys the exact reviewed artifact to the VPS with checksum pinning, then adds final deploy evidence and flips backlog rows to `SHIPPED`.

## Exact Target

Remote file:

`/home/gecko-agent/run-scanner-cycle.py`

Repo artifacts:

- `artifacts/scanner_hygiene_2026_05_27/run-scanner-cycle.before.py`
- `artifacts/scanner_hygiene_2026_05_27/run-scanner-cycle.after.py`
- `artifacts/scanner_hygiene_2026_05_27/run-scanner-cycle.diff`
- `tasks/findings_scanner_hygiene_2026_05_27.md`
- `backlog.md`
- `tasks/todo.md`

## Candidate Edit Rules

Before editing, enumerate the current match counts and line numbers from `run-scanner-cycle.before.py`:

- unbounded scanner exception sites targeted by `BL-NEW-SCANNER-EXISTING-EXCEPTION-BOUNDING`;
- `datetime.utcnow()` sites targeted by `BL-NEW-SCANNER-DATETIME-UTCNOW-DEPRECATION`;
- final-report raw `print(...)` sites targeted by `BL-NEW-SCANNER-PRINT-TO-LOG-CONSISTENCY`.

If counts or sections differ from backlog assumptions outside the same scanner hygiene class, stop and produce findings/status only. If drift is only additional same-class sites in `/home/gecko-agent/run-scanner-cycle.py`, document the drift in findings and patch the complete same-class set so the staged artifact is internally consistent.

1. Exception bounding:
   - Replace existing unbounded `log(f"... {e}")` sites in the scanner with the already-used bounded pattern:
     `log(f"... {type(e).__name__}: {str(e)[:120]}")`.
   - Do not alter exception control flow.
   - Do not touch unrelated `traceback.format_exc()` or intentionally verbose diagnostic outputs unless they are part of the six scanner backlog sites.

2. UTC cleanup:
   - Runtime drift found three scanner-local `datetime.utcnow()` sites, not the single site named by the original backlog note: `state.start_time`, `log(...)` timestamping, and final-report duration.
   - Replace all three with `datetime.now(timezone.utc)` in the scanner. Replacing only `state.start_time` would mix aware and naive datetimes in the duration calculation.
   - Confirm `timezone` is already imported before changing.

3. Final-report print/log consistency:
   - Convert only the final-report raw `print(...)` sites to the existing `log(...)` wrapper.
   - Preserve message text as closely as possible.
   - Do not change JSON summary emits or wrapper behavior.
   - Keep the `SCANNER_CYCLE:` summary line as raw `print(...)` because the wrapper greps and forwards that line; adding the `log(...)` timestamp prefix would change the operator-facing summary body.

## Validation Before PR

Local/staged validation:

- `python -m py_compile artifacts/scanner_hygiene_2026_05_27/run-scanner-cycle.after.py`
- `sha256sum` or local Python SHA256 for both staged artifacts:
  - `run-scanner-cycle.before.py`;
  - `run-scanner-cycle.after.py`.
- grep staged diff for:
  - no `datetime.utcnow()`:
    `rg -n "datetime\.utcnow\(" artifacts/scanner_hygiene_2026_05_27/run-scanner-cycle.after.py`
  - no targeted unbounded `log(f"... {e}")` sites:
    `rg -n "log\(f.*\{e\}" artifacts/scanner_hygiene_2026_05_27/run-scanner-cycle.after.py`
  - no raw `print(` in the human-readable final-report block except the intentionally preserved structured JSON emit and wrapper-dependent `SCANNER_CYCLE:` summary print:
    run a scoped scanner from `log("FINAL REPORT"` through the final summary separator, ignoring those two allowed emits.

Remote validation before deploy:

Use two-step SSH only:

```powershell
ssh root@srilu-vps 'command' > .ssh_out.txt 2>&1
Get-Content .ssh_out.txt
```

Required remote checks:

1. Pre-stat and checksum:
   - `stat -c '%u %g %U %G %a %s %n' /home/gecko-agent/run-scanner-cycle.py`
   - `sha256sum /home/gecko-agent/run-scanner-cycle.py`
   - hard stop unless remote checksum equals the reviewed `run-scanner-cycle.before.py` checksum.
2. Upload candidate to same filesystem temp path:
   - `/home/gecko-agent/run-scanner-cycle.py.codex-20260527`
   - hard stop unless uploaded candidate checksum equals the reviewed `run-scanner-cycle.after.py` checksum.
3. Pre-replace syntax:
   - run no-side-effect syntax validation with the VPS Python interpreter:
     `python3 - <<'PY'` and `compile(open(path, encoding='utf-8').read(), path, 'exec')`.
4. Backup:
   - `/home/gecko-agent/backups/run-scanner-cycle.py.20260527T<HHMMSS>Z`
   - record `stat` and `sha256sum` for backup.
   - hard stop unless backup checksum equals original checksum.
5. Replace while preserving owner/group/mode:
   - check no active scanner process is running before replace.
   - use captured numeric uid, gid, and mode with `install -o <uid> -g <gid> -m <mode> <candidate> /home/gecko-agent/run-scanner-cycle.py`.
   - This plain Python script is assumed to have no load-bearing ACL/xattr/capability metadata; if `getfacl` or xattr checks show non-default metadata, stop and document findings.
6. Post-replace checks:
   - `stat` matches owner/group/mode;
   - target checksum equals reviewed `run-scanner-cycle.after.py` checksum;
   - run no-side-effect syntax validation against `/home/gecko-agent/run-scanner-cycle.py`;
   - grep confirms the three hygiene patterns are gone in scoped areas:
     `grep -n 'datetime\.utcnow(' /home/gecko-agent/run-scanner-cycle.py`;
     `grep -n 'log(f.*{e}' /home/gecko-agent/run-scanner-cycle.py`;
     a scoped final-report `print(` check that ignores the structured JSON report emit.

Rollback command, if validation fails after replace:

```bash
install -o <owner> -g <group> -m <mode> <backup_path> /home/gecko-agent/run-scanner-cycle.py
# then verify: target sha256 == backup sha256, stat matches original owner/group/mode,
# and no-side-effect compile() syntax validation passes.
```

If rollback occurs, do not mark backlog items as shipped. Record rollback findings instead.

## Repo Status Updates

Phase 1 status updates:

- `BL-NEW-SCANNER-EXISTING-EXCEPTION-BOUNDING`: `STAGED-FOR-DEPLOY 2026-05-27`.
- `BL-NEW-SCANNER-DATETIME-UTCNOW-DEPRECATION`: `STAGED-FOR-DEPLOY 2026-05-27`.
- `BL-NEW-SCANNER-PRINT-TO-LOG-CONSISTENCY`: `STAGED-FOR-DEPLOY 2026-05-27`.
- `tasks/todo.md`: add a short active-work record for the scanner hygiene sweep.

After successful Phase 2 deploy, update:

- `BL-NEW-SCANNER-EXISTING-EXCEPTION-BOUNDING`: `SHIPPED 2026-05-27`.
- `BL-NEW-SCANNER-DATETIME-UTCNOW-DEPRECATION`: `SHIPPED 2026-05-27`.
- `BL-NEW-SCANNER-PRINT-TO-LOG-CONSISTENCY`: `SHIPPED 2026-05-27`.
- `tasks/findings_scanner_hygiene_2026_05_27.md`: add deploy timestamp, pre/post sha256, backup path, stat before/after, syntax-check output, and rollback path.

## Backlog Rows Not To Touch

- `BL-NEW-TG-ALERT-QUALIFICATION-DESIGN`: remains gated until tracker-promotion soak has three mature UTC days.
- `BL-NEW-CROSS-IDENTIFIER-RESOLVER-TRACKER-PAPER`: remains `AUDITED-PHANTOM`.
- Source/KOL ranking and source pruning rows: remain price-coverage gated.
- `BL-NEW-FIRST-SIGNAL-RETIREMENT-DECISION`: remains operator-gated until 2026-05-31.
- Actionability downstream rows: eligible only for separate stale-PR/current-base triage, not implementation here.
- `tasks/todo.md` changes are limited to scanner-hygiene sweep status only.

## Anti-Scope

- No wrapper script changes.
- No Hermes job config changes.
- No Telegram alert qualification.
- No actionability downstream implementation.
- No signal, scoring, ranking, dispatch, source-quality, paper-trade, or live-trading behavior changes.
