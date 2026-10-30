# Evidence for the problem statement's metrics

From `results-final` (frozen system, seeds 0 to 9) and `results-fresh` (fresh seeds 10 to 14). Medians over seeds. No new runs: everything is computed from the runs' per-second metrics.

## Bulk progress and starvation

Throughput of every bulk entry, and its throughput under AQoSE as a share of the same entry under plain CAKE on the same seed. A starved second is one in which the transfer got under 100 kbit/s.

| Scenario | Bulk entry | CAKE, Mbit/s | Rules, Mbit/s | AQoSE, Mbit/s | AQoSE / CAKE, median (lowest) | Starved seconds, worst run: AQoSE / CAKE |
| --- | --- | --- | --- | --- | --- | --- |
| S1 | download (down) | 88.46 | 88.53 | 88.45 | 100% (100%) | 0.7% / 0.7% |
| S2 | download (down) | 37.82 | 37.85 | 36.94 | 97% (97%) | 0.7% / 0.7% |
| S4 | backup (up) | 8.92 | 8.94 | 8.92 | 100% (100%) | 0.7% / 0.7% |
| S5 | backup (up) | 2.78 | 3.29 | 2.28 | 83% (54%) | 0.6% / 0.6% |
| S6 | download (down) | 43.76 | 42.39 | 41.70 | 95% (94%) | 0.6% / 0.6% |
| S6 | sharing (down) | 40.69 | 40.32 | 42.41 | 104% (103%) | 0.6% / 0.6% |
| S6 | sharing (up) | 14.57 | 14.54 | 10.90 | 75% (75%) | 0.6% / 0.6% |
| S7 | update (down) | 66.23 | 66.75 | 64.96 | 98% (96%) | 0.7% / 0.7% |
| S8 | share (down) | 52.71 | 54.62 | 53.04 | 101% (100%) | 0.6% / 0.6% |
| S8 | share (up) | 14.67 | 14.64 | 14.35 | 98% (97%) | 0.6% / 0.6% |
| S8 | update (down) | 31.10 | 31.39 | 30.61 | 98% (98%) | 25.6% / 25.6% |
| S10 | download (down) | 88.45 | 88.47 | 79.15 | 90% (89%) | 0.7% / 0.7% |

Fresh seeds:

| Scenario | Bulk entry | CAKE, Mbit/s | AQoSE, Mbit/s | AQoSE / CAKE, median (lowest) | Starved seconds, worst run: AQoSE / CAKE |
| --- | --- | --- | --- | --- | --- |
| S1 | download (down) | 87.84 | 87.89 | 100% (100%) | 0.7% / 0.7% |
| S2 | download (down) | 37.68 | 36.86 | 98% (97%) | 0.7% / 0.7% |
| S4 | backup (up) | 8.90 | 8.90 | 100% (100%) | 0.7% / 0.7% |
| S5 | backup (up) | 2.87 | 2.66 | 93% (68%) | 0.6% / 0.6% |
| S6 | download (down) | 43.98 | 41.79 | 95% (94%) | 0.6% / 0.6% |
| S6 | sharing (down) | 40.54 | 42.32 | 104% (103%) | 0.6% / 0.6% |
| S6 | sharing (up) | 14.58 | 10.91 | 75% (75%) | 0.6% / 0.6% |
| S7 | update (down) | 68.27 | 67.78 | 99% (94%) | 0.7% / 0.7% |
| S8 | share (down) | 52.99 | 53.39 | 100% (100%) | 0.6% / 0.6% |
| S8 | share (up) | 14.82 | 14.71 | 100% (96%) | 0.6% / 0.6% |
| S8 | update (down) | 31.51 | 31.12 | 98% (98%) | 25.6% / 25.6% |
| S10 | download (down) | 87.84 | 78.98 | 90% (89%) | 0.7% / 0.7% |
| S11 | backup (up) | 5.92 | 6.24 | 105% (105%) | 0.9% / 0.9% |
| S11 | download (down) | 20.21 | 21.08 | 103% (101%) | 0.6% / 0.6% |
| S11 | update (down) | 19.84 | 18.87 | 96% (94%) | 0.8% / 0.8% |

## Fairness between devices

Jain's index over the devices' mean throughput in one direction (1 is perfectly even). Devices with light traffic lower it under any scheduler, so the comparison that matters is CAKE against AQoSE.

| Scenario | Direction | Devices | Plain CAKE | AQoSE |
| --- | --- | --- | --- | --- |
| S1 | down | 2 | 0.518 | 0.518 |
| S2 | down | 2 | 0.542 | 0.544 |
| S3 | down | 4 | 0.743 | 0.743 |
| S4 | up | 2 | 0.507 | 0.507 |
| S5 | down | 5 | 0.708 | 0.708 |
| S5 | up | 2 | 0.516 | 0.516 |
| S6 | down | 2 | 0.999 | 0.996 |
| S7 | down | 2 | 0.578 | 0.568 |
| S8 | down | 3 | 0.701 | 0.697 |
| S8 | up | 2 | 0.503 | 0.503 |
| S10 | down | 2 | 0.518 | 0.520 |
| S1 (fresh) | down | 2 | 0.520 | 0.520 |
| S2 (fresh) | down | 2 | 0.546 | 0.547 |
| S3 (fresh) | down | 4 | 0.738 | 0.737 |
| S4 (fresh) | up | 2 | 0.509 | 0.509 |
| S5 (fresh) | down | 5 | 0.709 | 0.709 |
| S5 (fresh) | up | 2 | 0.520 | 0.520 |
| S6 (fresh) | down | 2 | 0.999 | 0.996 |
| S7 (fresh) | down | 2 | 0.559 | 0.552 |
| S8 (fresh) | down | 3 | 0.684 | 0.681 |
| S8 (fresh) | up | 2 | 0.503 | 0.503 |
| S10 (fresh) | down | 2 | 0.520 | 0.522 |
| S11 (fresh) | down | 4 | 0.595 | 0.593 |
| S11 (fresh) | up | 3 | 0.494 | 0.487 |

## Calls and games: jitter, loss, throughput

| Scenario | Flow | Jitter ms: CAKE / rules / AQoSE | Loss %: CAKE / rules / AQoSE | Throughput kbit/s: CAKE / AQoSE |
| --- | --- | --- | --- | --- |
| S1 | call down | 0.1 / 0.1 / 0.1 | 0.00 / 0.00 / 0.00 | 1,614 / 1,614 |
| S1 | call up | 0.4 / 0.4 / 0.4 | 0.00 / 0.00 / 0.00 | 1,790 / 1,790 |
| S2 | call down | 0.5 / 0.5 / 0.9 | 0.29 / 0.32 / 0.00 | 1,604 / 1,614 |
| S2 | call up | 0.3 / 0.3 / 0.3 | 0.00 / 0.00 / 0.00 | 1,790 / 1,790 |
| S3 | game down | 0.2 / 0.3 / 0.2 | 0.00 / 0.00 / 0.00 | 80 / 80 |
| S3 | game up | 0.0 / 0.1 / 0.1 | 0.00 / 0.00 / 0.00 | 64 / 64 |
| S4 | game down | 0.1 / 0.1 / 0.1 | 0.00 / 0.00 / 0.00 | 80 / 80 |
| S4 | game up | 0.5 / 0.5 / 0.5 | 0.00 / 0.00 / 0.00 | 64 / 64 |
| S5 | call down | 0.7 / 0.7 / 0.7 | 0.00 / 0.00 / 0.00 | 1,614 / 1,614 |
| S5 | call up | 8.0 / 15.3 / 3.0 | 23.99 / 53.56 / 0.01 | 1,324 / 1,790 |
| S5 | game down | 1.1 / 2.4 / 1.1 | 0.00 / 0.00 / 0.00 | 80 / 80 |
| S5 | game up | 1.0 / 0.9 / 0.9 | 0.00 / 0.00 / 0.00 | 64 / 64 |
| S6 | call down | 27.3 / 3.2 / 0.2 | 91.05 / 52.80 / 0.00 | 432 / 5,051 |
| S6 | call up | 6.6 / 8.7 / 0.6 | 65.39 / 69.72 / 0.03 | 1,530 / 5,047 |
| S7 | game down | 3.1 / 0.1 / 0.3 | 78.92 / 16.74 / 0.01 | 3,914 / 18,982 |
| S7 | game up | 0.0 / 0.0 / 0.0 | 0.00 / 0.00 / 0.00 | 78 / 78 |
| S8 | call down | 2.5 / 4.0 / 0.1 | 0.04 / 8.24 / 0.00 | 1,182 / 1,183 |
| S8 | call up | 6.6 / 8.7 / 0.5 | 12.15 / 6.24 / 0.00 | 1,016 / 1,308 |
| S8 | game down | 0.3 / 0.3 / 0.2 | 0.00 / 0.00 / 0.00 | 51 / 51 |
| S8 | game up | 0.3 / 0.2 / 0.2 | 0.00 / 0.00 / 0.00 | 41 / 41 |
| S10 | call down | 0.1 / 0.1 / 0.1 | 0.00 / 0.00 / 0.00 | 1,614 / 1,614 |
| S10 | call up | 0.4 / 0.4 / 0.4 | 0.00 / 0.00 / 0.00 | 1,790 / 1,790 |

## Link estimation (S2)

The line drops from 100 to 20 Mbit/s at 100 s. Detection: from the drop to the first cut. Under the line: from the drop to the first second the shaper is at or below the new capacity. Error: mean absolute difference from the true capacity from 10 s after the drop, as a share of it.

| | Runs | Detection, s | Under the line after, s | Steady-state error | Time above the line |
| --- | --- | --- | --- | --- | --- |
| AQoSE | 10 | 1.0 | 2.0 | 6.2% | 2% |
| cake-autorate | 10 | 1.0 | 3.0 | 20.5% | 76% |

Plain CAKE stays at its fixed 95 Mbit/s, 375% above the line after the drop.

## Dashboard

`dashboard-S5.png`: one run of S5, plain CAKE against AQoSE.

