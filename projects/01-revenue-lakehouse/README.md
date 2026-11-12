# Revenue Lakehouse

Work in progress. Medallion lakehouse that turns hotel reservation events into trusted revenue KPIs
(`contracts/gold_kpis.yaml` at the repo root is what it must deliver to the Ops Knowledge Copilot).

> All data is synthetic.

## Step 1: source contract and a realistic, hostile source

`uv run poe lh-generate` writes about 14,000 PMS reservation events as daily batches, plus the
*truth* (the real daily KPIs), so every later stage can be tested against the right answer:

| Problem injected | How much | What the pipeline must do |
|---|---|---|
| Late events | ~3% arrive 1 to 5 days late | Resolve by event time, not arrival time |
| Duplicates | ~1.5% delivered twice | Deduplicate by `event_id` |
| Cancellations and modifications | 12% / 8% of reservations | Keep the latest state per reservation |
| New column (`loyalty_tier`), from 2026-03-01 | compatible | Report it, ignore it until the contract adopts it |
| `nightly_rate` renamed to `rate_amount` (string), from 2026-06-15 | breaking | Quarantine the batch before silver |

`uv run poe lh-contracts` checks every batch against `contracts/pms_reservation_events.yaml`.
