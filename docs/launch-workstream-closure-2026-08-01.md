# GECKO-ALPHA LAUNCH WORKSTREAM — CLOSURE RECORD (2026-08-01)

Deployed HEAD: 442a806. Both milestones delivered EARLY.

## MILESTONE 1 — KRAKEN (target Aug 6, delivered Jul 31)
Signature/order OK6KKB-52USI-3IUOR3, trade TGWZ5F-AKV52-PWBZLS.
BUY 0.0002 BTC limit 63100 -> FILLED @ 62900.10. Cost 12.58002, fee 0.10064 (taker).
USD 159.1247 -> 146.4441; BTC 0 -> 0.0002. Reconciliation PASS (observed -12.6806 vs
expected -12.68066). Venue open orders after: 0. Decision/cl_ord_id
998dd9f0-0d46-4cf0-ae82-cc750e48d5ca. Ledger row 1, status `open` (position held BY DESIGN).
Evidence: /root/gecko-alpha/pilot_evidence/kraken_pilot_998dd9f0-*.json
PRs: #483 fc60c40e (adapter core), #484 63e4c7e4 (order lifecycle), #485 6c3cf20e (runner).

## MILESTONE 2 — SOLANA (target Aug 13, delivered Aug 1)
Signature 3oUrwFDL7ThojusxnTY5RhwHgwTNVL1pgwdGAgyaaw68ZQKrDzgAeH2HvaHiGmXZ1TbcFanj4pADw1gRzMmpj1M3
Slot 436606196, FINALIZED, err=None, base fee 5000.
0.09854428 SOL -> 6.899618 USDC. Entry 71.871 vs quoted 71.861 => slippage -1 bps (BETTER).
SOL 0.200001 -> 0.10145672; USDC 0 -> 6.899618. Reconciliation PASS (wallet == ledger exactly).
Decision a6ed47d6-f373-4809-8359-c0e9998172d0, execution state `reconciled`.
Ledger row 5, status `open` (position held BY DESIGN).
Evidence: /root/gecko-alpha/pilot_evidence/solana_lane_a6ed47d6-*.json
PRs: #486 (execution core), #487 (approve-before-signing), #488 964f22de (permanent lane),
#489 5c033a62 (terminal-state coherence), #490 442a8064 (bundleOnly + header casing).

## PRODUCTION-COMPLETION STANDARD
8 of 9 acceptance items satisfied. 1 item intentionally deferred: BOUNDED_AUTONOMOUS.
1 supervised tx finalized+reconciled ............ PASS (on-chain verified independently)
2 restart recovery at EVERY durable state ....... PASS (13 states; real interrupt + fresh runner)
3 unknown submission prevents duplicate exec .... PASS (4 verdicts; fresh place blocks pre-network)
4 all financial/fee limits mechanically enforced  PASS (17 limits, 4 stages, fail-closed)
5 kill switch + emergency stop .................. PASS (both boundaries, kill read twice)
6 resolver failover implemented and tested ...... PASS (pool, genesis validation, split-view downgrade)
7 Jito is the SOLE submission path .............. PASS (rpc_client structurally has no send method)
8 transition to bounded autonomy = config only .. PARTIAL — see below
9 no architectural rewrite required ............. JUDGEMENT (4 completeness assertions as proxy)

### Item 8 — the honest gap
BOUNDED_AUTONOMOUS refuses BY CONSTRUCTION: `authorize()` returns False unconditionally.
The EXECUTION architecture is genuinely mode-agnostic (verified at RUNTIME, not just AST:
engine built under each mode declares 17 limits, ZERO differences; one authorize() call site;
place() cannot name either live mode; preconditions enforced mechanically). What is missing is
the autonomous per-trade DECISION policy — deliberately unimplemented because NO SIGNAL PATH
feeds this lane (it is operator-invoked with an explicit amount). Writing one would mean
inventing trading logic nobody specified. Bounded change behind a built seam.

## DEFECTS FOUND ONLY BY TRADING FOR REAL (both same shape: mechanism exists, looks right,
## defeated one layer down by something untested)
- #489: a definitive verdict retired the LEDGER row but left the EXECUTION row at
  submission_attempted; `resolve` printed "rerunning is safe" while `place` refused.
  Root cause: `assert_coherent` existed, declared the rule, and was NEVER CALLED on a write path.
- #490: `x-bundle-id` WAS read, but aiohttp's case-INsensitive CIMultiDict was copied into a
  plain dict, making lookup case-SENSITIVE. Every test mocked it lowercase, so the tests
  encoded the same wrong assumption as the code.

## WHY THE FIRST TWO SOLANA ATTEMPTS DID NOT LAND (both cost ZERO)
Not the tip (100k then 500k vs P95=370k) and not latency (all Jito regions ~0.1s from Helsinki).
Cause: `bundleOnly=true` — Jito docs: revert protection means the tx "must win the block-engine
auction rather than having fallback routing options". Now SOLANA_JITO_BUNDLE_ONLY=False;
Jito remains the SOLE broadcast path; independent pre-sign simulation covers the revert risk.

## CURRENT POSTURE (verified 2026-08-01)
LIVE_MODE=shadow | LIVE_USE_REAL_SIGNED_REQUESTS=false (Kraken disarmed post-trade)
KRAKEN_PILOT_ENABLED=true | SOLANA_MODE=SUPERVISED_LIVE | bounded-autonomy flag ABSENT
Kill switches INACTIVE both lanes. lifecycle/money axes: coherent.
Positions HELD: 0.0002 BTC (Kraken, row 1) + 6.899618 USDC (Solana, row 5). Each blocks its
lane's next placement BY DESIGN. Wallet CqnCgVWJCimvkNX1YE1nerNZgBD69qKu8rhrqWGeDDAE also
holds 0.10145672 SOL.

## OPEN ITEMS (none launch-blocking)
1. Dispose of the two held positions (operator decision). Kraken exit feature deliberately NOT
   built pre-Solana; a Solana supervised sell is unbuilt. Either lane's next placement is
   blocked until its position is disposed.
2. Item 8 decision policy — build only if/when a signal path is specified.
3. Kraken fee estimate said 0.4% taker, actual was 0.8% ($0.05 error). Fetch the
   account-specific schedule (TradeVolume) before the next Kraken API trade.
4. Single resolver endpoint => corroboration UNAVAILABLE; a definitive "not submitted" rests
   on one node. Add a second to SOLANA_RESOLVER_RPC_URLS to close.
5. Dynamic Jito tip-floor sizing (deferred). CLAMP VISIBILITY IS THE LOAD-BEARING PART:
   observed P99 (3,500,047) EXCEEDS the 1,000,000 ceiling, so a clamp would silently deliver a
   tip below the percentile the approval screen claimed.
6. Debt: engine.py:412 single-adapter assumption; per-venue services framework unwired;
   narrow aiohttp catch in BOTH adapters; size_usd = authorized-ceiling not executed notional.
