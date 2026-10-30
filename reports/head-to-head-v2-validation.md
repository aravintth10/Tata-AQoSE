# Offline head-to-head: v2, validation split

Dataset `v2` (sha256 `b3f691d89f00`), 3609 flows from 372 sessions. Damage table: `damage_table.yaml` (measured). Every flow counts once and every class counts the same.

## Verdict: go

- LightGBM, expected damage (O2) has lower balanced expected damage than the tuned rules: 0.239 against 0.745 (difference -0.506, 95% interval -0.598 to -0.420).
- Video calls (444 flows): damage 0.01 against the rules' 1.52; sent to Bulk 0% against 0%.
- Cloud gaming (106 flows): damage 0.05 against the rules' 0.85; sent to Bulk 0% against 2%.

## Scores

| Contestant | Balanced expected damage | Against tuned rules (95% interval) | Accuracy | Macro-F1 | Calibration error |
| --- | --- | --- | --- | --- | --- |
| Rules, qosify defaults | 1.991 | +1.246 (+1.030 to +1.478) | 0.460 | 0.110 |  |
| Rules, tuned (4000 pkt/s, 1200 B) | 0.745 |  | 0.418 | 0.104 |  |
| Random forest, top label (O1) | 0.330 | -0.415 (-0.497 to -0.330) | 0.950 | 0.732 | 0.019 |
| Random forest, expected damage (O2) | 0.246 | -0.499 (-0.587 to -0.415) | 0.950 | 0.732 | 0.019 |
| LightGBM, top label (O1) | 0.299 | -0.446 (-0.539 to -0.356) | 0.972 | 0.801 | 0.032 |
| LightGBM, expected damage (O2) | 0.239 | -0.506 (-0.598 to -0.420) | 0.972 | 0.801 | 0.032 |

Lower damage is better. The interval comes from resampling whole sessions 2,000 times; one that stays below zero means the contestant beats the tuned rules.

## Expected damage by true class

| Contestant | telephony | realtime_interactive | multimedia_conferencing | multimedia_streaming | standard | high_throughput_data | low_priority_data |
| --- | --- | --- | --- | --- | --- | --- | --- |
| *flows* | 20 | 111 | 444 | 23 | 2843 | 67 | 101 |
| Rules, qosify defaults | 0.00 | 5.51 | 1.89 | 1.04 | 0.01 | 4.13 | 1.37 |
| Rules, tuned (4000 pkt/s, 1200 B) | 0.00 | 0.83 | 1.52 | 0.21 | 0.00 | 0.75 | 1.92 |
| Random forest, top label (O1) | 0.00 | 0.00 | 0.01 | 0.95 | 0.07 | 0.09 | 1.18 |
| Random forest, expected damage (O2) | 0.00 | 0.05 | 0.01 | 0.50 | 0.00 | 0.00 | 1.17 |
| LightGBM, top label (O1) | 0.00 | 0.02 | 0.01 | 0.90 | 0.02 | 0.03 | 1.12 |
| LightGBM, expected damage (O2) | 0.00 | 0.04 | 0.01 | 0.50 | 0.00 | 0.00 | 1.11 |

## The traffic the project is about

| Contestant | Video calls: sent to Bulk | Video calls: damage | Cloud gaming: sent to Bulk | Cloud gaming: damage |
| --- | --- | --- | --- | --- |
| Rules, qosify defaults | 15% of 444 | 1.89 | 89% of 106 | 5.51 |
| Rules, tuned (4000 pkt/s, 1200 B) | 0% of 444 | 1.52 | 2% of 106 | 0.85 |
| Random forest, top label (O1) | 0% of 444 | 0.01 | 0% of 106 | 0.00 |
| Random forest, expected damage (O2) | 0% of 444 | 0.01 | 0% of 106 | 0.05 |
| LightGBM, top label (O1) | 0% of 444 | 0.01 | 0% of 106 | 0.02 |
| LightGBM, expected damage (O2) | 0% of 444 | 0.01 | 0% of 106 | 0.05 |

## Where each class's flows were sent

**Rules, qosify defaults**

| True class | voice | video | best_effort | bulk | Recall |
| --- | --- | --- | --- | --- | --- |
| telephony | 45% | 0% | 55% | 0% | 0.00 |
| realtime_interactive | 3% | 0% | 8% | 88% | 0.03 |
| multimedia_conferencing | 37% | 0% | 48% | 15% | 0.00 |
| multimedia_streaming | 4% | 0% | 41% | 56% | 0.00 |
| standard | 42% | 0% | 58% | 0% | 0.58 |
| high_throughput_data | 0% | 0% | 41% | 59% | 0.00 |
| low_priority_data | 16% | 0% | 70% | 14% | 0.14 |

**Rules, tuned (4000 pkt/s, 1200 B)**

| True class | voice | video | best_effort | bulk | Recall |
| --- | --- | --- | --- | --- | --- |
| telephony | 48% | 0% | 52% | 0% | 0.00 |
| realtime_interactive | 69% | 0% | 29% | 2% | 0.69 |
| multimedia_conferencing | 96% | 0% | 4% | 0% | 0.00 |
| multimedia_streaming | 59% | 0% | 41% | 0% | 0.00 |
| standard | 50% | 0% | 50% | 0% | 0.50 |
| high_throughput_data | 62% | 0% | 32% | 6% | 0.00 |
| low_priority_data | 85% | 0% | 15% | 0% | 0.00 |

**Random forest, top label (O1)**

| True class | voice | video | best_effort | bulk | Recall |
| --- | --- | --- | --- | --- | --- |
| telephony | 55% | 10% | 35% | 0% | 0.55 |
| realtime_interactive | 99% | 1% | 0% | 0% | 0.97 |
| multimedia_conferencing | 0% | 100% | 0% | 0% | 1.00 |
| multimedia_streaming | 0% | 91% | 9% | 0% | 0.91 |
| standard | 1% | 2% | 97% | 0% | 0.95 |
| high_throughput_data | 0% | 2% | 98% | 0% | 0.97 |
| low_priority_data | 1% | 3% | 74% | 22% | 0.71 |

**Random forest, expected damage (O2)**

| True class | voice | video | best_effort | bulk | Recall |
| --- | --- | --- | --- | --- | --- |
| telephony | 37% | 0% | 63% | 0% | 0.55 |
| realtime_interactive | 97% | 1% | 2% | 0% | 0.97 |
| multimedia_conferencing | 0% | 100% | 0% | 0% | 1.00 |
| multimedia_streaming | 0% | 0% | 100% | 0% | 0.91 |
| standard | 1% | 0% | 99% | 0% | 0.95 |
| high_throughput_data | 0% | 0% | 100% | 0% | 0.97 |
| low_priority_data | 0% | 0% | 77% | 22% | 0.71 |

**LightGBM, top label (O1)**

| True class | voice | video | best_effort | bulk | Recall |
| --- | --- | --- | --- | --- | --- |
| telephony | 45% | 15% | 40% | 0% | 0.45 |
| realtime_interactive | 96% | 3% | 1% | 0% | 0.96 |
| multimedia_conferencing | 0% | 100% | 0% | 0% | 1.00 |
| multimedia_streaming | 0% | 80% | 20% | 0% | 0.80 |
| standard | 0% | 0% | 99% | 0% | 0.99 |
| high_throughput_data | 1% | 0% | 98% | 0% | 0.70 |
| low_priority_data | 0% | 1% | 73% | 26% | 0.80 |

**LightGBM, expected damage (O2)**

| True class | voice | video | best_effort | bulk | Recall |
| --- | --- | --- | --- | --- | --- |
| telephony | 32% | 5% | 62% | 0% | 0.45 |
| realtime_interactive | 96% | 2% | 2% | 0% | 0.96 |
| multimedia_conferencing | 0% | 100% | 0% | 0% | 1.00 |
| multimedia_streaming | 0% | 0% | 100% | 0% | 0.80 |
| standard | 0% | 0% | 100% | 0% | 0.99 |
| high_throughput_data | 0% | 0% | 100% | 0% | 0.70 |
| low_priority_data | 0% | 0% | 74% | 26% | 0.80 |

## Has the model learned the lab?

LightGBM trained without each source in turn, then scored on that source's flows by expected damage. A class the model can only place when it has seen that source's own recordings shows up here as high damage.

| Source left out | Flows | Damage by class on that source |
| --- | --- | --- |
| iscx-tor-nontor-2016 | 1591 | standard 0.00 (1591) |
| iscx-vpn-nonvpn-2016 | 273 | high_throughput_data 0.00 (23), multimedia_streaming 0.50 (5), standard 0.22 (229), telephony 0.00 (16) |
| labayen-2020 | 831 | high_throughput_data 0.00 (6), multimedia_streaming 0.47 (13), standard 0.08 (812) |
| own-captures | 5 | multimedia_conferencing 0.10 (1), realtime_interactive 0.80 (3), telephony 0.00 (1) |
| purdue-vca-2026 | 437 | multimedia_conferencing 1.54 (437) |
| testbed | | Skipped: a class would have no data left without this source |
| unsw-cloud-gaming-2025 | 97 | realtime_interactive 2.04 (97) |

