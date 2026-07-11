# Dashboard dist build guardrail (DASH-12)

The dashboard SPA is built by vite into `dashboard/frontend/dist/`. The
committed entrypoint `dist/index.html` references content-hashed bundles, e.g.:

```html
<script type="module" crossorigin src="/assets/index-6vj13d1h.js"></script>
<link rel="stylesheet" crossorigin href="/assets/index-BaMtQseu.css">
```

If a rebuild changes a bundle's hash but `index.html` is not re-committed
alongside it (or the new asset is not staged), the deployed page points at a
dead hash and 404s on first load — the 2026-05-12 silent-failure shape. Until
now the only guard was process-only (the "remember to commit dist/" convention +
the `scripts/pre-commit-dist-consistency.sh` pre-commit hook).

## Two layers

1. **Existence check — always runs (CI + local, Windows-safe).**
   `tests/test_dist_build_guardrail.py` parses the `/assets/*` refs out of
   `dist/index.html` and asserts each referenced file exists on disk. This is
   the cheap, node-free half: it catches a stale `index.html` that points at a
   hash no longer present, and a bundle that was deleted/renamed without
   updating `index.html`. It runs anywhere the Python suite runs, including
   `uv run pytest` in CI (`.github/workflows/test.yml`).

2. **Full hash-match — CI-only, node-gated (not wired today).**
   A stronger check reruns the vite build (`npm ci && npm run build`) and
   asserts the freshly-produced asset hashes equal the committed ones — i.e.
   the committed `dist/` is byte-for-byte what the current source produces. This
   requires a Node toolchain. The current CI image (`ubuntu-latest`,
   `astral-sh/setup-uv`) has **no Node step**, so the full rebuild is *not* run
   today; if a Node step is added, extend the guardrail to build and compare
   hashes there. The intended behavior when Node is absent is to **skip
   gracefully** (never fail for lack of a toolchain) — the existence check in
   layer 1 remains the always-on floor.

## What CI does today

- Runs `tests/test_dist_build_guardrail.py` as part of the normal
  `uv run pytest` step → the existence check is enforced on every PR.
- Does **not** rebuild the frontend (no Node in the CI image), so a stale bundle
  whose `index.html` still references valid on-disk hashes is not caught by CI
  yet — that residual is covered at commit time by
  `scripts/pre-commit-dist-consistency.sh` and would be closed fully by adding
  the layer-2 Node step above.
