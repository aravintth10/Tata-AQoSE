# Damage table sensitivity

Validation split of dataset v1. Every class counts the same. The balanced expected damage of the unperturbed table's placements is 0.141.

## Every cell at once, each by a random factor from 0.75 to 1.25 (200 tables)

| | Median | 95th percentile | Worst |
| --- | --- | --- | --- |
| Flows whose tin changes | 0.2% | 2.0% | 2.1% |
| Balanced expected damage, judged by the unperturbed table | 0.142 | 0.144 | 0.146 |

## Every cell at once, each moved by up to one point either way (200 tables)

| | Median | 95th percentile | Worst |
| --- | --- | --- | --- |
| Flows whose tin changes | 14.1% | 33.9% | 39.4% |
| Balanced expected damage, judged by the unperturbed table | 0.156 | 0.297 | 0.359 |

## One cell at a time, by 25% down or up: the ten that move most

| Class | Tin | Factor | Flows whose tin changes | Balanced expected damage |
| --- | --- | --- | --- | --- |
| low_priority_data | best_effort | 0.75 | 2.0% | 0.142 |
| low_priority_data | voice | 0.75 | 0.4% | 0.143 |
| low_priority_data | best_effort | 1.25 | 0.2% | 0.142 |
| high_throughput_data | voice | 0.75 | 0.2% | 0.142 |
| low_priority_data | voice | 1.25 | 0.1% | 0.142 |
| high_throughput_data | bulk | 1.25 | 0.0% | 0.141 |
| high_throughput_data | bulk | 0.75 | 0.0% | 0.141 |
| standard | bulk | 1.25 | 0.0% | 0.141 |
| telephony | voice | 0.75 | 0.0% | 0.141 |
| telephony | voice | 1.25 | 0.0% | 0.141 |

55 of 56 single-cell changes move under 0.5% of flows.
