# Vendor sample / probe packet template

**New primitives introduced:** NONE (packet only)

## Goal

What question the sample answers (one sentence).

## Drift-check

Confirm an in-tree solution does not already exist.

## Hermes-first analysis

| Domain | Hermes skill found? | Decision |
|---|---|---|
| Vendor integration | <yes — name + url> OR <none found> | <use it> OR <probe only> |

Awesome-hermes-agent ecosystem check: <checked; verdict>.

## Operator-only gates (explicit)

- **Paid APIs or paid vendor calls require explicit operator approval.**
- No secrets changes in this packet.
- No prod DB writes in this packet.

## Budget + safety envelope

- Max requests:
- Rate limit:
- Expected cost: (must be $0 unless operator-approved)
- Timeout / retry policy:

## Inputs (exact)

- Token set / sample selection:
- Timestamp window:
- Expected request shapes:

## Pre-registered success criteria

- What would count as “YES”:
- What would count as “NO”:
- What would count as “INCONCLUSIVE”:

## Runtime-state verification (before running)

| Assumption | Where verified | Current value | Active/enabled? |
|---|---|---|---|
| <assumption> | <command/query> | <value> | <yes/no> |

## Output artifacts to record

- Raw responses (sanitized)
- Summary table
- Decision + next step

