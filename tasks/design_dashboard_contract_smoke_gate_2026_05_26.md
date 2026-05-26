**New primitives introduced:** `scripts/check_dashboard_contracts.py` aggregate dashboard contract-smoke runner; explicit CI dashboard-contract firewall step. No new DB table, no alerting primitive, no execution primitive, no ranking primitive.

# Dashboard Contract Smoke Gate Design

## Hermes-First Analysis

| Domain | Hermes skill found? | Decision |
|---|---|---|
| Dashboard contract smoke orchestration | None found that understands gecko-alpha's `/api/live_candidates` and `/api/trade_inbox` contracts. | Build a repo-local aggregate command over existing checkers. |
| CI contract enforcement | No Hermes primitive should own GitHub Actions enforcement for this repo-local firewall. | Add an explicit workflow step. |
| Post-deploy smoke | Hermes can later schedule or invoke the command, but the command itself must live with the contract code. | Build local command; keep alerting out of scope. |

Awesome-hermes-agent ecosystem check: no checked ecosystem primitive replaces these endpoint-specific contract validators. Hermes can orchestrate the command later; this PR owns only the local smoke surface and CI gate.

## Contract Runner

Create `scripts/check_dashboard_contracts.py`.

### Imports

Use stdlib only. Import sibling checker scripts with `importlib.util.spec_from_file_location` so `scripts/` does not need to become a package. Load:

- `scripts/check_live_candidates_contract.py`
- `scripts/check_trade_inbox_contract.py`

Anchor paths relative to `Path(__file__).resolve().parent`, and register loaded modules in `sys.modules` so tests can monkeypatch the module objects predictably.

### CLI

Arguments:

- `--url`, default `http://localhost:8000`
- `--live-limit`, default `20`
- `--trade-limit-per-group`, default `10`
- `--window-hours`, default `36`
- `--timeout-sec`, default `10.0`
- `--json`
- `--verbose`

Config validation:

- `live_limit` in `[1, 50]`
- `trade_limit_per_group` in `[1, 100]`
- `window_hours` in `[6, 72]`
- range-validation failures return exit `4`; argparse type errors keep argparse's default exit behavior, matching the existing checker CLIs.

### Execution

Always run both checks, even if the first one fails.

Call live candidates:

```python
live.fetch_and_validate(
    args.url,
    timeout_sec=args.timeout_sec,
    slo_ms=3000,
    limit=args.live_limit,
    window_hours=args.window_hours,
)
```

Call Trade Inbox:

```python
trade.fetch_and_validate(
    args.url,
    timeout_sec=args.timeout_sec,
    limit_per_group=args.trade_limit_per_group,
    window_hours=args.window_hours,
)
```

### Exit Code

Return `0` only if both checkers return `0`.

If either checker fails, return the first present exit code in this priority order:

1. `1` contract critical
2. `2` HTTP
3. `3` JSON
4. `4` config

This preserves CI's binary pass/fail while keeping failure class meaningful.

### Output

JSON output shape:

```json
{
  "status": "ok",
  "exit_code": 0,
  "url": "http://localhost:8000",
  "checks": {
    "live_candidates": {
      "status": "ok",
      "exit_code": 0,
      "critical_count": 0,
      "warning_count": 0,
      "criticals": [],
      "warnings": [],
      "passed": 1
    },
    "trade_inbox": {
      "status": "ok",
      "exit_code": 0,
      "critical_count": 0,
      "warning_count": 0,
      "criticals": [],
      "warnings": [],
      "passed": 1
    }
  }
}
```

Text output:

- OK: `OK: dashboard contracts clean (live_candidates=0, trade_inbox=0)`
- Failure: `FAIL: dashboard contract smoke failed (exit N)`
- In `--verbose` or failure mode, print per-check criticals and warnings.

## CI Gate

Modify `.github/workflows/test.yml` to add a step after dependency install and before the full suite:

```yaml
- name: Dashboard contract firewalls
  timeout-minutes: 5
  run: >
    uv run pytest --tb=short -q
    tests/test_check_dashboard_contracts.py
    tests/test_check_live_candidates_contract.py
    tests/test_live_candidates_endpoint.py
    tests/test_check_trade_inbox_contract.py
    tests/test_trade_inbox_endpoint.py
  env:
    TELEGRAM_BOT_TOKEN: test
    TELEGRAM_CHAT_ID: test
    ANTHROPIC_API_KEY: test
```

This is the anti-rot mechanism for the read-only firewall. It is not a table-freshness watchdog because no new writer table ships in this branch.

## Tests

Create `tests/test_check_dashboard_contracts.py`.

Required tests:

1. `test_main_json_ok_when_both_checkers_pass`
   - monkeypatch both checker modules' `fetch_and_validate`;
   - call `main(["--url", "http://dash", "--json"])`;
   - assert exit `0`, status `ok`, and both check blocks present.
2. `test_main_runs_both_checks_even_when_live_fails`
   - live returns exit `1`, trade returns exit `0`;
   - assert both fakes were called and aggregate exit is `1`.
3. `test_exit_code_priority_prefers_contract_over_http`
   - live returns HTTP `2`, trade returns contract `1`;
   - assert aggregate exit `1`.
4. `test_argument_forwarding_uses_endpoint_defaults`
   - pass `--live-limit 7 --trade-limit-per-group 8 --window-hours 24 --timeout-sec 1.5`;
   - assert forwarded args match.
5. `test_config_validation_rejects_bad_limits`
   - invalid limits return `4` and do not call checkers.
6. `test_failure_json_preserves_per_check_details`
   - one checker returns criticals/warnings;
   - assert JSON includes those details under that check.
7. `test_failure_text_prints_per_check_details`
   - call without `--json`;
   - assert failure output includes check name and critical text.

Use simple fake result objects exposing `criticals`, `warnings`, `passed`, and `is_clean` as needed.

## Backlog And Lessons

Update `backlog.md`:

- Change `BL-NEW-CROSS-IDENTIFIER-RESOLVER-TRACKER-PAPER` status to `AUDITED-PHANTOM`.
- Demote the Track 1 index entry from buildable-next language to audited-phantom/gated language so future sessions do not pick it as immediate implementation work.
- Evidence: `0` current true cross-id candidate pairs as of the 2026-05-26 runtime baseline.
- Re-audit trigger: same-symbol different-identifier candidate rate exceeds `3` per UTC day for two consecutive UTC days, or operator captures at least `3` visible duplicate rows in one Trade Inbox review window with identity evidence.
- Conditional guardrail: do not implement a resolver until the trigger fires and the re-audit proves operator-visible noise. Prefer deterministic provider/contract mapping; do not build a symbol-only merge.

Add `BL-NEW-TG-ALERT-QUALIFICATION-DESIGN` if absent:

- Hard dependency: tracker-to-cockpit promotion shipped and soak gate cleared.
- Exact unlock metric: `>= 5` unique tracker-promoted `coin_id`s/day for `>= 3` mature UTC days, measured from `scripts/trade_inbox_tracker_promotion_soak.sql`, or the 14-day calendar backstop closes with an explicit low-volume decision.
- Anti-scope: urgency tiers remain out of `/api/trade_inbox`.
- Default future shape: put urgency/alert intent in a separate endpoint such as `/api/trade_alert_intent`; relax Trade Inbox firewall only via a deliberate contract PR with new invariants.

Update `tasks/lessons.md`:

- Rule: when a plan's anti-scope can be encoded as a contract, lint, checker, or CI gate, encode it. The plan explains why; the runtime/CI check enforces it.
- Example: no urgency tiers, alert qualification, ranking fields, or cross-id resolver behavior in `/api/trade_inbox` are enforced by contract checks, not just by prose.

Also create active memory note:

- `C:\Users\srini\.claude\projects\C--projects-gecko-alpha\memory\feedback_anti_scope_as_runtime_contract.md`
- Required content: anti-scope should be enforced by runtime or CI contracts when possible; examples include no urgency tiers, alert qualification, ranking fields, or cross-id resolver behavior in `/api/trade_inbox`; verify the `.claude` memory file exists before claiming persistence.

Do not claim a `MEMORY.md` index update unless that index exists in the active memory directory.

## Verification

Run:

```powershell
uv run pytest -q tests/test_check_dashboard_contracts.py tests/test_check_live_candidates_contract.py tests/test_live_candidates_endpoint.py tests/test_check_trade_inbox_contract.py tests/test_trade_inbox_endpoint.py
git diff --check
```

Post-deploy smoke:

```bash
cd /root/gecko-alpha && .venv/bin/python scripts/check_dashboard_contracts.py --url http://localhost:8000 --json
```
