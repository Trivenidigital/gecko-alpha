**New primitives introduced:** NONE — handoff/state doc documenting the post-swap VPS layout. Supersedes the VPS-layout sections of `tasks/handoff_narrative_scanner_overnight_2026_05_13.md` (the activation-prereq sections of that doc remain valid).

# VPS swap completed — 2026-05-13 04:00Z

Full swap of operator-authorized VPS layout:
- **shift-agent** (catering): srilu-vps → main-vps
- **gecko-agent Hermes** (narrative scanner): main-vps → srilu-vps

Driver: co-locate gecko-agent Hermes with gecko-alpha on srilu so the narrative-scanner HMAC handshake is localhost (not cross-VPS), and so BL-073 Phase 1 GEPA on `narrative_prediction` can run with local file access.

## Final architecture

| Component | Host | Status |
|---|---|---|
| gecko-alpha (pipeline + dashboard + scout.db 4.5 GB) | **srilu-vps** | active throughout migration |
| gecko-alpha narrative endpoints (`/api/coin/lookup`, `/api/narrative-alert`) | srilu-vps | live, gated off (HMAC secret empty → 503) |
| gecko-agent Hermes (narrative scanner) | **srilu-vps** | installed at `/home/gecko-agent/.hermes/` (separate user from shift-agent's old install which is now decommissioned on srilu) |
| Hermes binary `/usr/local/bin/hermes` on srilu | srilu-vps | system-wide install at `/usr/local/lib/hermes-agent/` (kept after shift-agent decom — shared between shift-agent's gateway formerly and gecko-agent's tool now) |
| 5 SKILL.md drafts (narrative scanner) | srilu-vps:`/home/gecko-agent/.hermes/skills/` | placed; activation pending operator inputs |
| Self-Evolution Kit (gecko-agent) | srilu-vps:`/home/gecko-agent/hermes-agent-self-evolution/` | installed during Phase 1 catch-up |
| shift-agent (catering production) | **main-vps** | active (`hermes-gateway.service` running, bridge listening :3000, WhatsApp connected) |
| shift-agent data | main-vps:`/opt/shift-agent/` + `/root/.hermes/` | migrated from srilu, ownership preserved |
| shift-agent binaries | main-vps:`/usr/local/bin/{create-catering-lead,apply-catering-owner-decision,...}` | migrated (Fix A from PR #85 is in the source-tarball path, but the srilu hot-patched versions are what currently sit on main; both contain Fix A) |
| Self-Evolution Kit (shift-agent) | main-vps:`/opt/shift-agent/hermes-agent-self-evolution/` | installed per operator's "install on main too" |
| `/opt/triveni/portal/` | main-vps:`/opt/triveni/portal/` | migrated (small static HTML, ~44 KB, NOT running on either VPS — no systemd unit, was manual python http.server on srilu) |
| Minara CLI (live execution) | srilu-vps (planned) | **NOT YET INSTALLED** — gated on BL-055 live-trading unlock + operator-provided wallet keys |
| Quarantine of pre-swap state | srilu-vps:`/root/decommissioned-2026-05-13/shift-agent-srilu-backup/` (~136 MB) + main-vps:`/root/decommissioned-2026-05-13/gecko-agent-main-backup/home.tar.gz` (1.4 GB) | retained for ~30-day rollback window |

## What was migrated (deltas the parallel session handed off)

Per their 6-row table:

| Item | Source | Status post-migration |
|---|---|---|
| `/usr/local/bin/create-catering-lead` (Fix A) | PR #85 merged to main; srilu hot-patched version migrated | ✅ on main-vps, Fix A active |
| `/root/.hermes/skills/parse_catering_inquiry/SKILL.md` (Fix A) | PR #85 source | ✅ on main-vps, Fix A active (verified via migrated copy) |
| `WHATSAPP_MODE=bot` + `WHATSAPP_ALLOWED_USERS=*` in `/root/.hermes/.env` AND `/opt/shift-agent/.env` | runtime config, NOT in source | ✅ set on main-vps in both files |
| `/opt/shift-agent/state/catering-leads.json` (L0001–L0013, last=OWNER_REJECTED) | state | ✅ migrated via tar of `/opt/shift-agent/` |
| `/opt/shift-agent/logs/decisions.log` | state (audit chain) | ✅ migrated |

## What was NOT in their handoff but I migrated anyway

- **`/opt/triveni/portal/`** — 44 KB static HTML, was running via manual `python3 -m http.server` as shift-agent user on srilu since May 10. No systemd unit, no nginx config on srilu. Killed during process-list cleanup; files preserved + migrated. **Currently NOT running on either VPS.** Parallel session should confirm whether this needs to be restarted on main-vps (if so, decide on systemd unit vs nginx + decide port).

## What I didn't preserve (intentional decisions worth flagging)

- `/usr/local/bin/hermes` on srilu was a symlink to `/usr/local/lib/hermes-agent/`. The system-wide Hermes install on srilu stays in place; gecko-agent's separately-installed Hermes uses `/home/gecko-agent/.local/bin/hermes` (per-user). On main-vps, I fresh-installed `/usr/local/lib/hermes-agent/` via the official installer (1.9 GB) — same version as srilu (v0.13.0).
- **Stale debug processes on srilu** (7 × `tail -F /opt/shift-agent/logs/decisions.log` + 7 × awk filters, all from May 3): killed during stop sequence. Operator/parallel-session can confirm these were stale and don't need to be restarted on main.
- **Failed systemd services on srilu** (`catering-owner-action-watchdog`, `shift-missed-dispatch-notifier`): unit files migrated to main but they were in `failed` state on srilu before migration. They may behave the same on main. Triage during operator review.

## Failure modes encountered (and fixed) during Phase 2

1. **`hermes-gateway.service` flapping initially**: bridge dir `/usr/local/lib/hermes-agent/scripts/whatsapp-bridge/` is root-owned; runtime tries to `npm install` as `shift-agent` user → `EACCES`. **Fix**: ran `npm install` as root in that dir (144 packages, 2 min). After that, gateway came up clean and WhatsApp connected via the migrated `auth.json` (no re-pair needed). **If operator re-installs Hermes from scratch in the future, this npm install step needs to be repeated** — flag for parallel-session runbook.

2. **`/root/.hermes/node/bin/` PATH ref in systemd unit doesn't resolve** on main (same as srilu — preserved-but-unused). PATH fallback hits `/usr/bin/node` (apt-installed Node 20.20.2). Working but worth a cleanup pass on the unit file.

## Phase 1 (gecko-agent migration to srilu) — done

| Step | State |
|---|---|
| `gecko-agent` user created on srilu | ✅ UID assigned, home `/home/gecko-agent/` |
| Hermes installed as gecko-agent (non-root, doesn't clobber shift-agent's binary) | ✅ verified via sha256 — shift-agent's `/usr/local/bin/hermes` unchanged (was symlink anyway, gecko-agent uses `/home/gecko-agent/.local/bin/hermes`) |
| OpenRouter key transferred from shift-agent's `/root/.hermes/.env` → gecko-agent's `~/.hermes/.env` (same VPS, same key as previously verified `sk-o...cf22`) | ✅ |
| Model section in `~/.hermes/config.yaml` set to `moonshotai/kimi-k2-thinking` via openrouter base_url | ✅ |
| `GECKO_ALPHA_BASE_URL=http://localhost:8000` in `~/.hermes/.env` (now localhost — was cross-VPS) | ✅ |
| 5 SKILL.md drafts + `kol_list.yaml` placeholder transferred to `/home/gecko-agent/.hermes/skills/` | ✅ |
| Self-Evolution Kit cloned + uv venv + `pip install -e .[dev]` | ✅ |
| `gecko-agent` quarantined on main-vps (`/root/decommissioned-2026-05-13/gecko-agent-main-backup/home.tar.gz`) | ✅ 1.4 GB backup retained |

## Operator activation prereqs — UPDATED for new VPS layout

The handoff from the prior session (`tasks/handoff_narrative_scanner_overnight_2026_05_13.md`) listed 3 prereqs. Updated for the swap:

### 1. Generate + place HMAC secret (~30 sec) — UPDATED FOR LOCALHOST

```bash
SECRET=$(python3 -c "import secrets; print(secrets.token_hex(32))")
ssh srilu-vps "echo 'NARRATIVE_SCANNER_HMAC_SECRET=$SECRET' >> /root/gecko-alpha/.env && chmod 600 /root/gecko-alpha/.env"
ssh srilu-vps "echo 'NARRATIVE_SCANNER_HMAC_SECRET=$SECRET' >> /home/gecko-agent/.hermes/.env"
ssh srilu-vps "chown gecko-agent:gecko-agent /home/gecko-agent/.hermes/.env && chmod 600 /home/gecko-agent/.hermes/.env"
ssh srilu-vps "systemctl restart gecko-pipeline gecko-dashboard"
```

Both files now live on srilu (same VPS) — much simpler than the original cross-VPS instruction.

### 2. Curate real KOL list (~10 min)

Edit `/home/gecko-agent/.hermes/skills/crypto_narrative_scanner/kol_list.yaml` on **srilu-vps** (was main-vps in the prior handoff). 11 starter handles + 4 PLACEHOLDER entries you must replace. Keep total = 15 to fit X v2 Basic quota.

### 3. xurl OAuth-PKCE pairing (~5 min, interactive)

On srilu-vps as gecko-agent user:
```bash
sudo -u gecko-agent -i
# inside gecko-agent shell:
hermes install xurl    # installs the xurl skill
xurl auth              # opens browser flow; complete X dev account pairing
```

## NEW prereq: Minara on srilu for live execution

Per operator's "Minara also to Srilu for live execution":

- Minara CLI is currently on operator's local terminal (per memory `project_m1_5c_deployed_2026_05_11.md`)
- Target architecture: Minara on srilu-vps, executed by gecko-alpha alerts
- **Gated on**: (a) BL-055 live-trading unlock — currently shadow mode per memory; (b) operator-provided wallet keys
- **Not actionable tonight** — requires BL-055 status review + wallet-key handling (security-sensitive)
- **Recommendation**: revisit when BL-055 unlock criteria met (per memory: "7d clean + balance_gate.py + policy review")

## Verification commands (run anytime to confirm state)

```bash
# srilu — gecko-alpha + gecko-agent Hermes
ssh srilu-vps 'systemctl is-active gecko-pipeline gecko-dashboard; \
  curl -s -o /dev/null -w "lookup: %{http_code}\n" "http://localhost:8000/api/coin/lookup?ca=Foo123Bar456789&chain=solana"; \
  id gecko-agent; \
  sudo -u gecko-agent /home/gecko-agent/.local/bin/hermes --version'

# main — shift-agent
ssh main-vps 'systemctl is-active hermes-gateway; \
  ss -tlnp | grep ":3000"; \
  sudo -u shift-agent /usr/local/bin/create-catering-lead --help | head -3'
```

## Pre-registered evaluation (UNCHANGED from prior handoff)

Decision date: writer-deployment + 28d = **2026-06-10**. Per-chain (Solana / ETH / Base) metrics: latency reduction ≥30 min, coverage delta ≥3 pumps, precision ≥15%, operator-manual-tag recall ≥40%. n-gate 10 per chain.

## Rollback (if either side breaks within ~30 days)

**gecko-agent on srilu broken:** `userdel -r gecko-agent` removes the install. shift-agent on srilu was already decommissioned; gecko-alpha on srilu unaffected.

**shift-agent on main broken:** services already stopped on srilu (quarantined). Restore from main's `/root/decommissioned-2026-05-13/gecko-agent-main-backup/home.tar.gz` (1.4 GB) is for gecko-agent's main install — IRRELEVANT for shift-agent rollback. For shift-agent rollback: restore from srilu's `/root/decommissioned-2026-05-13/shift-agent-srilu-backup/` (move dirs back to original paths, re-enable systemd units, restart services).

**Hermes binary lost on either VPS:** re-run installer. `/usr/local/lib/hermes-agent/` (1.9 GB) gets re-created.

## End-state

shift-agent live and serving on main-vps. gecko-agent installed and disabled-state on srilu (Day 1 PR #110 endpoints still responding 503 because operator hasn't set HMAC secret yet — same as before swap). Self-Evolution Kit installed on both VPSes under their respective users. Minara install deferred until BL-055 unlock.

**Operator next steps to activate the narrative scanner are the 3 prereqs above — all on srilu now, no cross-VPS work.**
