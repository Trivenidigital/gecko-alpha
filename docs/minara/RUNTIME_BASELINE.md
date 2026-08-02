# RUNTIME_BASELINE — 2026-08-02

Established read-only before any change. Every conclusion is tied to the active
consuming process, not to documentation, a stale env file, or a similarly-named unit.

## Repository

| | |
|---|---|
| Worktree | `.claude/worktrees/kraken-launch-lane` (locked) |
| Branch at session start | `feat/solana-reverse-swap` @ `3034f6eb` |
| Working branch | `feat/venue-neutral-trade-intent` |
| `origin/master` | `2128b457` |
| `3034f6eb` in master? | **No** — contained only in `origin/feat/solana-reverse-swap` |

`3034f6eb` ("the daily cap is an ENTRY cap and must not gate the exit") is an
unmerged commit on the branch this worktree was already on. It is carried into this
branch as inherited history and is **not** part of this session's work.

## Deployed testbed (`srilu-vps`)

| | |
|---|---|
| Deployed HEAD | `2128b457` — matches `origin/master` |
| `gecko-pipeline` | `active` |
| `gecko-dashboard` | `active` |
| Hermes process | pid 1912637, user `gecko-agent` |
| Hermes executable | `/home/gecko-agent/.hermes/hermes-agent/venv/bin/python -m hermes_cli.main gateway run --replace` |
| Effective `HERMES_HOME` | `/home/gecko-agent/.hermes` (`/root/.hermes` also exists and is **not** the active one) |

Two-Hermes-home condition still present; the active home is the `gecko-agent` one, as
recorded previously. Routing changes, had any been made, would belong there.

## Minara — absent

Finding 6 of the prior ruling re-verified independently this session:

- no `minara` on `PATH` for `root`
- no `minara` on `PATH` for `gecko-agent`
- `/root/.minara` — does not exist
- `/home/gecko-agent/.minara` — does not exist

The last is decisive rather than merely suggestive: `dist/config.js::ensureDir()` creates
`~/.minara` on first run, so its absence for both users means the CLI has never executed
here.

## Kraken MCP — absent

`find / -maxdepth 6` for `*kraken*mcp*` / `*mcp*kraken*` returned nothing, and there is no
`mcp.json` in the active Hermes home. This confirms the 2026-05-30 phantom-precondition
finding is still true 2026-08-02. There is no Kraken MCP to integrate.

## Effective flags

```
LIVE_MODE=shadow
SOLANA_MODE=SUPERVISED_LIVE
SOLANA_BOUNDED_AUTONOMOUS_ENABLED   (not set → False)
MINARA_ALERT_ENABLED=false
KRAKEN_PILOT_ENABLED=true
KRAKEN_PILOT_PAIR=BTC
```

## Positions — unchanged

| Venue | Position | Touched this session |
|---|---|---|
| Kraken | 0.0002 BTC | no |
| Solana | 101,456,720 lamports (≈0.1015 SOL) | no |
| Solana | 6,899,618 raw USDC = **6.899618 USDC** | no |

USDC is 6-decimal; the raw integer is base units. Total testbed exposure is roughly $40.
No order was placed, no transaction was signed, no transaction was broadcast, no ATA was
closed, and no wallet was funded or swept.

## Incident — credential exposure (operator action required)

During the recon step a redaction filter was written incorrectly
(`sed 's/=.*KEY.*/=<redacted>/'` requires the literal `KEY` to appear *after* the `=`, so
it matched nothing). `KRAKEN_API_KEY` and `KRAKEN_API_SECRET` printed in full into a
scratchpad file.

Remediation taken: file deleted; repository verified clean of both values
(`grep -rl` over the worktree excluding `.git` returns only test fixtures with dummy
values). No commit, log, or document in this branch contains them.

Remediation **not** taken, and owed to the operator: **rotate the Kraken API key pair.**
Credential rotation is on this session's prohibited list, so it was not performed here.
This is the credential guarding the open BTC position.
