# Matrix results: `results-ipv6/`

Lower is better everywhere. Cost is 0 for as good as it gets and 1 for unusable, per traffic entry, summed over a scenario's entries. Intervals are 95% bootstrap intervals of the median over seeds. A comparison reads "verdict: median paired difference (interval), p, r"; negative means the first configuration is better; r is the rank-biserial effect size, -1 when it was better on every seed.

## Runs

| Scenario | B2 | O2 |
| --- | --- | --- |
| S14 HD video call on a laptop that is also sharing files, all over IPv6 | 3 | 3 |

Valid runs per scenario and configuration. No A/A runs: differences are judged against zero.

## Summed cost, configuration against configuration

| Scenario | O2 against B2 |
| --- | --- |
| S14 | too few seeds: -1.00 (-1.00 to -1.00), r -1.00 |

O2 against B4 and O2 against O1 were named in advance; the p-values of the others are Holm-corrected.

## Summed cost, median over seeds

| Scenario | B2 | O2 |
| --- | --- | --- |
| S14 | 1.00 (1.00 to 1.00) | 0.00 (0.00 to 0.00) |

## Calls and games

| Scenario | Measure | B2 | O2 |
| --- | --- | --- | --- |
| S14 | call: late frames % | 94.3 (94.3 to 94.3) | 0.3 (0.1 to 0.6) |
| S14 | call: latency p95 ms | 1,788.1 (1,783.8 to 1,926.7) | 18.8 (18.4 to 19.3) |

## What the controller did, mean per run

| Scenario | Configuration | labelled | relabelled | triggers | rechecks | rate_changes | watched | regressed | rolled_back | rejected |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| S14 | O2 | 43.0 | 10.3 | 124.3 | 70.3 | 3.3 | 3.7 | 0.0 | 0.0 | 0.0 |

## CPU per component, % of one core over the run

| Scenario | Configuration | controller | ml-service | reflector | watchdog |
| --- | --- | --- | --- | --- | --- |
| S14 | O2 | 3.45 | 0.05 | 0.24 | 0.05 |

Measured on the reference laptop (12th-gen Intel i5, 16 threads), so a router's figures will be several times higher. The ML service is the Go service for O1 and O2 and Python for B4.

