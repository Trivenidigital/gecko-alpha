**New primitives introduced:** NONE.

# BL-NEW-AUDIT-SURFACE-ADDENDUM — Mini-Sweep Findings 2026-05-18

**Data freshness:** Computed against srilu prod 2026-05-18. Re-run via the SSH block in backlog.md L1593-1601 (or the canonical 5 commands below) if any cycle re-verifies.

**Source:** srilu `ssh srilu-vps` (HEAD = `cdeb31f` = origin/master, includes PRs #150-#154).
**Scope:** cycle-11 V58 PR-review follow-up surfaces — 5 categories not covered in original `BL-NEW-OTHER-PROD-CONFIG-AUDIT`.

**Drift-check (per CLAUDE.md §7a):** worktree HEAD = `cdeb31f` = origin/master (zero divergence). Grep for `BL-NEW-AUDIT-SURFACE-ADDENDUM` returns backlog.md:1589 only — no parallel session in-progress.

**Hermes-first verdict:** In-tree infrastructure audit (Linux systemd / apt sources / web-server presence). No Hermes primitive applies; this is operator-driven prod inspection. Briefly documented per CLAUDE.md §7b.

## TL;DR

**ALL 5 CATEGORIES CLEAN. No follow-up items filed. Backlog status flips PROPOSED → AUDITED 2026-05-18.**

## Raw output (5 categories)

```text
=== Q1: nginx/caddy ===
not-found
not-found

=== Q2: /etc/systemd/system.conf (uncommented) ===
[Manager]

=== Q3: /etc/apt/sources.list.d/ ===
nodesource.sources
ubuntu.sources

=== Q4: docker/containerd ===
not-found
not-found

=== Q5: systemd units (excluding template@) ===
[... standard cloud-init + gecko-{pipeline,dashboard,backup,backup-watchdog}
     + eod-reconcile + check-compliance-deadlines + standard Linux services ...]
```

## Per-category verdict

| Category | Result | Verdict |
|---|---|---|
| nginx/caddy presence | both `not-found` | ✅ No web servers running on srilu; gecko-dashboard exposes :8000 directly per `reference_srilu_vps.md`. Acceptable architecture. |
| `/etc/systemd/system.conf` overrides | only `[Manager]` (no key-value overrides) | ✅ Defaults — no operator-customized systemd manager settings to track |
| `/etc/apt/sources.list.d/` | `nodesource.sources` + `ubuntu.sources` only | ✅ Minimal repository surface — Node (for narrative scanner / dashboard build) + Ubuntu only. No third-party sources to audit for trust |
| docker/containerd | both `not-found` | ✅ No container runtime; all gecko services run as systemd-managed bare-metal Python processes (per cycle-6 systemd unit capture) |
| systemd unit inventory | gecko-{pipeline,dashboard,backup,backup-watchdog} + eod-reconcile + check-compliance-deadlines + standard cloud-init/Ubuntu units | ✅ Inventory matches the captured units in `systemd/` from cycle 6 plus 2 expected sibling services (eod-reconcile, check-compliance-deadlines from earlier project setup). No surprise services. |

## Decision

**Close BL-NEW-AUDIT-SURFACE-ADDENDUM.** No code changes, no new backlog items, no operator action required.

Mini-sweep proved the cycle-11 V58 reviewer's concern (additional surfaces beyond original 17 categories) is empirically unfounded for srilu's current configuration. If srilu's infrastructure shifts (e.g., adds a web server, containerization, or third-party apt source), the next operator-prod-config audit cycle should re-include these 5 categories.

## Re-run command (operator convenience)

```bash
ssh srilu-vps '
echo "=== Q1: nginx/caddy ===" && systemctl is-enabled nginx caddy 2>&1
echo "" && echo "=== Q2: /etc/systemd/system.conf ===" && grep -v "^#\|^$" /etc/systemd/system.conf
echo "" && echo "=== Q3: /etc/apt/sources.list.d/ ===" && ls /etc/apt/sources.list.d/
echo "" && echo "=== Q4: docker/containerd ===" && systemctl is-enabled docker containerd 2>&1
echo "" && echo "=== Q5: systemd units ===" && systemctl list-units --type=service --all | grep -v "@\.service$" | head -40
' > /tmp/audit_surface.txt
```

## Cross-references

- `backlog.md` BL-NEW-AUDIT-SURFACE-ADDENDUM (originating, now flipping to AUDITED)
- `tasks/findings_other_prod_config_audit_2026_05_17.md` (cycle 11 original — this addendum closes the V58 follow-up surface)
- `systemd/` directory (cycle 6 BL-NEW-SYSTEMD-UNIT-IN-REPO — pipeline + dashboard units captured)
- `reference_srilu_vps.md` (operator-facing VPS deployment reference)
