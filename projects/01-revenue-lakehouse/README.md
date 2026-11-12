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

## Step 4: a second source (restaurant POS) through the same framework

`pos_checks` is the restaurant point-of-sale export. Bronze, the batch ledger, the contract check and
the quarantine are the same code as for the PMS: a source is an entry in `sources.py` plus a contract
file. What is new is in silver and gold, because restaurant data has its own traps:

| Trap injected | What goes wrong if ignored | How it is handled |
|---|---|---|
| ~1,100 checks close after midnight | They land on the wrong day | The service day ends at 04:00 local time |
| Timestamps in local time, with a UTC offset that changes with daylight saving | Cutting days at UTC midnight misplaces every evening check (over 10,000 here) | The day is computed in `America/Los_Angeles` |
| ~5,000 corrected and ~1,600 voided checks | Revenue is overstated | The highest revision of each check wins; voids count for nothing |
| ~1.5% duplicated, ~3% late rows | Double counting | Deduplicated by `(check_id, revision)`; order comes from the revision, not the arrival |
| A day with no sales | The day disappears from the table | Gold has a row of zeros |

```
hotel_daily: gold matches the truth on all 730 venue-days
restaurant_daily: gold matches the truth on all 730 venue-days
```

Both gold tables are exported to `data/exports/`, which is exactly what the Ops Knowledge Copilot
reads (`uv run poe mcp-lakehouse`), so the copilot no longer needs its synthetic KPI sample.
