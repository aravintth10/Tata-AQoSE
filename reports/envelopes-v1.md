# Envelope check: false triggers on flows that did not change

Dataset `v1.jsonl.gz`, validation split. Each flow is checked against the range of its own true class, so a flow sent back is a false trigger. Window records are five seconds apart here; live they are one second apart and overlap, so live figures will differ.

| Class | Flows | Records | Records outside, k=1 | k=2 | k=3 | Flows sent back, k=2, 2 in a row | k=2, 3 in a row | k=3, 2 in a row |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| telephony | 13 | 2364 | 100% | 100% | 99% | 38% | 38% | 31% |
| realtime_interactive | 111 | 6370 | 38% | 25% | 13% | 75% | 48% | 42% |
| multimedia_conferencing | 438 | 18425 | 38% | 18% | 9% | 69% | 38% | 50% |
| multimedia_streaming | 17 | 179 | 28% | 20% | 12% | 18% | 12% | 6% |
| standard | 1880 | 10973 | 28% | 24% | 21% | 12% | 8% | 10% |
| high_throughput_data | 51 | 615 | 18% | 14% | 11% | 14% | 12% | 12% |
| low_priority_data | 101 | 655 | 81% | 56% | 33% | 60% | 44% | 37% |
