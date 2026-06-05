# Trader Decision Cockpit Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a compact read-only trade decision board that turns crowded trader surfaces into a scarce "act / watch / avoid" cockpit.

**Architecture:** Keep existing backend contracts unchanged. Add a pure frontend decision helper that consumes `/api/trade_inbox` rows, ranks at most one immediate row plus a few watch/late rows, and wire it into the Trade Inbox tab as the first visual surface.

**Tech Stack:** React 19, Vite, Python static/Node helper tests, existing FastAPI SQLite dashboard endpoints.

---

### Task 1: Decision Helper

**Files:**
- Create: `dashboard/frontend/components/tradeDecisionBoard.js`
- Test: `tests/test_trade_decision_board_frontend.py`

- [ ] Write failing Node-driven tests for `buildTradeDecisionBoard(payload)`.
- [ ] Verify the tests fail because the helper module does not exist.
- [ ] Implement helper functions for strict row eligibility, risk demotion, late-runner quarantine, and headline copy.
- [ ] Re-run the helper tests and confirm they pass.
- [ ] Commit the helper and tests.

### Task 2: Trade Inbox UI

**Files:**
- Modify: `dashboard/frontend/components/TradeInboxTab.jsx`
- Modify: `dashboard/frontend/style.css`
- Test: `tests/test_dashboard_frontend_layout.py`

- [ ] Add failing static layout tests proving the new board is imported, rendered before grouped tables, caps active rows, and keeps blocked/late rows out of the main decision area.
- [ ] Verify those tests fail against the current JSX/CSS.
- [ ] Render the board above the grouped Trade Inbox panels.
- [ ] Add compact CSS classes for the board, decision cards, meta chips, and mobile layout.
- [ ] Re-run layout tests and helper tests.
- [ ] Commit the UI and CSS.

### Task 3: Build, Review, Deploy

**Files:**
- Modify after build: `dashboard/frontend/dist/**`
- Modify: `tasks/todo.md`

- [ ] Build the Vite dist bundle from the worktree, using the existing local node modules if dependency install is blocked.
- [ ] Run focused backend and frontend verification.
- [ ] Run Claude Code review over the branch diff.
- [ ] Fold any critical or important findings and re-run verification.
- [ ] Update `tasks/todo.md` with verification and review results.
- [ ] Commit final dist and task-log updates.
- [ ] Merge to `master`, push, deploy to srilu, restart `gecko-dashboard`, and smoke `/api/trade_inbox` plus the served dashboard.
