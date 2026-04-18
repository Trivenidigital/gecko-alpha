"""Velocity alerter -- CoinGecko 1h early-pump detection.

Research-only: no paper trade dispatch. Consumes cached /coins/markets
data to flag tokens with extreme 1h momentum inside the micro/small-cap
band, dedups per coin-id in a rolling window, and pushes plain-text
Telegram alerts.
"""
