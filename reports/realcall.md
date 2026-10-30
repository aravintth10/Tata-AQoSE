# Matrix results: `results-realcall/`

Lower is better everywhere. Cost is 0 for as good as it gets and 1 for unusable, per traffic entry, summed over a scenario's entries. Intervals are 95% bootstrap intervals of the median over seeds. A comparison reads "verdict: median paired difference (interval), p, r"; negative means the first configuration is better; r is the rank-biserial effect size, -1 when it was better on every seed.

## Runs

| Scenario | B2 | B4 | O1 | O2 |
| --- | --- | --- | --- | --- |
| S12 A real WhatsApp video call, replayed, on a laptop that is also sharing files | 5 | 5 | 5 | 5 |
| S13 A larger real WhatsApp video call, replayed, on a laptop that is also sharing files | 5 | 5 | 5 | 5 |

Valid runs per scenario and configuration. No A/A runs: differences are judged against zero.

## Summed cost, configuration against configuration

| Scenario | O2 against B4 | O2 against O1 | O2 against B2 | O1 against B2 | B4 against B2 |
| --- | --- | --- | --- | --- | --- |
| S12 | too few seeds: -0.93 (-0.93 to -0.92), r -1.00 | too few seeds: +0.00 (-0.00 to +0.01), r +0.60 | too few seeds: -0.93 (-0.93 to -0.92), r -1.00 | too few seeds: -0.93 (-0.93 to -0.93), r -1.00 | too few seeds: +0.00 (+0.00 to +0.00) |
| S13 | too few seeds: -0.29 (-0.32 to -0.29), r -1.00 | too few seeds: +0.01 (+0.01 to +0.05), r +1.00 | too few seeds: -0.29 (-0.32 to -0.29), r -1.00 | too few seeds: -0.30 (-0.37 to -0.30), r -1.00 | too few seeds: +0.00 (+0.00 to +0.00) |

O2 against B4 and O2 against O1 were named in advance; the p-values of the others are Holm-corrected.

## Summed cost, median over seeds

| Scenario | B2 | B4 | O1 | O2 |
| --- | --- | --- | --- | --- |
| S12 | 1.00 (1.00 to 1.00) | 1.00 (1.00 to 1.00) | 0.07 (0.07 to 0.07) | 0.07 (0.07 to 0.08) |
| S13 | 1.00 (1.00 to 1.00) | 1.00 (1.00 to 1.00) | 0.70 (0.63 to 0.70) | 0.71 (0.68 to 0.71) |

## Calls and games

| Scenario | Measure | B2 | B4 | O1 | O2 |
| --- | --- | --- | --- | --- | --- |
| S12 | call: late frames % | 57.0 (49.4 to 60.4) | 42.7 (37.0 to 49.2) | 2.3 (2.2 to 2.4) | 2.4 (2.3 to 2.4) |
| S12 | call: latency p95 ms | 99.8 (92.8 to 124.6) | 212.9 (142.7 to 235.0) | 79.4 (79.2 to 80.3) | 79.4 (78.3 to 80.3) |
| S13 | call: late frames % | 78.7 (76.5 to 79.8) | 97.3 (75.9 to 98.7) | 14.2 (13.0 to 14.3) | 14.4 (14.0 to 14.4) |
| S13 | call: latency p95 ms | 1,706.7 (1,396.5 to 1,867.1) | 7,442.9 (3,581.9 to 7,812.3) | 223.3 (211.3 to 225.0) | 224.0 (220.8 to 228.0) |

## What the controller did, mean per run

| Scenario | Configuration | labelled | relabelled | triggers | rechecks | rate_changes | watched | regressed | rolled_back | rejected |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| S12 | B4 | 52.8 | 20.2 | 0.0 | 0.0 | 0.0 | 4.8 | 0.0 | 0.0 | 1.0 |
| S12 | O1 | 43.4 | 10.8 | 97.2 | 77.0 | 6.4 | 6.0 | 0.4 | 0.4 | 0.0 |
| S12 | O2 | 42.8 | 10.2 | 103.8 | 76.6 | 4.8 | 4.4 | 0.0 | 0.0 | 0.0 |
| S13 | B4 | 52.6 | 20.0 | 0.0 | 0.0 | 0.0 | 2.8 | 0.0 | 0.0 | 2.6 |
| S13 | O1 | 44.0 | 11.4 | 59.8 | 86.8 | 9.2 | 8.2 | 3.8 | 1.6 | 0.0 |
| S13 | O2 | 42.8 | 10.2 | 56.2 | 88.2 | 11.2 | 8.2 | 3.2 | 1.8 | 0.0 |

## CPU per component, % of one core over the run

| Scenario | Configuration | controller | ml-service | reflector | watchdog |
| --- | --- | --- | --- | --- | --- |
| S12 | B4 | 2.08 | 1.29 |  | 0.05 |
| S12 | O1 | 2.77 | 0.04 | 0.12 | 0.04 |
| S12 | O2 | 3.14 | 0.05 | 0.15 | 0.05 |
| S13 | B4 | 2.10 | 1.29 |  | 0.05 |
| S13 | O1 | 3.27 | 0.05 | 0.15 | 0.05 |
| S13 | O2 | 3.43 | 0.05 | 0.15 | 0.05 |

Measured on the reference laptop (12th-gen Intel i5, 16 threads), so a router's figures will be several times higher. The ML service is the Go service for O1 and O2 and Python for B4.

