# Autonomous status report (local, read-only)

This report is a **local-only** status surface for Gecko-Alpha autonomous closeout work. It does not require prod access and must not access secrets.

## What it is

- A Markdown snapshot generated from git metadata + repo files (`backlog.md`, `tasks/todo.md`, templates presence).
- Intended for operator use as: “what changed since last run, and what remains gated?”

## What it is not

- Not a production health check.
- Not a DB-backed truth source.
- Not a vendor/probe runner.

## How to run

From repo root:

```bash
node scripts/report_autonomous_status.mjs
```

With an explicit “since” timestamp (ISO 8601):

```bash
node scripts/report_autonomous_status.mjs --since 2026-05-23T16:21:46.603Z
```

Write to a file:

```bash
node scripts/report_autonomous_status.mjs --since 2026-05-23T16:21:46.603Z --out tasks/autonomous_status_report_2026_05_23.md
```

Safety note: `--out` only allows writing to `tasks/*.md` and refuses to overwrite tracked files.

## Interpreting the report

- If `templates missing` appears: create/ship template pack first.
- `Runner candidates` covers this repository only. An empty list does not mean
  the process is manual or has never run; external Codex automations or Hermes
  jobs may exist. A candidate file likewise does not prove activation or success.
- Verify external evidence separately: saved scheduler configuration establishes
  intended scheduling; an observed invocation establishes a started run; verified
  output/PR/CI establishes completion. Memory is historical attestation until
  checked against its cited artifacts. This script does not inspect those systems.
- Backlog anchors are historical extracted headers, not a reconciled work queue.
  Follow the top reconciliation and verify its named successor tracker before
  selecting superseded child work. A missing tracker is a scope-recovery blocker,
  not permission to rebuild the parent.
- `Reference-only mentions` are docs/history/status-surface hits. They prove the closeout was discussed or reported, not that an executable runner exists.
- If backlog anchors show `SHIPPED-MERGED` but UI work is missing: treat as a follow-up item; do not re-implement the shipped primitive.

## Safety constraints (hard gates)

The reporter must remain:
- no network
- no DB access
- no `.env` / secrets reads
- no SSH
- no working-tree writes (except optional `--out` to `tasks/*.md`, and it must not overwrite tracked files)
