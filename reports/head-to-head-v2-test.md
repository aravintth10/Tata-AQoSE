# Offline head-to-head: v2, test split

Dataset `v2` (sha256 `b3f691d89f00`), 1213 flows from 183 sessions. Damage table: `damage_table.yaml` (measured). Every flow counts once and every class counts the same.

## Verdict: go

- Random forest, expected damage (O2) has lower balanced expected damage than the tuned rules: 0.248 against 0.636 (difference -0.388, 95% interval -0.454 to -0.336).
- Video calls (221 flows): damage 0.07 against the rules' 1.52; sent to Bulk 0% against 0%.
- Cloud gaming (47 flows): damage 0.14 against the rules' 0.91; sent to Bulk 0% against 1%.

## Scores

| Contestant | Balanced expected damage | Against tuned rules (95% interval) | Accuracy | Macro-F1 | Calibration error |
| --- | --- | --- | --- | --- | --- |
| Rules, qosify defaults | 1.620 | +0.984 (+0.652 to +1.446) | 0.119 | 0.043 |  |
| Rules, tuned (4000 pkt/s, 1200 B) | 0.636 |  | 0.136 | 0.057 |  |
| Random forest, top label (O1) | 0.480 | -0.156 (-0.335 to +0.123) | 0.963 | 0.744 | 0.015 |
| Random forest, expected damage (O2) | 0.248 | -0.388 (-0.454 to -0.336) | 0.963 | 0.744 | 0.015 |
| LightGBM, top label (O1) | 0.493 | -0.143 (-0.323 to +0.126) | 0.969 | 0.731 | 0.031 |
| LightGBM, expected damage (O2) | 0.270 | -0.366 (-0.444 to -0.299) | 0.969 | 0.731 | 0.031 |

Lower damage is better. The interval comes from resampling whole sessions 2,000 times; one that stays below zero means the contestant beats the tuned rules.

## Expected damage by true class

| Contestant | telephony | realtime_interactive | multimedia_conferencing | multimedia_streaming | standard | high_throughput_data | low_priority_data |
| --- | --- | --- | --- | --- | --- | --- | --- |
| *flows* | 6 | 47 | 221 | 8 | 268 | 4 | 659 |
| Rules, qosify defaults | 0.00 | 5.06 | 1.89 | 0.92 | 0.12 | 1.71 | 1.64 |
| Rules, tuned (4000 pkt/s, 1200 B) | 0.00 | 0.91 | 1.52 | 0.23 | 0.00 | 0.12 | 1.67 |
| Random forest, top label (O1) | 0.00 | 0.07 | 0.07 | 0.97 | 0.14 | 1.10 | 1.01 |
| Random forest, expected damage (O2) | 0.00 | 0.14 | 0.07 | 0.50 | 0.01 | 0.00 | 1.01 |
| LightGBM, top label (O1) | 0.00 | 0.12 | 0.07 | 0.99 | 0.04 | 1.23 | 1.01 |
| LightGBM, expected damage (O2) | 0.00 | 0.16 | 0.08 | 0.50 | 0.01 | 0.12 | 1.01 |

## The traffic the project is about

| Contestant | Video calls: sent to Bulk | Video calls: damage | Cloud gaming: sent to Bulk | Cloud gaming: damage |
| --- | --- | --- | --- | --- |
| Rules, qosify defaults | 14% of 221 | 1.89 | 78% of 47 | 5.06 |
| Rules, tuned (4000 pkt/s, 1200 B) | 0% of 221 | 1.52 | 1% of 47 | 0.91 |
| Random forest, top label (O1) | 0% of 221 | 0.07 | 0% of 47 | 0.07 |
| Random forest, expected damage (O2) | 0% of 221 | 0.07 | 0% of 47 | 0.14 |
| LightGBM, top label (O1) | 0% of 221 | 0.07 | 0% of 47 | 0.12 |
| LightGBM, expected damage (O2) | 0% of 221 | 0.08 | 0% of 47 | 0.16 |

## Where each class's flows were sent

**Rules, qosify defaults**

| True class | voice | video | best_effort | bulk | Recall |
| --- | --- | --- | --- | --- | --- |
| telephony | 96% | 0% | 4% | 0% | 0.00 |
| realtime_interactive | 7% | 0% | 15% | 78% | 0.07 |
| multimedia_conferencing | 37% | 0% | 48% | 14% | 0.00 |
| multimedia_streaming | 2% | 0% | 55% | 43% | 0.00 |
| standard | 46% | 0% | 52% | 2% | 0.52 |
| high_throughput_data | 0% | 0% | 76% | 24% | 0.00 |
| low_priority_data | 28% | 0% | 71% | 0% | 0.00 |

**Rules, tuned (4000 pkt/s, 1200 B)**

| True class | voice | video | best_effort | bulk | Recall |
| --- | --- | --- | --- | --- | --- |
| telephony | 96% | 0% | 4% | 0% | 0.00 |
| realtime_interactive | 65% | 0% | 34% | 1% | 0.65 |
| multimedia_conferencing | 95% | 0% | 5% | 0% | 0.00 |
| multimedia_streaming | 54% | 0% | 46% | 0% | 0.00 |
| standard | 50% | 0% | 50% | 0% | 0.50 |
| high_throughput_data | 24% | 0% | 76% | 0% | 0.00 |
| low_priority_data | 33% | 0% | 67% | 0% | 0.00 |

**Random forest, top label (O1)**

| True class | voice | video | best_effort | bulk | Recall |
| --- | --- | --- | --- | --- | --- |
| telephony | 82% | 0% | 18% | 0% | 0.78 |
| realtime_interactive | 95% | 2% | 3% | 0% | 0.91 |
| multimedia_conferencing | 1% | 96% | 2% | 0% | 0.96 |
| multimedia_streaming | 0% | 95% | 5% | 0% | 0.95 |
| standard | 1% | 4% | 95% | 0% | 0.93 |
| high_throughput_data | 0% | 24% | 76% | 0% | 0.50 |
| low_priority_data | 0% | 0% | 67% | 33% | 0.99 |

**Random forest, expected damage (O2)**

| True class | voice | video | best_effort | bulk | Recall |
| --- | --- | --- | --- | --- | --- |
| telephony | 79% | 0% | 21% | 0% | 0.78 |
| realtime_interactive | 93% | 1% | 6% | 0% | 0.91 |
| multimedia_conferencing | 1% | 96% | 2% | 0% | 0.96 |
| multimedia_streaming | 0% | 0% | 100% | 0% | 0.95 |
| standard | 1% | 0% | 98% | 0% | 0.93 |
| high_throughput_data | 0% | 0% | 100% | 0% | 0.50 |
| low_priority_data | 0% | 0% | 67% | 33% | 0.99 |

**LightGBM, top label (O1)**

| True class | voice | video | best_effort | bulk | Recall |
| --- | --- | --- | --- | --- | --- |
| telephony | 33% | 0% | 67% | 0% | 0.17 |
| realtime_interactive | 89% | 6% | 5% | 0% | 0.89 |
| multimedia_conferencing | 0% | 97% | 3% | 0% | 0.96 |
| multimedia_streaming | 0% | 98% | 2% | 0% | 0.98 |
| standard | 0% | 1% | 99% | 0% | 0.98 |
| high_throughput_data | 25% | 24% | 51% | 0% | 0.25 |
| low_priority_data | 0% | 0% | 67% | 33% | 0.98 |

**LightGBM, expected damage (O2)**

| True class | voice | video | best_effort | bulk | Recall |
| --- | --- | --- | --- | --- | --- |
| telephony | 17% | 17% | 67% | 0% | 0.17 |
| realtime_interactive | 89% | 4% | 6% | 0% | 0.89 |
| multimedia_conferencing | 0% | 96% | 4% | 0% | 0.96 |
| multimedia_streaming | 0% | 0% | 100% | 0% | 0.98 |
| standard | 0% | 0% | 99% | 0% | 0.98 |
| high_throughput_data | 25% | 0% | 75% | 0% | 0.25 |
| low_priority_data | 0% | 0% | 67% | 33% | 0.98 |

