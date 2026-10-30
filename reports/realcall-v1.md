# Matrix results: `results-realcall-v1/`

Lower is better everywhere. Cost is 0 for as good as it gets and 1 for unusable, per traffic entry, summed over a scenario's entries. Intervals are 95% bootstrap intervals of the median over seeds. A comparison reads "verdict: median paired difference (interval), p, r"; negative means the first configuration is better; r is the rank-biserial effect size, -1 when it was better on every seed.

## Runs

| Scenario | O2 |
| --- | --- |
| S12 A real WhatsApp video call, replayed, on a laptop that is also sharing files | 5 |
| S13 A larger real WhatsApp video call, replayed, on a laptop that is also sharing files | 5 |

Valid runs per scenario and configuration. No A/A runs: differences are judged against zero.

## Summed cost, configuration against configuration

| Scenario |  |
| --- |
| S12 |  |
| S13 |  |

O2 against B4 and O2 against O1 were named in advance; the p-values of the others are Holm-corrected.

## Summed cost, median over seeds

| Scenario | O2 |
| --- | --- |
| S12 | 0.29 (0.12 to 0.74) |
| S13 | 1.00 (0.89 to 1.00) |

## Calls and games

| Scenario | Measure | O2 |
| --- | --- | --- |
| S12 | call: late frames % | 6.6 (3.3 to 15.1) |
| S12 | call: latency p95 ms | 154.8 (108.3 to 198.8) |
| S13 | call: late frames % | 30.9 (17.9 to 45.2) |
| S13 | call: latency p95 ms | 517.1 (361.3 to 665.6) |

## What the controller did, mean per run

| Scenario | Configuration | labelled | relabelled | triggers | rechecks | rate_changes | watched | regressed | rolled_back | rejected |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| S12 | O2 | 46.8 | 14.2 | 106.0 | 75.0 | 5.2 | 8.0 | 2.4 | 1.0 | 1.0 |
| S13 | O2 | 46.0 | 13.4 | 74.8 | 82.6 | 5.6 | 7.8 | 2.2 | 1.2 | 0.2 |

## CPU per component, % of one core over the run

| Scenario | Configuration | controller | ml-service | reflector | watchdog |
| --- | --- | --- | --- | --- | --- |
| S12 | O2 | 3.39 | 2.71 | 0.15 | 0.05 |
| S13 | O2 | 2.67 | 2.52 | 0.12 | 0.04 |

Measured on the reference laptop (12th-gen Intel i5, 16 threads), so a router's figures will be several times higher. The ML service is the Go service for O1 and O2 and Python for B4.

