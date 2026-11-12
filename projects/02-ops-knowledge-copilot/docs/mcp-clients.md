# Connect the MCP server to a client

Tested with: **MCP Inspector** (see below). The VS Code and Claude Desktop sections are standard
configurations that I could not test on my machine (restricted marketplace and app settings).

## MCP Inspector (tested)

Needs Node.js. On a network that inspects HTTPS, set `$env:NODE_OPTIONS = "--use-system-ca"` first.

```powershell
npx @modelcontextprotocol/inspector uv run python -m ops_copilot.mcp_server
```

Connect, open **Tools > List Tools**, and run `search_documents` with
`query = "What time is check-in at Fogline Marina?"`. Run it again with `as_of = 2026-12-15`:
the renovation memo (valid until 2026-11-30) no longer appears.

The server speaks MCP over stdio. Before the first connection, build the data and warm the
index once, so the client does not time out while the embedding model loads:

```powershell
uv run poe ingest
uv run poe search "check-in time"
```

## VS Code (GitHub Copilot agent mode, untested)

The repo already includes `.vscode/mcp.json`. Open the repo folder in VS Code, open
`.vscode/mcp.json`, click **Start** above the server name, then open Copilot Chat in **Agent**
mode and check the tools icon lists `fogline-ops-copilot`.

## Claude Desktop (untested)

Edit `%APPDATA%\Claude\claude_desktop_config.json` (create it if missing):

```json
{
  "mcpServers": {
    "fogline-ops-copilot": {
      "command": "uv",
      "args": [
        "run",
        "--directory",
        "C:\\path\\to\\fogline-data-ai-platform",
        "python",
        "-m",
        "ops_copilot.mcp_server"
      ]
    }
  }
}
```

Replace the path with your clone, fully quit Claude Desktop (system tray too) and reopen it.

## Questions that show what it does

| Ask | What it shows |
|---|---|
| "What time is check-in at Fogline Marina?" | Date-aware: the autumn memo overrides the default time |
| "Which days does Saltgrass get its seafood deliveries?" | Newer memo vs older contract |
| "What was the housekeeping standard on 2025-06-01?" | Past `as_of`: older version of the procedure |
| "What is our parking policy?" | No document: the client should say it found nothing |
| "RevPAR for Fogline Marina in October 2026 by week" | KPI tool with fixed SQL |
