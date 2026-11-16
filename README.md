# Fogline Data & AI Platform

End-to-end data and AI platform for Fogline Hospitality Group, a fictional
hospitality and retail operator in San Francisco. Three projects, one platform.

| # | Project | What it proves | Status |
|---|---------|----------------|--------|
| 01 | [Revenue Lakehouse](projects/01-revenue-lakehouse) | Contract-checked Bronze/Silver/Gold on Delta Lake + DuckDB over two sources (PMS, POS), quarantine and replay, incremental Silver with a [measured benchmark](projects/01-revenue-lakehouse/BENCHMARK.md), Gold verified against generator ground truth | In progress |
| 02 | [Ops Knowledge Copilot](projects/02-ops-knowledge-copilot) | Hybrid retrieval, grounded answers with citations, MCP server, held-out [evals](projects/02-ops-knowledge-copilot/evals/REPORT.md), CI quality gates | Done |
| 03 | Agentic Commerce Gateway | Agent-ready catalog with spend policies | Not started |

## Quickstart

Developed and tested on Python 3.12 (pinned in `.python-version`).

    uv sync --all-packages
    uv run poe check

Fogline is fictional. Synthetic data is always labeled as synthetic.