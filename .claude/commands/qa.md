Run full acceptance criteria check against the PRD.

Invoke the qa-agent to:
1. Run `uv run pytest --tb=short -q` and check for 0 failures
2. Run `uv run python -m scout.main --dry-run --cycles 1` and check for CG candidates
3. Verify all 8 acceptance criteria (AC-01 through AC-08)
4. Report pass/fail per criterion

Use the qa-agent defined in .claude/agents/qa-agent.md.
