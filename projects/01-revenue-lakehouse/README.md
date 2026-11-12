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

## Step 2: bronze, with a ledger and a quarantine you can replay

`uv run poe lh-ingest` loads the batches into Delta tables:

- `bronze/pms_reservation_events`: every accepted line, untouched (`_payload`), plus load metadata
  (file, hash, line number, load time, contract version, row-level errors). Because the raw line is
  kept, a schema change at the source cannot break this layer.
- `ops/batch_ledger`: one entry per decision about a batch (loaded, or quarantined and why).
- It is idempotent (a second run loads nothing), a loaded file that changes is an error, and if a
  run stops between the two writes the next run repairs the ledger.

The demo of what a data contract is for:

```powershell
uv run poe lh-ingest --contract 1   # 283 batches loaded, 139 quarantined (nightly_rate is gone)
uv run poe lh-ingest                # contract 2 published: the 139 are replayed and loaded
```

## Step 3: silver, gold, and a proof that the numbers are right

```powershell
uv run poe lh-all   # generate, ingest, silver, gold, verify
```

- **Silver** (`lh-silver`): parses the raw lines, removes duplicates, converts both spellings of the
  rate to one decimal, and keeps the latest state of each reservation *by event time*, so a late
  event never overwrites something newer. Invalid lines go to `silver/rejected_rows` with the reason.
- **Gold** (`lh-gold`): `hotel_daily`, one row per hotel per day, checked against
  `contracts/gold_kpis.yaml` (columns, types and every row check) before it is published. If the
  check fails, nothing is written.
- **Verify** (`lh-verify`): because the data is synthetic, the generator also writes what the KPIs
  really were. Gold must match on every hotel-day:

```
gold matches the truth on all 730 hotel-days
```

The same check shows what a quarantined batch costs: with contract v1 only, gold has a visible
hole from mid-June and `lh-verify` fails.

The code avoids `pyarrow.parquet` and `pyarrow.dataset`, which some locked-down Windows machines
block (a test runs the whole pipeline with both disabled).
