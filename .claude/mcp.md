# MCP Server Install Runbook for gecko-alpha

Run these commands manually in the gecko-alpha project directory to install MCP servers.

## 1. GitHub MCP (official)
Issue tracking, PR automation, commit context.
```bash
claude mcp add github -- npx -y @modelcontextprotocol/server-github
```

## 2. Filesystem MCP
Robust file operations across the project tree.
```bash
claude mcp add filesystem -- npx -y @modelcontextprotocol/server-filesystem ./
```

## 3. SQLite MCP
Direct inspection of scout.db during development.
```bash
claude mcp add sqlite -- npx -y @modelcontextprotocol/server-sqlite scout.db
```

## 4. Sequential Thinking MCP
Complex architectural decisions and signal logic trade-offs.
```bash
claude mcp add sequential-thinking -- npx -y @modelcontextprotocol/server-sequential-thinking
```

## 5. Playwright MCP
Verifying CoinGecko endpoint responses visually if needed.
```bash
claude mcp add playwright -- npx -y @playwright/mcp@latest
```

## Notes
- All servers are project-scoped (run from C:\projects\gecko-alpha)
- GitHub MCP requires GITHUB_TOKEN env var
- SQLite MCP points at scout.db (created on first pipeline run)
