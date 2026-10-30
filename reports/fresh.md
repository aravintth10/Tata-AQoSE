# Matrix results: `results-fresh/`

Lower is better everywhere. Cost is 0 for as good as it gets and 1 for unusable, per traffic entry, summed over a scenario's entries. Intervals are 95% bootstrap intervals of the median over seeds. A comparison reads "verdict: median paired difference (interval), p, r"; negative means the first configuration is better; r is the rank-biserial effect size, -1 when it was better on every seed.

## Runs

| Scenario | B2 | B4 | O1 | O2 |
| --- | --- | --- | --- | --- |
| S1 Large download starts during a video call | 5 | 5 | 5 | 5 |
| S2 WAN drops from 100 to 20 Mbit/s | 5 | 5 | 5 | 5 |
| S3 Three TVs streaming plus a gaming device | 5 | 5 | 5 | 5 |
| S4 Cloud backup saturates the upload during gaming | 5 | 5 | 5 | 5 |
| S5 Priority for the work laptop during a busy evening | 5 | 5 | 5 | 5 |
| S6 HD video call on a laptop that is also sharing files | 5 | 5 | 5 | 5 |
| S7 Cloud gaming on a PC that is also fetching a game update | 5 | 5 | 5 | 5 |
| S8 Flows that change what they are | 5 | 5 | 5 | 5 |
| S10 The delay probes go silent, then the controller is killed | 5 | 5 | 5 | 5 |
| S11 A family evening on a 50/10 line | 5 | 5 | 5 | 5 |

Valid runs per scenario and configuration. No A/A runs: differences are judged against zero.

## Summed cost, configuration against configuration

| Scenario | O2 against B4 | O2 against O1 | O2 against B2 | O1 against B2 | B4 against B2 |
| --- | --- | --- | --- | --- | --- |
| S1 | too few seeds: +0.00 (+0.00 to +0.00) | too few seeds: +0.00 (+0.00 to +0.00) | too few seeds: +0.00 (+0.00 to +0.00) | too few seeds: +0.00 (+0.00 to +0.00) | too few seeds: +0.00 (+0.00 to +0.00) |
| S2 | too few seeds: -1.00 (-1.00 to -1.00), r -1.00 | too few seeds: +0.00 (+0.00 to +0.00) | too few seeds: -1.00 (-1.00 to -1.00), r -1.00 | too few seeds: -1.00 (-1.00 to -1.00), r -1.00 | too few seeds: +0.00 (+0.00 to +0.00) |
| S3 | too few seeds: +0.00 (-1.00 to +0.00), r -1.00 | too few seeds: +0.00 (-0.00 to +0.00), r -1.00 | too few seeds: +0.00 (+0.00 to +0.00) | too few seeds: +0.00 (+0.00 to +0.00), r +1.00 | too few seeds: +0.00 (+0.00 to +1.00), r +1.00 |
| S4 | too few seeds: +0.00 (+0.00 to +0.00) | too few seeds: +0.00 (+0.00 to +0.00) | too few seeds: +0.00 (+0.00 to +0.00) | too few seeds: +0.00 (+0.00 to +0.00) | too few seeds: +0.00 (+0.00 to +0.00) |
| S5 | too few seeds: -2.00 (-2.02 to -0.52), r -1.00 | too few seeds: +0.00 (-0.10 to +0.00), r -0.33 | too few seeds: -1.00 (-1.00 to -0.80), r -1.00 | too few seeds: -0.93 (-1.00 to -0.80), r -1.00 | too few seeds: +1.00 (-0.28 to +1.09), r +0.87 |
| S6 | too few seeds: -1.00 (-1.00 to -0.98), r -1.00 | too few seeds: +0.00 (-0.00 to +0.01), r +0.33 | too few seeds: -1.00 (-1.00 to -0.98), r -1.00 | too few seeds: -1.00 (-1.00 to -0.98), r -1.00 | too few seeds: +0.00 (+0.00 to +0.00) |
| S7 | too few seeds: -1.00 (-1.00 to +0.00), r -1.00 | too few seeds: +0.00 (+0.00 to +0.07), r +1.00 | too few seeds: -1.00 (-1.00 to +0.00), r -1.00 | too few seeds: -1.00 (-1.00 to +0.00), r -1.00 | too few seeds: +0.00 (+0.00 to +0.00) |
| S8 | too few seeds: -1.00 (-1.79 to -0.58), r -1.00 | too few seeds: +0.00 (+0.00 to +0.00) | too few seeds: +0.00 (-1.00 to +0.00), r -1.00 | too few seeds: +0.00 (-1.00 to +0.00), r -1.00 | too few seeds: +0.79 (+0.22 to +1.00), r +1.00 |
| S10 | too few seeds: +0.00 (+0.00 to +0.00) | too few seeds: +0.00 (+0.00 to +0.00) | too few seeds: +0.00 (+0.00 to +0.00) | too few seeds: +0.00 (+0.00 to +0.00) | too few seeds: +0.00 (+0.00 to +0.00) |
| S11 | too few seeds: -1.13 (-1.16 to -1.02), r -1.00 | too few seeds: +0.00 (+0.00 to +0.00) | too few seeds: +0.00 (+0.00 to +0.00) | too few seeds: +0.00 (+0.00 to +0.00) | too few seeds: +1.13 (+1.02 to +1.16), r +1.00 |

O2 against B4 and O2 against O1 were named in advance; the p-values of the others are Holm-corrected.

## Summed cost, median over seeds

| Scenario | B2 | B4 | O1 | O2 |
| --- | --- | --- | --- | --- |
| S1 | 0.00 (0.00 to 0.00) | 0.00 (0.00 to 0.00) | 0.00 (0.00 to 0.00) | 0.00 (0.00 to 0.00) |
| S2 | 1.00 (1.00 to 1.00) | 1.00 (1.00 to 1.00) | 0.00 (0.00 to 0.00) | 0.00 (0.00 to 0.00) |
| S3 | 0.27 (0.19 to 0.34) | 0.32 (0.21 to 1.19) | 0.27 (0.19 to 0.34) | 0.27 (0.19 to 0.34) |
| S4 | 0.00 (0.00 to 0.00) | 0.00 (0.00 to 0.00) | 0.00 (0.00 to 0.00) | 0.00 (0.00 to 0.00) |
| S5 | 1.21 (1.10 to 1.27) | 2.23 (0.81 to 2.36) | 0.29 (0.19 to 0.34) | 0.27 (0.19 to 0.34) |
| S6 | 1.00 (1.00 to 1.00) | 1.00 (1.00 to 1.00) | 0.00 (0.00 to 0.02) | 0.00 (0.00 to 0.02) |
| S7 | 1.09 (1.01 to 1.12) | 1.09 (1.01 to 1.12) | 0.12 (0.09 to 1.01) | 0.12 (0.09 to 1.01) |
| S8 | 0.12 (0.05 to 1.12) | 1.09 (0.71 to 1.90) | 0.09 (0.01 to 0.12) | 0.09 (0.01 to 0.12) |
| S10 | 0.00 (0.00 to 0.00) | 0.00 (0.00 to 0.00) | 0.00 (0.00 to 0.00) | 0.00 (0.00 to 0.00) |
| S11 | 0.09 (0.01 to 0.12) | 1.21 (1.13 to 1.24) | 0.09 (0.01 to 0.12) | 0.09 (0.01 to 0.12) |

## Calls and games

| Scenario | Measure | B2 | B4 | O1 | O2 |
| --- | --- | --- | --- | --- | --- |
| S1 | call: late frames % | 0.0 (0.0 to 0.0) | 0.0 (0.0 to 0.1) | 0.0 (0.0 to 0.0) | 0.0 (0.0 to 0.0) |
| S1 | call: latency p95 ms | 11.3 (11.0 to 12.1) | 11.3 (11.0 to 12.1) | 11.3 (11.0 to 12.1) | 11.3 (11.0 to 12.1) |
| S2 | call: late frames % | 55.5 (55.5 to 55.5) | 55.6 (55.5 to 55.6) | 0.9 (0.9 to 0.9) | 0.9 (0.9 to 1.0) |
| S2 | call: latency p95 ms | 499.0 (496.7 to 499.8) | 497.7 (496.1 to 499.7) | 20.0 (17.2 to 21.2) | 20.0 (17.6 to 20.4) |
| S3 | game: late frames % | 0.0 (0.0 to 0.0) | 0.9 (0.0 to 16.8) | 0.0 (0.0 to 0.0) | 0.0 (0.0 to 0.0) |
| S3 | game: latency p95 ms | 17.7 (11.6 to 19.3) | 37.1 (32.0 to 155.5) | 17.2 (11.7 to 18.0) | 16.6 (11.5 to 17.7) |
| S4 | game: late frames % | 0.0 (0.0 to 0.0) | 0.0 (0.0 to 0.0) | 0.0 (0.0 to 0.0) | 0.0 (0.0 to 0.0) |
| S4 | game: latency p95 ms | 11.3 (11.3 to 11.3) | 11.3 (11.3 to 11.3) | 11.3 (11.3 to 11.3) | 11.3 (11.3 to 11.3) |
| S5 | call: late frames % | 78.1 (16.3 to 94.3) | 72.6 (5.0 to 86.5) | 0.0 (0.0 to 3.0) | 0.0 (0.0 to 0.2) |
| S5 | call: latency p95 ms | 35.4 (32.3 to 36.2) | 626.2 (61.0 to 1,786.5) | 21.6 (20.9 to 28.7) | 21.5 (19.6 to 28.4) |
| S5 | game: late frames % | 0.0 (0.0 to 0.0) | 21.9 (6.9 to 43.3) | 0.0 (0.0 to 0.0) | 0.0 (0.0 to 0.0) |
| S5 | game: latency p95 ms | 19.1 (14.9 to 21.4) | 131.8 (53.9 to 548.1) | 19.0 (14.8 to 19.9) | 18.7 (15.0 to 20.8) |
| S6 | call: late frames % | 94.3 (94.1 to 94.4) | 94.4 (94.1 to 94.4) | 1.0 (0.7 to 1.3) | 1.0 (0.6 to 1.3) |
| S6 | call: latency p95 ms | 1,706.6 (1,485.0 to 1,786.2) | 2,181.1 (2,086.9 to 2,316.6) | 17.6 (17.4 to 18.0) | 17.5 (17.4 to 18.1) |
| S7 | game: late frames % | 83.1 (83.1 to 83.3) | 19.4 (19.3 to 19.4) | 0.3 (0.1 to 69.2) | 0.4 (0.0 to 72.3) |
| S7 | game: latency p95 ms | 919.2 (551.9 to 1,217.2) | 21.5 (14.4 to 25.7) | 17.0 (15.0 to 43.4) | 16.9 (14.7 to 43.5) |
| S8 | call: late frames % | 0.3 (0.2 to 66.2) | 15.0 (8.1 to 50.8) | 0.0 (0.0 to 0.1) | 0.0 (0.0 to 0.0) |
| S8 | call: latency p95 ms | 26.8 (21.9 to 34.2) | 40.4 (31.8 to 791.1) | 12.7 (11.6 to 13.2) | 12.3 (11.6 to 13.1) |
| S8 | game: late frames % | 0.0 (0.0 to 0.0) | 0.0 (0.0 to 14.8) | 0.0 (0.0 to 0.0) | 0.0 (0.0 to 0.0) |
| S8 | game: latency p95 ms | 11.3 (11.0 to 12.0) | 16.5 (12.1 to 59.5) | 11.1 (10.9 to 13.6) | 11.0 (10.9 to 12.0) |
| S10 | call: late frames % | 0.0 (0.0 to 0.0) | 0.0 (0.0 to 0.1) | 0.0 (0.0 to 0.0) | 0.0 (0.0 to 0.0) |
| S10 | call: latency p95 ms | 11.3 (11.0 to 12.1) | 11.3 (10.9 to 12.1) | 11.3 (10.9 to 12.0) | 11.3 (10.9 to 12.0) |
| S11 | call: late frames % | 0.7 (0.6 to 0.7) | 69.3 (17.8 to 72.7) | 0.0 (0.0 to 0.1) | 0.0 (0.0 to 0.1) |
| S11 | call: latency p95 ms | 33.7 (30.3 to 34.7) | 59.7 (48.7 to 185.8) | 25.7 (24.1 to 27.6) | 25.2 (23.8 to 27.7) |
| S11 | game: late frames % | 0.1 (0.0 to 0.1) | 0.6 (0.4 to 0.7) | 0.0 (0.0 to 0.2) | 0.0 (0.0 to 0.2) |
| S11 | game: latency p95 ms | 22.9 (22.4 to 24.3) | 24.7 (22.2 to 25.5) | 23.6 (23.3 to 26.5) | 23.3 (22.9 to 23.8) |
| S11 | voice: latency p95 ms | 23.0 (22.4 to 24.2) | 22.2 (22.0 to 22.4) | 23.6 (23.2 to 26.4) | 23.3 (22.7 to 23.6) |

## What the controller did, mean per run

| Scenario | Configuration | labelled | relabelled | triggers | rechecks | rate_changes | watched | regressed | rolled_back | rejected |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| S1 | B4 | 5.6 | 2.8 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 |
| S1 | O1 | 2.8 | 0.0 | 18.8 | 2.2 | 0.0 | 1.0 | 0.0 | 0.0 | 0.0 |
| S1 | O2 | 2.8 | 0.0 | 18.8 | 3.4 | 0.0 | 1.0 | 0.0 | 0.0 | 0.0 |
| S2 | B4 | 5.6 | 2.8 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 |
| S2 | O1 | 2.8 | 0.0 | 13.6 | 5.6 | 19.2 | 10.0 | 0.4 | 0.0 | 0.0 |
| S2 | O2 | 2.8 | 0.0 | 13.6 | 5.6 | 19.4 | 10.0 | 0.0 | 0.0 | 0.0 |
| S3 | B4 | 8.0 | 4.0 | 0.0 | 0.0 | 0.0 | 0.4 | 0.0 | 0.0 | 0.0 |
| S3 | O1 | 7.4 | 3.4 | 17.8 | 10.2 | 2.4 | 6.0 | 0.2 | 0.2 | 0.0 |
| S3 | O2 | 4.0 | 0.0 | 12.0 | 12.2 | 3.4 | 3.2 | 0.0 | 0.0 | 0.0 |
| S4 | B4 | 5.2 | 2.6 | 0.0 | 0.0 | 0.0 | 0.4 | 0.0 | 0.0 | 0.0 |
| S4 | O1 | 4.2 | 1.6 | 17.4 | 3.0 | 0.0 | 1.0 | 0.0 | 0.0 | 0.0 |
| S4 | O2 | 4.2 | 1.6 | 17.6 | 3.2 | 0.0 | 1.0 | 0.0 | 0.0 | 0.0 |
| S5 | B4 | 15.6 | 8.6 | 0.0 | 0.0 | 0.0 | 1.4 | 0.0 | 0.0 | 1.6 |
| S5 | O1 | 12.0 | 5.0 | 46.4 | 13.2 | 9.2 | 11.6 | 0.0 | 0.0 | 0.0 |
| S5 | O2 | 9.0 | 2.0 | 34.8 | 15.8 | 7.4 | 7.8 | 0.2 | 0.2 | 0.0 |
| S6 | B4 | 69.4 | 38.8 | 0.0 | 0.0 | 0.0 | 3.8 | 0.0 | 0.0 | 9.8 |
| S6 | O1 | 42.2 | 11.6 | 114.6 | 65.6 | 3.6 | 5.2 | 0.0 | 0.0 | 0.0 |
| S6 | O2 | 41.4 | 10.8 | 115.4 | 64.0 | 3.2 | 4.0 | 0.0 | 0.0 | 0.0 |
| S7 | B4 | 22.0 | 12.0 | 0.0 | 0.0 | 0.0 | 1.0 | 0.0 | 0.0 | 1.0 |
| S7 | O1 | 10.6 | 0.6 | 9.0 | 29.0 | 3.6 | 4.6 | 0.0 | 0.0 | 0.0 |
| S7 | O2 | 10.0 | 0.0 | 7.6 | 29.8 | 3.8 | 3.6 | 0.2 | 0.2 | 0.0 |
| S8 | B4 | 73.2 | 43.6 | 0.0 | 0.0 | 0.0 | 8.2 | 0.0 | 0.0 | 7.0 |
| S8 | O1 | 40.0 | 10.4 | 104.4 | 63.0 | 3.0 | 6.8 | 0.0 | 0.0 | 0.0 |
| S8 | O2 | 38.2 | 8.6 | 101.8 | 60.6 | 2.4 | 4.2 | 0.0 | 0.0 | 0.0 |
| S11 | B4 | 42.8 | 27.0 | 0.0 | 0.0 | 0.0 | 1.2 | 0.0 | 0.0 | 10.2 |
| S11 | O1 | 20.0 | 4.0 | 37.8 | 33.8 | 1.8 | 5.4 | 0.0 | 0.0 | 0.0 |
| S11 | O2 | 18.2 | 2.4 | 35.2 | 34.4 | 5.4 | 6.6 | 0.0 | 0.0 | 0.0 |

## CPU per component, % of one core over the run

| Scenario | Configuration | controller | ml-service | reflector | watchdog |
| --- | --- | --- | --- | --- | --- |
| S1 | B4 | 1.86 | 1.04 |  | 0.04 |
| S1 | O1 | 2.67 | 0.03 | 0.16 | 0.04 |
| S1 | O2 | 2.93 | 0.03 | 0.18 | 0.05 |
| S2 | B4 | 1.89 | 1.03 |  | 0.04 |
| S2 | O1 | 2.64 | 0.03 | 0.16 | 0.04 |
| S2 | O2 | 2.67 | 0.03 | 0.17 | 0.04 |
| S3 | B4 | 1.91 | 1.07 |  | 0.04 |
| S3 | O1 | 2.68 | 0.03 | 0.13 | 0.04 |
| S3 | O2 | 2.76 | 0.03 | 0.14 | 0.04 |
| S4 | B4 | 2.07 | 1.04 |  | 0.05 |
| S4 | O1 | 2.81 | 0.03 | 0.15 | 0.04 |
| S4 | O2 | 2.84 | 0.02 | 0.16 | 0.04 |
| S5 | B4 | 1.95 | 1.09 |  | 0.04 |
| S5 | O1 | 2.69 | 0.04 | 0.12 | 0.04 |
| S5 | O2 | 2.59 | 0.03 | 0.12 | 0.04 |
| S6 | B4 | 1.80 | 1.23 |  | 0.04 |
| S6 | O1 | 2.72 | 0.04 | 0.15 | 0.04 |
| S6 | O2 | 2.62 | 0.04 | 0.15 | 0.04 |
| S7 | B4 | 1.87 | 1.12 |  | 0.04 |
| S7 | O1 | 2.88 | 0.03 | 0.18 | 0.04 |
| S7 | O2 | 2.69 | 0.03 | 0.16 | 0.04 |
| S8 | B4 | 1.76 | 1.22 |  | 0.04 |
| S8 | O1 | 2.62 | 0.04 | 0.15 | 0.04 |
| S8 | O2 | 2.85 | 0.04 | 0.17 | 0.04 |
| S10 | B4 |  | 1.05 |  | 0.04 |
| S10 | O1 |  | 0.03 | 0.12 | 0.04 |
| S10 | O2 |  | 0.03 | 0.12 | 0.04 |
| S11 | B4 | 1.76 | 1.09 |  | 0.04 |
| S11 | O1 | 2.63 | 0.03 | 0.14 | 0.04 |
| S11 | O2 | 2.81 | 0.03 | 0.15 | 0.04 |

Measured on the reference laptop (12th-gen Intel i5, 16 threads), so a router's figures will be several times higher. The ML service is the Go service for O1 and O2 and Python for B4.

