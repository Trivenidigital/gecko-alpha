"""BL-064 — TG Social Signals.

Telethon-based MTProto user-session listener subscribing to N curated
Telegram channels. Parses cashtags + contract addresses + DEX/explorer
URLs. Alerts always (with two-tier provenance UX); paper-trades via
TradingEngine when CA-resolved + admission gates pass.

Default OFF behind TG_SOCIAL_ENABLED. See:
    docs/superpowers/specs/2026-04-27-bl064-tg-social-signals-design.md
"""
