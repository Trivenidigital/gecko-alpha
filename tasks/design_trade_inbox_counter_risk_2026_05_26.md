**New primitives introduced:** Trade Inbox counter-risk context fields (`counter_risk_score`, `counter_flags`, `counter_risk_predicted_at`), supporting predictions coin/timestamp index, and read-only UI context.

# Trade Inbox Counter-Risk Context Design

## Hermes-First Analysis

| Domain | Hermes skill found? | Decision |
|---|---|---|
| Dashboard API schema extension | none applicable | Build from existing `/api/trade_inbox` Pydantic + contract checker pattern. |
| Counter-risk enrichment | none applicable | Reuse existing `predictions` data and live-candidates defensive parsing. |
| Operator UI context rendering | none applicable | Build from existing `TradeInboxTab.jsx` table UI. |

Awesome-Hermes ecosystem check verdict: no Hermes skill covers this narrow in-tree API/UI contract extension.

## Data Contract

Each Trade Inbox row gains:

- `counter_risk_score`: integer or null.
- `counter_flags`: list whose items are either strings or dictionaries.
- `counter_risk_predicted_at`: ISO timestamp string or null.

Paper rows use the latest prediction for `predictions.coin_id == paper_trades.token_id`, ordered by `predicted_at DESC, id DESC`. Tracker-only rows return null/empty counter-risk fields because the project has intentionally not shipped a cross-identifier resolver.

The row fields are display-only. They are added to the row after base row diagnostics are computed and before serialization, but they are not read by:

- `_trade_block_reason`
- `_trade_score`
- `_trade_sort_key`
- `_trade_why_now`
- group/action-label assignment

## Query Shape

Add an idempotent startup migration in `scout.db.Database.initialize()` with
paper-migration sentinel `predictions_coin_predicted_id_idx_v1`:

```sql
CREATE INDEX IF NOT EXISTS idx_predictions_coin_predicted_id
    ON predictions(coin_id, predicted_at DESC, id DESC);
```

The dashboard read query remains optional enrichment:

```sql
SELECT coin_id, counter_risk_score, counter_flags, predicted_at
  FROM (
        SELECT coin_id, counter_risk_score, counter_flags, predicted_at, id,
               ROW_NUMBER() OVER (
                   PARTITION BY coin_id
                   ORDER BY predicted_at DESC, id DESC
               ) AS rn
          FROM predictions
         WHERE coin_id IN (...)
       )
 WHERE rn = 1
```

If `predictions` or its columns are unavailable, the endpoint returns normal Trade Inbox rows with null/empty counter-risk fields.

## Counter-Flag Parsing

`counter_flags` is stored as text JSON. The endpoint:

- JSON-decodes the field when present.
- Keeps list items only when each item is a string or dict.
- Drops garbage items.
- Returns `[]` on malformed JSON.

The contract checker accepts dict/string items, rejects other item types, and recursively scans nested strings for banned trading-advice / alert / urgency language.

## UI Semantics

The Trade Inbox cell should render:

- `Counter-risk context` label.
- `score <n>` as secondary metadata when present, not as a badge, column
  headline, threshold color, or sort/filter control.
- `from <timestamp>` or a compact timestamp/age when present.
- Up to two short flag fragments.
- `Counter-risk unavailable` when score and flags are empty.

It must not use high/medium/low risk labels, threshold colors, filters, sort headers, urgency labels, or alert-like copy.

Flag rendering is fixed and plain-text only:

- For string flags: render the string.
- For dict flags: choose the first non-empty string from `label`, `type`,
  `name`, `reason`, then append `detail` only when it is a string and the
  combined text stays short.
- Render at most two fragments.
- Trim each fragment to 80 characters.
- Do not derive CSS class, color, icon, severity label, urgency copy, alert copy,
  or ordering from flag contents.

## Tests

- Contract tests:
  - clean rows with counter-risk fields pass;
  - tracker rows with non-empty counter-risk fields fail;
  - rich dict/string flags pass;
  - invalid flag items fail.
- Endpoint tests:
  - paper row gets latest prediction context;
  - tracker-only row gets null/empty context;
  - two otherwise identical rows with different counter-risk values keep the same `group`, `action_label`, `trade_score`, and relative order.
- Frontend static test:
  - Trade Inbox reads `counter_risk_score`, `counter_flags`, `counter_risk_predicted_at`;
  - renders `Counter-risk context` and `Counter-risk unavailable`;
  - does not add sort/filter controls for counter-risk.
  - does not add escalation vocabulary such as high/low/urgent/alert/trade-now
    inside the counter-risk rendering block.

## Anti-Scope

- No alerting, Telegram sends, alert qualification, urgency tiers, ranking, source trust score, cross-id resolver, execution advice, signal threshold changes, or pruning.
- PR #278’s Now Tradable-only implementation is superseded by this Trade Inbox-first design.

## Design Review Folds

- UI/product review: pin flag rendering rules to fixed plain-text precedence,
  hard caps, and no style/severity behavior derived from flag contents; keep
  score visually subordinate and guard the block against escalation vocabulary.
- Runtime/contract review: enforce tracker rows as null/empty counter-risk in
  the contract checker, add a static display-only boundary test, place the
  prediction index in startup migration with a `paper_migrations` sentinel, and
  use DESC ordering in the index to match the latest-prediction query.
