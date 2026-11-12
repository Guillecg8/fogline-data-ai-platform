# Silver: full rebuild vs incremental MERGE

Measured 2026-11-12 on Windows AMD64, 22 CPUs, Python 3.12.15, DuckDB 2.0.0, deltalake 1.6.6.

Time for silver to absorb one more daily batch (both sources), median of 3 runs. Both strategies were checked to produce identical tables.

| History | Bronze rows | Full rebuild | Incremental MERGE | Speed-up |
|---|---|---|---|---|
| 30 days | 7,842 | 0.19 s | 0.24 s | 0.8x |
| 90 days | 25,251 | 0.24 s | 0.27 s | 0.9x |
| 180 days | 50,525 | 0.35 s | 0.30 s | 1.1x |
| 360 days | 102,150 | 0.54 s | 0.33 s | 1.6x |

Run it again with `uv run poe lh-generate` then `uv run poe lh-benchmark`.
