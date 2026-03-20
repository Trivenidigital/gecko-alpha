Score a single token by contract address using current scorer weights.

Usage: /score <contract_address>

Read the token from scout.db by contract_address, run `score(token, settings)`, and print:
- Each signal: fired or not, points awarded
- Total quant score
- Config thresholds used
