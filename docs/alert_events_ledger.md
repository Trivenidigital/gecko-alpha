# `alert_events` — analyst notes

Reading notes for the F3 control-plane event ledger. The schema itself lives in
`scout/db.py` (`_ALERT_EVENTS_DDL`) and the writer in
`scout/trading/alert_events.py`; this file records the things a query against
the table will get WRONG without them.

Two boundaries matter, and both are invisible in the row data unless you know to
look for them.

---

## 1. `PAROLE_DENIED_DEDUP_V2_T0` — the `parole_denied` counting boundary

**`parole_denied` rows mean two different things either side of one deploy, and
any COUNT that spans the boundary is meaningless.**

| Era | What one row is | How to select it |
|---|---|---|
| pre-T0 | one row per DENIAL ATTEMPT (per-attempt fanout) | `event_type = 'parole_denied' AND payload_hash IS NULL` |
| post-T0 | one row per DISTINCT DENIAL STATE (first occurrence) | `event_type = 'parole_denied' AND payload_hash IS NOT NULL` |

**The boundary is exactly and unambiguously `payload_hash IS NULL` vs
`IS NOT NULL`.** Do not use a timestamp cutoff: `payload_hash` is what the
first-occurrence probe keys on, so it is NULL on every row the old writer
produced and non-NULL on every row the new one produces, by construction. A
timestamp comparison additionally has to get the deploy instant right and is
wrong for any row written inside the restart window.

### Why the two eras differ

A latched combo (`suppressed=1`, parole window open, budget spent) takes the same
denial branch on EVERY dispatch attempt. The original writer appended a row per
attempt, which produced 20,094 byte-identical rows in 17h of prod across three
combos. The current writer records the first occurrence of each distinct denial
state — keyed on `denial_digest(combo_key, denial_reason, suppressed,
suppressed_at, parole_at)` — so a meaningful change (a different reason, or a
re-latch, which moves `suppressed_at` and `parole_at`) still records while pure
repetition collapses.

### Deploy facts

- Deployed **2026-08-16, ~06:14Z**.
- First post-T0 rows: **06:14:56Z** (`losers_contrarian`), **06:15:54Z**
  (`cg_trending_rank+first_signal`), **06:29:35Z** (`chain_completed`).
- Prod holds **22,938 pre-T0 rows**.

### The pre-T0 rows are PRESERVED — permanently

**Operator ruling: there is no compaction migration for these rows, ever.** They
are a truthful record of what the old writer did and of the defect itself; a
migration that collapsed them would destroy the only evidence of the fanout and
would silently rewrite history that other analyses may already have cited.

Consequences for analysis:

- **Never** compute a denial RATE across the boundary. Pre-T0 counts scale with
  dispatch attempts; post-T0 counts scale with distinct denial states. They are
  different units.
- To count denial STATES over all time, use the post-T0 arm only, and say so.
- To count denial ATTEMPTS, only the pre-T0 arm can answer, and only for its own
  window.

---

## 2. `payload_hash` means two different things depending on `event_type`

`payload_hash` is a `sha256` hex digest, but it is not always a digest OF A
MESSAGE.

| Rows | What the digest is over | Preimage stored? |
|---|---|---|
| `alert_dispatched` / `alert_delivered` / `alert_failed`, `marker_stamped`, `marker_cleared`, `reversal_pending_recorded` | the EXACT text that was sent (or the exact durable payload that was written) — `payload_digest(text)` | yes |
| `marker_anomaly` | usually the same, but see below | **mostly** |
| `parole_denied` | the denial STATE — `denial_digest(...)`, a dedup key | **no** |
| everything else (`suppression_transition`, `parole_slot_spent`, `parole_slot_refunded`, `refresh_completed`, `ledger_installed`) | nothing — `payload_hash` is NULL | n/a |

`parole_denied` rows have no body because nobody was sent anything. Their digest
resolves to nothing in `alert_payloads`, and that is correct rather than a gap.
The state that was decided against is preserved in `state_json` on the row.

`marker_anomaly` is **not uniform**. Most of its rows carry a real message
digest with a stored body (`pending_commit_lost`, `pending_not_cleared`, the
marker-write failures). But the `pending_unreadable` writer stores
`payload_hash = NULL` when the durable `reversal_alert_pending_json` value is
not TEXT — there are no bytes to hash and no body to keep. Read this column per
row, never per `event_type`.

---

## 3. `alert_payloads` — reconstructing a body from its digest

`alert_events` records only the digest. The exact bytes live in the
content-addressed `alert_payloads` table (`schema_version 20260817`):

| Column | |
|---|---|
| `payload_hash` | `TEXT PRIMARY KEY` — `sha256` of the body's UTF-8 bytes |
| `payload` | `BLOB` — those exact bytes |
| `byte_length` | `INTEGER` — their length, a second independent statement about the same bytes |
| `first_seen_at` | `TEXT` — when the body was first stored, never moved by a later reference |

**Read it through `scout.trading.alert_events.load_alert_payload(db, digest)`.**
It verifies the stored body against its own key before returning it and raises
`AlertPayloadCorrupt` rather than handing back text it cannot vouch for. A raw
`SELECT` skips that check.

### NEVER `CAST(payload AS TEXT)`

The column is a BLOB on purpose, and casting it back to TEXT reintroduces
exactly the hazard that choice was made to avoid. Two bodies this substrate
stores faithfully break under the cast — measured, not theorised:

| Body | Reality | Under `CAST(payload AS TEXT)` |
|---|---|---|
| contains a lone surrogate (`\ud800`) | 14 bytes, round-trips exactly | the `SELECT` **raises** `OperationalError: Could not decode to UTF-8` |
| contains an embedded NUL (`abc\x00defghijk`) | 12 bytes, round-trips exactly | SQL `length()` reports **3** — silently truncated at the NUL, and any SQL-side `substr` / `LIKE` / comparison is truncated with it |

The surrogate case is loud and merely inconvenient. The NUL case is the
dangerous one: the row is still returned, no error is raised, and an analyst
reading a length or matching a pattern gets a confident wrong answer about what
the operator was told. Both are reachable — `json.loads` yields unpaired
surrogates from `\udXXX` escapes in upstream API text, which is why the writer
encodes with `surrogatepass`.

If you must query in raw SQL, select the BLOB and decode it in the client with
`errors="surrogatepass"`; use `length(p.payload)` (bytes) or `p.byte_length`,
never `length(CAST(...))`.

```sql
-- what the operator was actually told, for one combo.
-- `p.payload` is a BLOB: decode it client-side with
-- bytes.decode("utf-8", errors="surrogatepass"), or prefer load_alert_payload.
SELECT e.created_at, e.event_type, e.transition,
       p.payload AS body_bytes,
       p.byte_length
FROM alert_events e
LEFT JOIN alert_payloads p ON p.payload_hash = e.payload_hash
WHERE e.combo_key = ?
  AND e.event_type IN ('alert_dispatched', 'alert_delivered', 'alert_failed')
ORDER BY e.id;
```

### `body IS NULL` has at least four distinct causes — do not conflate them

Separate the two questions first: **is `e.payload_hash` itself NULL, or is it
present but unresolvable?** They are different findings.

1. **The row carries no `payload_hash` at all.** This is the common case, not an
   edge case: most `event_type`s never pass one (see the table in section 2), and
   `marker_anomaly` / `pending_unreadable` deliberately writes NULL when the
   durable payload is not TEXT. Nothing is missing — there was never a digest.
   Filter with `e.payload_hash IS NOT NULL` before drawing any conclusion about
   coverage.
2. **Pre-cutover row.** The digest predates `alert_payloads`. **No backfill is
   possible**: the bodies behind those digests exist nowhere to recover from,
   and a re-rendered body would be different text that hashes differently.
3. **A `parole_denied` row.** By design — see section 2. There was no message,
   so the digest is a dedup key and resolves to nothing.
4. **A preimage write that failed.** The writer is fail-soft: it logs
   `alert_payload_write_failed` (`err_id=ALERT_PAYLOAD_WRITE`) and returns the
   digest anyway, so the page still goes out and the ledger row still lands.
   Search journald for that event before concluding (2).

"At least" is meant literally — treat this list as the causes known today, not
as a closed set, and check the writer before asserting a cause you cannot see in
the row.

The `LEFT JOIN` is load-bearing. An inner join silently drops every row in all
four categories, which is most of the historical table.
