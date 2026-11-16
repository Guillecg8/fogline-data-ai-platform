# Demand forecast backtest: LightGBM vs seasonal baselines

Measured 2026-11-16 on Linux x86_64, 22 CPUs, Python 3.12.16, LightGBM 4.7.0, NumPy 2.5.4.

Walk-forward, expanding window: 33 origins from 2026-03-01 to 2026-10-11 (every 7 days), horizon 14 days. Lift is the relative WAPE reduction of the model against the best baseline per series (positive = model better), with a 95% block bootstrap interval over origins (2000 replicates, best baseline re-selected in each). The headline interval is the wider block size.

## Hotel occupancy

| Series | WAPE last same weekday | WAPE mean of 4 | WAPE model | MAE model | Best baseline | Lift vs best [95% CI] | Lift vs mean of 4 | Origins model wins |
|---|---|---|---|---|---|---|---|---|
| fogline-marina/occupancy | 12.57% | 10.60% | 11.42% | 0.08722 | mean4_same_weekday | -7.7% [-17.2%, +1.2%] (block 4; block 2: [-15.6%, +0.4%]) | -7.7% | 30% |
| fogline-mission/occupancy | 14.36% | 12.50% | 13.69% | 0.1015 | mean4_same_weekday | -9.6% [-20.8%, -1.0%] (block 2; block 4: [-19.6%, -2.1%]) | -9.6% | 39% |
| **pooled** | 13.45% | 11.54% | 12.54% | 0.09436 | mean4_same_weekday | -8.7% [-17.0%, -1.7%] (block 4; block 2: [-16.8%, -1.9%]) | -8.7% | 33% |

Pooled WAPE weights each series by its share of actual volume (fogline-marina/occupancy 51%, fogline-mission/occupancy 49%): fogline-marina/occupancy counts 1.0 times as much as fogline-mission/occupancy.

By horizon (pre-declared, descriptive, no interval; best baseline as chosen above):

| Series | Best baseline days 1-7 | Model days 1-7 | Best baseline days 8-14 | Model days 8-14 |
|---|---|---|---|---|
| fogline-marina/occupancy | 10.32% | 11.29% | 10.88% | 11.54% |
| fogline-mission/occupancy | 12.28% | 13.36% | 12.72% | 14.03% |

## Restaurant covers

| Series | WAPE last same weekday | WAPE mean of 4 | WAPE model | MAE model | Best baseline | Lift vs best [95% CI] | Lift vs mean of 4 | Origins model wins | Floor ref WAPE | Floor bias |
|---|---|---|---|---|---|---|---|---|---|---|
| lumen/covers | 10.76% | 8.71% | 8.68% | 28.05 | mean4_same_weekday | +0.3% [-5.0%, +6.5%] (block 4; block 2: [-5.2%, +6.1%]) | +0.3% | 52% | 7.63% | -2.55 |
| saltgrass/covers | 10.45% | 9.19% | 9.07% | 13.33 | mean4_same_weekday | +1.3% [-3.6%, +8.7%] (block 4; block 2: [-5.1%, +7.2%]) | +1.3% | 48% | 7.00% | +2.11 |
| **pooled** | 10.66% | 8.86% | 8.80% | 20.69 | mean4_same_weekday | +0.6% [-3.7%, +5.1%] (block 2; block 4: [-2.7%, +5.4%]) | +0.6% | 48% | 7.43% | -0.22 |

Pooled WAPE weights each series by its share of actual volume (lumen/covers 69%, saltgrass/covers 31%): lumen/covers counts 2.2 times as much as saltgrass/covers.

By horizon (pre-declared, descriptive, no interval; best baseline as chosen above):

| Series | Best baseline days 1-7 | Model days 1-7 | Best baseline days 8-14 | Model days 8-14 |
|---|---|---|---|---|
| lumen/covers | 8.94% | 8.81% | 8.48% | 8.56% |
| saltgrass/covers | 8.86% | 8.83% | 9.52% | 9.30% |

Without the 4 origins whose horizon reaches the Saltgrass Sunday closure (2026-10-04; origins 323, 330, 337, 344). Pre-declared; the headline numbers above keep all origins.

| Series | WAPE last same weekday, all / without | WAPE mean of 4, all / without | WAPE model, all / without |
|---|---|---|---|
| lumen/covers | 10.76% / 10.75% | 8.71% / 8.85% | 8.68% / 8.89% |
| saltgrass/covers | 10.45% / 9.99% | 9.19% / 8.21% | 9.07% / 7.86% |
| **pooled** | 10.66% / 10.51% | 8.86% / 8.65% | 8.80% / 8.56% |

Floor reference (restaurants only): the generator's mean covers net of voids. It is not a forecast any method could make; bias is actual minus reference, in covers per day.

Run it again with `uv run poe lh-all` then `uv run poe lh-backtest`.
