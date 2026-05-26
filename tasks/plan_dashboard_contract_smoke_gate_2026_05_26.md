**New primitives introduced:** `scripts/check_dashboard_contracts.py` aggregate dashboard contract-smoke runner; explicit CI dashboard-contract firewall step. No new DB table, no alerting primitive, no execution primitive, no ranking primitive.

# Dashboard Contract Smoke Gate Plan

## Hermes-First Analysis

| Domain | Hermes skill found? | Decision |
|---|---|---|
| Dashboard contract smoke orchestration | None found that knows gecko-alpha's `/api/live_candidates` and `/api/trade_inbox` contracts. | Build a tiny repo-local aggregator over existing checker scripts. |
| Post-deploy smoke automation | Hermes automation templates support deploy-triggered smoke workflows, but the repo still needs a concrete local command to run. | Build local command now; Hermes can call it later if desired. |
| Cross-identifier resolver | Not applicable. Runtime baseline found zero current true cross-id pairs. | Mark backlog as AUDITED-PHANTOM with re-audit trigger, no resolver build. |

Awesome-hermes-agent ecosystem check: no checked ecosystem primitive replaces these local dashboard contract checks over gecko-alpha API shapes. Hermes may schedule or call the command, but it should not own the contract logic.

## Runtime / Drift Findings

- PR #282/#283 shipped `scripts/check_trade_inbox_contract.py`.
- `scripts/check_live_candidates_contract.py` already exists.
- CI runs the full test suite, which includes both checker tests, but the workflow does not explicitly name the dashboard contract firewalls.
- Deploy smoke currently requires remembering two separate commands.
- `BL-NEW-CROSS-IDENTIFIER-RESOLVER-TRACKER-PAPER` is still PROPOSED even though runtime baseline on 2026-05-26 found zero true current cross-id candidate pairs.
- The contract firewall should be watched by CI, not memory: this branch must add a named GitHub Actions gate that runs the aggregate checker tests and both endpoint contract suites on every PR/push to `master`.
- Post-deploy smoke remains operator-run/deploy-script-run in this branch, but the aggregate command is the single stable command to log after deploy. No Telegram alerting is added.

## Scope

1. Add `scripts/check_dashboard_contracts.py`:
   - stdlib-only CLI;
   - calls both existing checker modules against a base dashboard URL;
   - emits combined text or JSON summary;
   - runs both checks every time, even if the first fails;
   - exits `0` only if both checks pass; otherwise exits with the first non-zero checker code in priority order `1` contract, `2` HTTP, `3` JSON, `4` config.
2. Add focused tests for the aggregate runner.
3. Add an explicit GitHub Actions step named `Dashboard contract firewalls` that runs the focused checker and endpoint tests:
   - `tests/test_check_dashboard_contracts.py`
   - `tests/test_check_live_candidates_contract.py`
   - `tests/test_live_candidates_endpoint.py`
   - `tests/test_check_trade_inbox_contract.py`
   - `tests/test_trade_inbox_endpoint.py`
4. Update `backlog.md`:
   - mark `BL-NEW-CROSS-IDENTIFIER-RESOLVER-TRACKER-PAPER` as `AUDITED-PHANTOM`;
   - evidence: zero current true cross-id pairs as of 2026-05-26 baseline;
   - re-audit trigger: same-symbol different-identifier candidate rate exceeds 3 per UTC day for two consecutive UTC days, or operator-visible duplicate examples are captured with paper/tracker identity evidence;
   - conditional guardrail: do not implement resolver until trigger fires and audit proves visible noise.
5. Update `tasks/lessons.md` with the anti-scope-as-runtime-contract pattern.
6. Add the future alert-design coupling note to the deferred alert backlog:
   - urgency tiers stay out of `/api/trade_inbox` until the tracker-promotion soak gate clears;
   - default future shape is a separate `/api/trade_alert_intent`-style endpoint, not relaxing the Trade Inbox contract.

## Non-Scope

- No Telegram alerts.
- No urgency tiers or alert qualification.
- No cross-id resolver behavior.
- No dashboard producer changes.
- No new pipeline writer table; AGENTS �12a table freshness watchdog does not apply. The equivalent anti-rot guard for this read-only firewall is the named CI gate plus post-deploy logged smoke command.

## Build Steps

- [ ] Add failing tests for `scripts/check_dashboard_contracts.py`.
- [ ] Implement the aggregate runner.
- [ ] Add explicit CI step.
- [ ] Update backlog, deferred alert guardrails, and lessons.
- [ ] Run focused contract tests and workflow-relevant tests.
- [ ] Open PR, get two review vectors, fold feedback, merge, deploy.

## Verification

- `uv run pytest -q tests/test_check_dashboard_contracts.py tests/test_check_live_candidates_contract.py tests/test_live_candidates_endpoint.py tests/test_check_trade_inbox_contract.py tests/test_trade_inbox_endpoint.py`
- CI `Dashboard contract firewalls` step passes.
- Post-deploy: `.venv/bin/python scripts/check_dashboard_contracts.py --url http://localhost:8000 --json` returns status `ok`.
