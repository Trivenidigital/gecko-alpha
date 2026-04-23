# Live Mode Setup — DO NOT COMMIT real credentials

> **WARNING**: Never paste a real `BINANCE_API_KEY` or `BINANCE_API_SECRET`
> into `.env.example`, this document, or any file tracked by git. Credentials
> live only in the VPS `.env` (which is gitignored). If a key is ever
> committed: revoke it on Binance immediately, then rotate.

Operator runbook for BL-055 live trading. Read spec
`docs/superpowers/specs/2026-04-22-bl055-live-trading-execution-core-design.md`
end-to-end before flipping `LIVE_MODE` off `paper`.

---

## Binance API Key Creation

Generate the API key pair **only after** §11.8 soak-test passes.

1. Binance → API Management → Create API.
2. **Label**: `gecko-alpha-vps-<date>` so rotation history is obvious.
3. **Permissions** (unchecked boxes are non-negotiable):
   - [x] Enable Reading
   - [x] Enable Spot & Margin Trading (spot only — we never touch margin)
   - [ ] Enable Withdrawals — MUST stay off
   - [ ] Enable Margin — MUST stay off
   - [ ] Enable Futures — MUST stay off
   - [ ] Universal Transfer — MUST stay off
4. **IP access restrictions**: "Restrict access to trusted IPs only" →
   add `89.167.116.187` (VPS). No other IPs. No unrestricted keys, ever.
5. Copy key + secret straight into the VPS `.env`. Do not email, Slack,
   paste into a local file, or commit. Close the browser tab.

If any of the unchecked boxes above gets enabled by mistake, delete the
key and start over — do not edit permissions in place.

---

## Pre-flight with `--check-config`

Run on the VPS every time `.env` changes, BEFORE generating credentials
and BEFORE restarting the pipeline:

```bash
uv run python -m scout.main --check-config
```

Output is the fully-resolved `LiveConfig` surface: `mode`, allowlist set,
sizing map, resolved TP/SL/duration (after PAPER_* fallback), exposure
caps. Read each line against your intent:

- `mode` matches what you expect (`paper` / `shadow` / `live`).
- `live_signal_allowlist_set` only contains signals you mean to trade.
- `live_signal_sizes_map` parses cleanly (typos raise immediately thanks
  to `extra="forbid"`; a clean run means the CSV was valid).
- Resolved TP/SL/duration match either your `LIVE_*` override or the
  inherited `PAPER_*` value — no surprises.

If anything looks off, fix `.env` and re-run. Never restart with unverified
config.

---

## Restart, not reload (spec §10.6)

`LiveConfig` reads `Settings` once at process start. Editing `.env` has
**zero effect** on a running pipeline until you restart it.

```bash
# Correct:
sudo systemctl restart gecko-pipeline

# WRONG — does nothing for config changes:
sudo systemctl reload gecko-pipeline
```

After restart, tail logs and confirm `live_boot_reconciliation_done`
fires (it always fires, even with zero open shadow rows — absence means
the engine did not come up clean; see spec §10.5).

---

## Flip-to-live checklist

Do not freehand this. Follow spec §11.8 line by line:
`docs/superpowers/specs/2026-04-22-bl055-live-trading-execution-core-design.md`.

Order matters — config is verified BEFORE credentials are generated (F1),
and `LIVE_TRADE_AMOUNT_USD=10` with a single-signal allowlist are the
initial live-day settings. Other signals are enabled one-at-a-time with
a 48h hold between each.
