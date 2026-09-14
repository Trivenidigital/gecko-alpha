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

2. **Fresh rebuild parity — independent CI job.**
   `frontend-dist-parity` installs Node 24.14.0 and uses `npm ci --ignore-scripts
   --no-audit --no-fund` with the existing lockfile. The ordinary Vite build emits
   into a fresh directory outside the checkout. The standard-library comparator
   `scripts/check_dist_rebuild_parity.py` compares every emitted file's bytes to
   immutable HEAD Git blobs: index, directly referenced assets, lazy chunks and
   copied public files. Extra committed historical assets are permitted.
   Missing tooling, install/build errors and mismatches fail this job; local
   Python tests still require no Node.

## What CI does today

- Runs `tests/test_dist_build_guardrail.py` as part of the normal
  `uv run pytest` step → the existence check is enforced on every PR.
- Rebuilds in the independent parity job on the same push/PR events. PRs test
  GitHub's synthetic merge checkout; logs include actual HEAD, Node/npm versions
  and lockfile SHA256. Success identifies the compared SHA and file count.

## Diagnosing a parity failure

`byte_mismatch` means a generated file differs despite its committed name;
`missing_committed_file` means a fresh file is absent from Git. Rebuild with the
pinned toolchain, review the source/output change, and commit the intended index
and assets together. Do not delete historical unreferenced assets to satisfy this
check. Do not mask failures or update the lockfile just to change the result.

For manual validation, use a clean committed checkout and a fresh output directory
outside it, then run `python scripts/check_dist_rebuild_parity.py --repo-root .
--build-dir /absolute/fresh/output`. A preexisting directory by itself provides no
source provenance: the CI job establishes this by building first. Dirty tracked
files, checkout changes, unsafe output entries and invalid references fail closed.
The checker never edits or deletes files, and parity does not prove browser or
production behavior.
