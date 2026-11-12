# Demo script (60-90 seconds)

Record with Windows Game Bar (`Win+G`) or ScreenToGif. VS Code, dark theme, font size up.
Prepare: `uv run poe ingest`, `uv run poe search "check-in time"`, start the MCP server.

1. (0:00) Repo open on the README results table. "A copilot for operations documents that
   knows which version is in force, cites its sources and abstains when it doesn't know."
2. (0:10) MCP Inspector in the browser, connected, Tools > List Tools showing the four tools.
3. (0:20) Run `search_documents` with *What time is check-in at Fogline Marina?* Show the result:
   the renovation memo. "It used the memo valid today, not the default policy."
4. (0:40) In the terminal: `uv run poe ask "What is our parking policy?"`. There is no such
   document, so the copilot abstains instead of inventing one.
5. (0:50) In the Inspector, run `query_kpi`: *RevPAR for Fogline Marina in October 2026 by week.* Show the KPI tool call.
6. (1:05) Open `evals/REPORT.md`: "80 questions, a held-out set, and the numbers are in CI."
7. (1:15) Close on the ADR title. "I chose the model with evidence, and documented what did not work."
