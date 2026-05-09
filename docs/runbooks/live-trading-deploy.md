# Live Trading Deploy Runbook

**Status:** spec-as-of-2026-05-09 | **Audience:** operator (manual VPS configuration)

This runbook covers the operator-side prerequisites for flipping
`LIVE_MODE='live'` after M1.5a deploys.

## 1. systemd unit hardening (REQUIRED before LIVE_MODE flip)

Per design-stage R2-C1 finding: smoke-check failure under default systemd
config = sub-second restart loop hitting Binance auth at 50+ req/s →
IP-ban risk within minutes. Required `[Service]` block fields:

```ini
[Service]
Restart=on-failure
RestartSec=30s
StartLimitBurst=3
StartLimitIntervalSec=300s
```

**Effect:** on failure, wait 30s before restart. Allow at most 3 restart
attempts in 300s (5 min). Beyond that, systemd marks the service `failed`
and stops trying — operator must manually intervene with
`systemctl reset-failed gecko-pipeline && systemctl start gecko-pipeline`
after fixing root cause.

### Apply via systemctl edit

```bash
sudo systemctl edit gecko-pipeline.service
# In the [Service] block, paste the 4 fields above.
sudo systemctl daemon-reload
sudo systemctl restart gecko-pipeline
```

### Verify

```bash
ssh root@89.167.116.187 \
  'systemctl show gecko-pipeline.service | grep -E "Restart=|RestartSec=|StartLimitBurst=|StartLimitIntervalUSec="'
```

Expected output:
```
Restart=on-failure
RestartSec=30000000
StartLimitBurst=3
StartLimitIntervalUSec=5min
```

## 2. Out-of-band failure notification (REQUIRED — R2-I4)

`gecko-pipeline.service` failure beyond `StartLimitBurst=3` leaves operator
without out-of-band signal (parent unit's Telegram path is dead). Add
`OnFailure=` directive pointing to a one-shot Telegram notify unit.

### `gecko-pipeline.service` addition

```ini
[Unit]
OnFailure=gecko-pipeline-failure-notify.service
```

### `/etc/systemd/system/gecko-pipeline-failure-notify.service` (NEW)

```ini
[Unit]
Description=Telegram notify on gecko-pipeline failure (M1.5a R2-I4)

[Service]
Type=oneshot
EnvironmentFile=/root/gecko-alpha/.env
ExecStart=/bin/bash -c '\
  if [ -n "$TELEGRAM_BOT_TOKEN" ] && [ -n "$TELEGRAM_CHAT_ID" ]; then \
    curl -sS -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
      --data-urlencode "chat_id=${TELEGRAM_CHAT_ID}" \
      --data-urlencode "text=🚨 gecko-pipeline.service FAILED — exhausted StartLimitBurst. Operator action required: investigate root cause, then systemctl reset-failed gecko-pipeline && systemctl start gecko-pipeline." \
      --max-time 10 || true; \
  fi'
```

Reload + verify:

```bash
sudo systemctl daemon-reload
systemctl list-unit-files | grep gecko-pipeline-failure-notify
# Expected: gecko-pipeline-failure-notify.service  static
```

## 3. Binance API key prerequisites

Before flipping `LIVE_MODE='live'`, verify all of:

- [ ] **API key has TRADE permission** (Binance API console → Edit
      Restrictions → Enable Spot & Margin Trading)
- [ ] **VPS IP whitelisted** (Binance API console → Edit Restrictions
      → Restrict access to trusted IPs only → add 89.167.116.187)
- [ ] **Account funded with USDT** for the LIVE_TRADE_AMOUNT_USD cap
      (start small: $100 testnet → $50-100 production)
- [ ] **NTP sync on VPS** (Binance recvWindow=5000ms requires clock
      skew < 5s; `timedatectl` should show `NTP service: active`)

### Pre-flip whitelist verification (R2-I1)

```bash
ssh root@89.167.116.187 \
  'curl -s -H "X-MBX-APIKEY: $(grep BINANCE_API_KEY /root/gecko-alpha/.env | cut -d= -f2)" \
   "https://api.binance.com/api/v3/ping"'
```

Expected: `{}` (status 200). If this fails or returns 4xx, IP whitelist
or DNS is broken before any signed call. Fix BEFORE proceeding.

## 4. .env activation checklist (REQUIRED checklist — R2-I2)

For LIVE_MODE='live' to boot successfully, ALL of these must be set:

- [ ] `LIVE_TRADING_ENABLED=True` (Layer 1 master kill)
- [ ] `LIVE_MODE=live` (Layer 2)
- [ ] `LIVE_USE_REAL_SIGNED_REQUESTS=True` (M1.5a — REQUIRED for
      runtime bodies; default False is the emergency-revert posture)
- [ ] `BINANCE_API_KEY=...` (TRADE-scoped key, IP whitelisted)
- [ ] `BINANCE_API_SECRET=...`
- [ ] At least one signal has `live_eligible=1` in signal_params:
      `sqlite3 /root/gecko-alpha/scout.db "UPDATE signal_params SET live_eligible=1 WHERE signal_type='first_signal';"`
- [ ] systemd unit hardened (§1 above)
- [ ] OnFailure notify unit installed (§2 above)

**M1.5a smoke pass ≠ live ready** (R2-I1): boot succeeds = smoke
passed. Engine routing dispatch + approval gateway wiring + correction
counter increment are M1.5b. Until M1.5b ships, signals will not
actually fire live trades.

## 5. Reversibility (R2-I4 emergency revert)

### Fast revert — `.env` flip (no git)

```bash
# In .env on VPS:
LIVE_USE_REAL_SIGNED_REQUESTS=False
# systemctl restart gecko-pipeline
# Result: 3 ABC runtime bodies fall back to NotImplementedError;
# Gate 10 returns 'live_signed_disabled' reject_reason.
```

### Slower revert — git revert

**Pre-revert checklist (R2 design fold):**
1. **Set `LIVE_MODE='paper'` BEFORE git revert** — otherwise restored
   NotImplementedError will crash next cycle on Gate 10
2. Verify systemctl status post-restart
3. Watch journal for 60s

```bash
# Local:
git revert <squash-merge-of-PR>
git push
# VPS:
ssh root@89.167.116.187 \
  'cd /root/gecko-alpha && git pull && find . -name __pycache__ -exec rm -rf {} +; systemctl restart gecko-pipeline'
```

## 6. Orphaned trade reconciliation (R2-I5 manual runbook)

`live_trades` rows where `place_order_request` succeeded at Binance but
`await_fill_confirmation` timed out leave:
- `entry_order_id IS NOT NULL`
- `status='open'`
- `fill_slippage_bps IS NULL`

M1.5a does NOT auto-reconcile (M1.5b's reconciler does). M1.5a operator
runbook query:

```sql
SELECT id, paper_trade_id, symbol, entry_order_id, created_at
FROM live_trades
WHERE status='open'
  AND fill_slippage_bps IS NULL
  AND entry_order_id IS NOT NULL
  AND created_at < datetime('now', '-10 minutes');
```

Manual remediation per row: query Binance
`GET /api/v3/order?origClientOrderId=...` (signed); read terminal status;
write outcome to live_trades manually.

## 7. M1.5a → M1.5b deferred items

For tracking — these are NOT blockers for M1.5a but DO gate full live
trading capability:

- Engine wiring of `RoutingLayer.get_candidates` (currently bypassed
  for shadow soak; M1.5b enables it under live mode)
- Engine call to `should_require_approval` before adapter dispatch
- `signal_venue_correction_count.consecutive_no_correction` increment
  on close events (V1-C2 closure)
- Recurring health probe (M1.5a smoke is point-in-time only)
- M1.5b's reconciliation worker for orphaned `live_trades` rows
- BL-055 retirement evaluation gate (3 pre-registered conditions in
  `tasks/design_live_trading_hybrid.md` v2.1)

Do NOT flip `live_eligible=1` for any signal under live mode until
M1.5b ships and the BL-055 retirement gate passes.
