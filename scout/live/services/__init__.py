"""BL-NEW-LIVE-HYBRID M1 v2.1: per-venue services framework.

Modules:
- base.py — VenueService ABC + concurrency contract
- runner.py — service-runner harness (asyncio.Lock per (adapter, service))
- health_probe.py — HealthProbe writes venue_health probe snapshot
- balance_snapshot.py — BalanceSnapshot writes wallet_snapshots row
- rate_limit_stub.py — conservative 50% headroom stub
- dormancy.py — DormancyJob flags zero-fill venues daily

Per design v2.1: services run in parallel across venues, serialized
per (adapter, service-class) pair. M1 ships the framework + 3 workers
+ stub + dormancy job. M2 fills in real RateLimitAccountant + adds
ReconciliationWorker.
"""
