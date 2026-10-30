# Offline head-to-head: v1, validation split

Dataset `v1` (sha256 `fe91bbad5e5e`), 3607 flows from 370 sessions. Damage table: `damage_table.yaml` (measured). Every flow counts once and every class counts the same.

## Verdict: go

- LightGBM, expected damage (O2) has lower balanced expected damage than the tuned rules: 0.242 against 0.745 (difference -0.503, 95% interval -0.593 to -0.415).
- Video calls (443 flows): damage 0.01 against the rules' 1.52; sent to Bulk 0% against 0%.
- Cloud gaming (106 flows): damage 0.02 against the rules' 0.85; sent to Bulk 0% against 2%.

## Scores

| Contestant | Balanced expected damage | Against tuned rules (95% interval) | Accuracy | Macro-F1 | Calibration error |
| --- | --- | --- | --- | --- | --- |
| Rules, qosify defaults | 1.991 | +1.246 (+1.031 to +1.485) | 0.460 | 0.110 |  |
| Rules, tuned (4000 pkt/s, 1200 B) | 0.745 |  | 0.418 | 0.104 |  |
| Random forest, top label (O1) | 0.379 | -0.366 (-0.425 to -0.297) | 0.942 | 0.719 | 0.025 |
| Random forest, expected damage (O2) | 0.245 | -0.500 (-0.588 to -0.415) | 0.942 | 0.719 | 0.025 |
| LightGBM, top label (O1) | 0.385 | -0.360 (-0.432 to -0.265) | 0.944 | 0.735 | 0.052 |
| LightGBM, expected damage (O2) | 0.242 | -0.503 (-0.593 to -0.415) | 0.944 | 0.735 | 0.052 |

Lower damage is better. The interval comes from resampling whole sessions 2,000 times; one that stays below zero means the contestant beats the tuned rules.

## Expected damage by true class

| Contestant | telephony | realtime_interactive | multimedia_conferencing | multimedia_streaming | standard | high_throughput_data | low_priority_data |
| --- | --- | --- | --- | --- | --- | --- | --- |
| *flows* | 19 | 111 | 443 | 23 | 2843 | 67 | 101 |
| Rules, qosify defaults | 0.00 | 5.51 | 1.89 | 1.04 | 0.01 | 4.13 | 1.37 |
| Rules, tuned (4000 pkt/s, 1200 B) | 0.00 | 0.83 | 1.52 | 0.21 | 0.00 | 0.75 | 1.92 |
| Random forest, top label (O1) | 0.00 | 0.01 | 0.01 | 0.95 | 0.10 | 0.40 | 1.18 |
| Random forest, expected damage (O2) | 0.00 | 0.04 | 0.01 | 0.50 | 0.00 | 0.00 | 1.17 |
| LightGBM, top label (O1) | 0.00 | 0.00 | 0.01 | 0.95 | 0.12 | 0.43 | 1.18 |
| LightGBM, expected damage (O2) | 0.00 | 0.02 | 0.01 | 0.50 | 0.00 | 0.00 | 1.16 |

## The traffic the project is about

| Contestant | Video calls: sent to Bulk | Video calls: damage | Cloud gaming: sent to Bulk | Cloud gaming: damage |
| --- | --- | --- | --- | --- |
| Rules, qosify defaults | 15% of 443 | 1.89 | 89% of 106 | 5.51 |
| Rules, tuned (4000 pkt/s, 1200 B) | 0% of 443 | 1.52 | 2% of 106 | 0.85 |
| Random forest, top label (O1) | 0% of 443 | 0.01 | 0% of 106 | 0.01 |
| Random forest, expected damage (O2) | 0% of 443 | 0.01 | 0% of 106 | 0.04 |
| LightGBM, top label (O1) | 0% of 443 | 0.01 | 0% of 106 | 0.00 |
| LightGBM, expected damage (O2) | 0% of 443 | 0.01 | 0% of 106 | 0.02 |

## Where each class's flows were sent

**Rules, qosify defaults**

| True class | voice | video | best_effort | bulk | Recall |
| --- | --- | --- | --- | --- | --- |
| telephony | 43% | 0% | 57% | 0% | 0.00 |
| realtime_interactive | 3% | 0% | 8% | 88% | 0.03 |
| multimedia_conferencing | 37% | 0% | 48% | 15% | 0.00 |
| multimedia_streaming | 4% | 0% | 41% | 56% | 0.00 |
| standard | 42% | 0% | 58% | 0% | 0.58 |
| high_throughput_data | 0% | 0% | 41% | 59% | 0.00 |
| low_priority_data | 16% | 0% | 70% | 14% | 0.14 |

**Rules, tuned (4000 pkt/s, 1200 B)**

| True class | voice | video | best_effort | bulk | Recall |
| --- | --- | --- | --- | --- | --- |
| telephony | 45% | 0% | 55% | 0% | 0.00 |
| realtime_interactive | 69% | 0% | 29% | 2% | 0.69 |
| multimedia_conferencing | 96% | 0% | 4% | 0% | 0.00 |
| multimedia_streaming | 59% | 0% | 41% | 0% | 0.00 |
| standard | 50% | 0% | 50% | 0% | 0.50 |
| high_throughput_data | 62% | 0% | 32% | 6% | 0.00 |
| low_priority_data | 85% | 0% | 15% | 0% | 0.00 |

**Random forest, top label (O1)**

| True class | voice | video | best_effort | bulk | Recall |
| --- | --- | --- | --- | --- | --- |
| telephony | 53% | 11% | 37% | 0% | 0.53 |
| realtime_interactive | 99% | 1% | 0% | 0% | 0.98 |
| multimedia_conferencing | 0% | 100% | 0% | 0% | 1.00 |
| multimedia_streaming | 0% | 91% | 9% | 0% | 0.91 |
| standard | 1% | 3% | 96% | 0% | 0.94 |
| high_throughput_data | 0% | 9% | 91% | 0% | 0.90 |
| low_priority_data | 1% | 3% | 73% | 22% | 0.75 |

**Random forest, expected damage (O2)**

| True class | voice | video | best_effort | bulk | Recall |
| --- | --- | --- | --- | --- | --- |
| telephony | 34% | 0% | 66% | 0% | 0.53 |
| realtime_interactive | 98% | 1% | 2% | 0% | 0.98 |
| multimedia_conferencing | 0% | 100% | 0% | 0% | 1.00 |
| multimedia_streaming | 0% | 0% | 100% | 0% | 0.91 |
| standard | 1% | 0% | 99% | 0% | 0.94 |
| high_throughput_data | 0% | 0% | 100% | 0% | 0.90 |
| low_priority_data | 0% | 0% | 77% | 22% | 0.75 |

**LightGBM, top label (O1)**

| True class | voice | video | best_effort | bulk | Recall |
| --- | --- | --- | --- | --- | --- |
| telephony | 74% | 16% | 11% | 0% | 0.74 |
| realtime_interactive | 96% | 4% | 0% | 0% | 0.95 |
| multimedia_conferencing | 0% | 100% | 0% | 0% | 1.00 |
| multimedia_streaming | 0% | 91% | 9% | 0% | 0.91 |
| standard | 1% | 3% | 96% | 0% | 0.95 |
| high_throughput_data | 3% | 9% | 88% | 0% | 0.77 |
| low_priority_data | 2% | 3% | 73% | 23% | 0.78 |

**LightGBM, expected damage (O2)**

| True class | voice | video | best_effort | bulk | Recall |
| --- | --- | --- | --- | --- | --- |
| telephony | 55% | 0% | 45% | 0% | 0.74 |
| realtime_interactive | 96% | 3% | 1% | 0% | 0.95 |
| multimedia_conferencing | 0% | 100% | 0% | 0% | 1.00 |
| multimedia_streaming | 0% | 0% | 100% | 0% | 0.91 |
| standard | 1% | 0% | 99% | 0% | 0.95 |
| high_throughput_data | 0% | 0% | 100% | 0% | 0.77 |
| low_priority_data | 0% | 0% | 77% | 23% | 0.78 |

## Has the model learned the lab?

LightGBM trained without each source in turn, then scored on that source's flows by expected damage. A class the model can only place when it has seen that source's own recordings shows up here as high damage.

| Source left out | Flows | Damage by class on that source |
| --- | --- | --- |
| iscx-tor-nontor-2016 | 1591 | standard 0.00 (1591) |
| iscx-vpn-nonvpn-2016 | 273 | high_throughput_data 0.00 (23), multimedia_streaming 0.50 (5), standard 0.11 (229), telephony 0.00 (16) |
| labayen-2020 | 831 | high_throughput_data 0.03 (6), multimedia_streaming 0.48 (13), standard 0.08 (812) |
| own-captures | 3 | realtime_interactive 0.80 (3) |
| purdue-vca-2026 | 437 | multimedia_conferencing 1.55 (437) |
| testbed | | Skipped: a class would have no data left without this source |
| unsw-cloud-gaming-2025 | 97 | realtime_interactive 2.10 (97) |

