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

## Interpreting the report

- If “templates missing” appears: create/ship template pack first.
- If “work loop runner not found” appears: treat overnight closeout as a **manual** process until a scheduler integration is explicitly designed and operator-approved.
- If backlog anchors show “SHIPPED-MERGED” but UI work is missing: treat as a follow-up item; do not re-implement the shipped primitive.

## Safety constraints (hard gates)

The reporter must remain:
- no network
- no DB access
- no `.env` / secrets reads
- no SSH
- no working-tree writes (except optional `--out`)

