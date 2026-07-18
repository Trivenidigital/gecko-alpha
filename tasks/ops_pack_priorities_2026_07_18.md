# Ops pack — priority closeout 2026-07-18

Session deliverable for the five priorities ranked from the 2026-07-17 state
(CG outage + #465/#466/#464 + inert flags). Structure per LOOPS.md: this file
is STATE + TRACE for the effort; per-PR approvals are recorded in the
approvals log at the bottom.

**Boundary note (Approvals Discipline):** Priorities 1, 2, and 5 are
prod-state changes — operator-only. This pack gives ready-to-paste command
packs; the agent session did NOT execute them. Priorities 3 and 4 (merges)
were executed only against recorded per-PR approvals (see log).

---

## Priority 1 — restore CoinGecko ingestion (operator, prod)

**State:** CG Demo key quota exhausted ~2026-07-14 01:53Z. All CG lanes dark
since 2026-07-13 16:12Z (`trending_snapshots` writer). 1,559 backoff events,
zero alerts (gap now closed by #465, see Priority 2).

**Decision needed first — pick one:**
- **(a) Wait for monthly quota reset** on the existing Demo key (Demo tier
  resets on the key's monthly cycle). Cheapest, but the pipeline stays dark
  until reset. Check the reset date in the CG developer dashboard
  (coingecko.com → account → developer dashboard → usage).
- **(b) Rotate to a fresh Demo key** (new key, same tier). Follow
  `tasks/runbook_cg_demo_api_key_2026_05_18.md` Steps 1–5 verbatim — it
  covers no-echo key entry, `.env` backup, restart, and the 2h validation
  window. Note the runbook's hygiene rules: never echo the key; backup
  `.env` first.
- **(c) Upgrade tier (paid)** if monthly exhaustion is now structural: the
  quota died mid-month, so at current call volume options (a)/(b) buy at
  most a month. Related open decision:
  BL-NEW-SOURCE-CALL-PRICE-COVERAGE-SAMPLE-CG-PRO in `backlog.md`.

**Recommendation:** (b) now to restore ingestion today, plus a standing
look at call volume before the new key's month rolls over. The #465
watchdog (Priority 2) pages within 2–3h if it goes dark again.

**Verify restoration (SSH to prod):**

```bash
# Key present + pipeline restarted per runbook, then after ~15 min:
sqlite3 /root/gecko-alpha/scout.db \
  "SELECT MAX(snapshot_at) FROM trending_snapshots;"   # should be minutes old
journalctl -u gecko-pipeline --since "15 minutes ago" --no-pager \
  | grep -c cg_429_backoff                             # should be 0
journalctl -u gecko-pipeline --since "15 minutes ago" --no-pager \
  | grep -c coingecko_lanes_stopped_for_backoff        # should be 0
```

---

## Priority 2 — activate the CG-ingestion watchdog (#465, operator, prod)

Merged 2026-07-17 but deploy-without-activate: inert until the managed cron
block is installed. The cron line already carries the inline
`CG_INGESTION_WATCHDOG_ENABLED=true` gate, so **activation = pull + deploy.sh**:

```bash
ssh root@srilu-vps '
cd /root/gecko-alpha
git pull origin master                       # picks up #465 (749882e)
bash cron/deploy.sh                          # idempotent managed-block merge
crontab -l | grep cg-ingestion-watchdog      # expect the 40 * * * * line
# Smoke test — with CG still dark this SHOULD report both checks breached:
.venv/bin/python scripts/cg_ingestion_watchdog.py --db scout.db --enabled true --dry-run
'
```

Notes:
- If activated BEFORE Priority 1 is fixed, the first real run (:40 past the
  hour) pages once per check (24h cooldown bounds it to one page/check/day).
  That page is correct — it is the outage alarm working.
- SLO knobs if needed: `TRENDING_SNAPSHOT_STALENESS_ALERT_HOURS` (3h) /
  `CG_OUTAGE_ALERT_HOURS` (2h) in `.env`.
- Caution: `cron/deploy.sh` installs the ENTIRE managed block, which also
  contains the liquidity-enrichment writer + watchdog lines (see Priority 5
  item 3). If `LIQUIDITY_ENRICHMENT_ENABLED` is not set in prod `.env`, the
  writer no-ops but its lag watchdog will (correctly) alert
  writer-heartbeat staleness per
  `tasks/runbook_enable_liquidity_enrichment_2026_06_23.md` — flip that
  flag first or expect/silence that page.

---

## Priority 3 — PR #466 review brief (ALR-02 detection quality gate)

**Verdict: APPROVE-READY. CI green; full suite green in this session's
Linux env (the aiohttp paths the Windows dev host could not run).**

- Diff: `scout/config.py` +1 Settings field
  (`DETECTION_ALERT_MIN_QUANT_SCORE`, default 1, ge=0 le=100);
  `scout/trading/detection_alert.py` gate helper + score-desc ordering +
  `detection_alert_funnel` log; 11 new tests.
- Evidence basis checked out: gate applied before cap; universe / trigger /
  dedup / cap safeguards untouched; `None` score reads as 0 (blocked);
  rollback = `MIN_QUANT_SCORE=0`, one `.env` line, no migration.
- House-rule conformance: no hardcoded threshold (Settings), no schema
  change, lane stays default-OFF.
- CI: check run 87947216862 success (2026-07-17T17:24Z).
- Local: full suite on the PR head — see TRACE below.

## Priority 4 — PR #464 review brief (rate-limit audit flood → log-only)

**Verdict: APPROVE-READY with one required follow-up commit (below). CI
green on its base (2026-07-12), diff is minimal and correct.**

- Diff: overflow branch writes a `detection_lane_rate_limited` structlog
  line instead of a `tg_alert_log` DB row; 2 existing tests re-pinned.
- **Cross-PR conflict found (session finding):** #466's two new tests
  (`test_score_ordered_selection_beats_freshness`,
  `test_cap_enforced_after_gating`) assert the overflow candidate gets a
  `blocked_cooldown`/`detection_lane:rate_limit` DB row — the exact rows
  #464 removes. Git auto-merges cleanly (different hunks) but the COMBINED
  tree fails those 2 tests. Each PR is green alone; the combination is red.
- **Fix (2 lines, belongs in #464's scope):** re-pin both assertions to
  log-only semantics (`assert "coin-lo" not in by_token` /
  `assert "coin-b" not in by_token`). Verified in this session: combined
  tree + fix → 29/29 detection tests pass.
- **Merge sequence:** merge #466 first → update #464 (merge master + the
  2-assertion fix) → CI green → merge #464.
- Post-deploy optional cleanup (from the PR body):
  `DELETE FROM tg_alert_log WHERE detail='detection_lane:rate_limit';`
  (~9.8K rows) at next deploy, after a backup.

---

## Priority 5 — merged-but-inert flags: decision briefs (operator)

1. **`DETECTION_ALERT_LANE_ENABLED`** (#460 + #466) — recommend ON for a
   soak AFTER Priority 1 restores CG (lane is inert during CG backoff) and
   #466 is merged+deployed. Watch the `detection_alert_funnel` log
   (pool/gated_out/eligible/sent) for the first week.
2. **`MOVED_ALREADY_POSTMORTEM_ENABLED`** (#459) — recommend ON at next
   deploy: bare-additive table, hourly recorder, evidence otherwise
   evaporates with `gainers_snapshots` 7-day retention. Rollback = flag off.
3. **`LIQUIDITY_ENRICHMENT_ENABLED`** (#382) — decide at watchdog
   activation time (see Priority 2 caution — its lag watchdog is in the
   same managed cron block). Enable sequence:
   `tasks/runbook_enable_liquidity_enrichment_2026_06_23.md`.
4. **`RETIRE_DEAD_TABLES_ENABLED`** (#461) — destructive DROPs,
   restore-from-backup-only rollback. Recommend: leave OFF until a deploy
   where a fresh backup is confirmed on disk; then flip, deploy, verify,
   unflip.

Also noted: the forward tracker `tasks/backlog_fable_analysis_2026_07_10.md`
is referenced by `backlog.md` as authoritative but is NOT committed to the
repo. If it exists only on a local machine, commit it.

---

## TRACE (evidence)

- #466 CI: run 29599282687 / job 87947216862 — success 2026-07-17T17:24:34Z.
- #464 CI: run 29201974946 / job 86674784037 — success 2026-07-12T17:35:33Z
  (base = pre-#465 master; superseded by the combined-tree verification below).
- Session-local (Linux, py3.12, `uv run --extra dev pytest`):
  - PR #466 head (893c7b5): full suite — result recorded in approvals log.
  - master + #466 + #464 combined: `tests/test_detection_alert.py` 2 FAILED
    (the cross-PR conflict) → with the 2-assertion fix: **29/29 passed**.
  - Combined-tree full suite — result recorded in approvals log.
- Merge cleanliness: `git merge --no-commit` of both branches onto master —
  auto-merge clean, no textual conflicts.

## Approvals log

| Action | Class | Approval record | Timestamp (UTC) |
|---|---|---|---|
| Merge PR #466 | merge | PENDING | — |
| Push 2-assertion test fix to `fix/detection-audit-flood` + merge PR #464 | branch-push + merge | PENDING | — |
| CG key restore (Priority 1) | prod-state | operator-executed, not in session scope | — |
| Watchdog activation via cron/deploy.sh (Priority 2) | deploy | operator-executed, not in session scope | — |
| Flag flips (Priority 5) | flag | operator decision, briefs above | — |
