# Expected damage against top label, with a weaker classifier

Every number the classifier sees is multiplied by exp(N(0, noise)). Offline: validation flows of dataset v2, every class counting the same. Live: the call in S5 and S6, median late frames over seeds 0 to 2.

| Noise | Accuracy | Macro-F1 | Damage, top label | Damage, expected damage | S5 call late %, top label | S5, expected damage | S6 call late %, top label | S6, expected damage |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 0 | 0.976 | 0.841 | 0.302 | 0.235 | 1.0 | 0.0 | 0.7 | 0.5 |
| 0.5 | 0.679 | 0.346 | 0.691 | 0.577 | 23.0 | 22.1 | 9.6 | 54.4 |
| 1 | 0.610 | 0.263 | 0.858 | 0.789 | 56.2 | 91.6 | 62.1 | 94.3 |
| 1.5 | 0.551 | 0.228 | 0.915 | 0.863 | 86.4 | 87.9 | 94.1 | 94.3 |
