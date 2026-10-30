# Live runs, side by side

From `results-demo/`. Cost is 0 for as good as it gets and 1 for unusable, by the thresholds in `damage_scoring.yaml`; a download or a web page is judged against the same one under B2.

## S6: HD video call on a laptop that is also sharing files (seed 1)

| Entry | Measure | B2 | O2 |
| --- | --- | --- | --- |
| call (video_call) | latency p95 ms | 1,678.5 | 17.6 |
| call (video_call) | loss % | 91.1 | 0.1 |
| call (video_call) | late frames % | 94.3 | 1.2 |
| call (video_call) | **cost** | 1.00 | 0.01 |
| sharing (p2p) | throughput Mbit/s | 52.0 | 49.7 |
| sharing (p2p) | **cost** | 0.00 | 0.00 |
| download (bulk_download) | throughput Mbit/s | 42.0 | 39.4 |
| download (bulk_download) | **cost** | 0.00 | 0.00 |
| *all entries* | **summed cost** | **1.00** | **0.01** |

- O2: 40 labels written, 4 rate changes, 0 proposals rejected by the shield

## Summed cost over the scenarios run

| | B2 | O2 |
| --- | --- | --- |
| Scenarios | 1 | 1 |
| Mean summed cost | 1.00 | 0.01 |

