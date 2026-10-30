# Adaptive QoS Engine: Project Log

A record of what was done, how, why, and what was learned, written so the work can be picked up again and so the case study can be written from it. The [PRD](<Adaptive QoS Engine Product Requirements (Teliport S4).md>) says what and why; the [build plan](<Build Plan and Timeline.md>) says in what order; the [technical specification](<Technical Specification.md>) says how each part is built and, in "As built" notes, how it turned out. This file is the story across all of them.

Last updated: night of Oct 13, 2026. Waves 1 to 7 are built, dataset `v1` is sealed, the model is trained, and the smoke run of every scenario on every core configuration has passed. Sections 3 to 10 below were written on Oct 12 and are kept as they were; section 11 is what happened since and overrides them where they differ.

## 1. The project in one paragraph

A home router that works out what each connection is (a call, a game, a download) from packet sizes and timing alone, and puts it in the right priority queue of Linux CAKE. The claim to prove: a small trained model does this better than the hand-written threshold rules existing tools use, which mistake an HD video call or a cloud-gaming stream for a bulk download because all three send many packets. Entry for Tata Elxsi Teliport Season 4. Two people: Systems (testbed, Go controller, Linux networking) and ML (data, classifier, experiments, report).

## 2. Where it stands

| Wave | What | State |
| --- | --- | --- |
| 1 | Pinned versions, interface contracts | Done |
| 2 | Testbed, traffic generators, real-traffic captures | Done; captures became a larger data effort (section 6) |
| 3 | Baselines and metrics, feature extractor, experiment runner | Done |
| 4 | Dataset, threshold rules, enforcer, watchdog | Done; dataset `v1` sealed Oct 12 |
| 5 | Classifier, damage table, decision engine, safety shield, dashboard | Done. Models trained on `v1`, LightGBM chosen. The damage table has two seeds per cell. The dashboard is written but not yet seen live |
| 6 | Controller integration, go/no-go, scenarios | Done. The go/no-go on `v1` says go. S1 to S8 and S10 all run |
| 7 | Reclassifier, verifier, capacity estimator, intent leases | Done (section 11) |
| 8 | Experiments | The smoke gate has passed, 45 of 45 runs valid. The full matrix and the A/A runs are written as matrix files. Ablations and demo not started |
| 9 | Report | The statistics tool is built. Charts, the written report and slides not started |

The work is in git from Oct 13 (`main`, not pushed). Datasets, results, binaries and the random forest's model file are left out.

## 3. Results so far

These are measured, on the reference laptop (Fedora 43, kernel 7.2.8, 16 cores).

**CAKE against no QoS (chunk 3A).** Scenario S1: a video call, then a large download, 200 s, same traffic both times.

| | No QoS (B0) | Plain CAKE (B2) |
| --- | --- | --- |
| Call latency toward the home, p95 | 507 ms | 10.4 ms |
| Call frames later than 100 ms | 4,157 of about 5,400 | 0 |
| Download throughput | 94.3 Mbit/s | 89.5 Mbit/s |

CAKE costs about 5% of throughput and removes the delay. This is the floor the classifier must beat, and it is a high one: plain CAKE already protects a call from one download. The classifier has to earn its place in the harder scenarios.

**Testbed accuracy (2A).** The emulated bottleneck measures within 0.02% of the configured rate in both directions.

**Generators hold their timing (2B).** Cloud gaming at 25 Mbit/s, 2,600 packets per second, from Python: packets leave 85 to 113 µs late at the 99th percentile, no frame misses.

**Extractor: one code path (3B).** Live in the router and offline from a capture, the feature records were identical for all 120 flows. The kernel sent 1,082 packets for 180 flows, never more than 8 per flow.

**Marking works as designed (4C).** A conntrack mark of 1, 2, 3 or 4 puts a flow in Bulk, Best Effort, Video or Voice, upload and download, IPv4 and IPv6. A deliberately corrupted change is caught and undone.

**Watchdog (4D).** Controller killed or frozen: router back to plain CAKE in 2.05 to 2.14 s over 20 trials. Limit is 3 s.

**Rules, first look (4B). Not to be quoted.** On an early small dataset and a placeholder damage table: qosify's default thresholds put most gaming flows and half the video calls in Bulk. Tuning the thresholds on training data switched both rules off: the best threshold rules were no rules, which is plain CAKE. An early sign the thesis holds; the numbers will move.

**The pipeline works end to end (6A).** In the testbed, on a short S1, with the controller running: the call was put in Video at 0.0 s and the download's four connections in Bulk as they opened, each with a plain reason ("Looks like a video call (100% confident); the Video tin has the lowest expected damage."). All three live configurations (rules, model by top label, model by expected damage) labelled every flow; no read-back failed. With the rules, CAKE's Bulk tin held a queue from the moment the download was labelled and none before.

**The safety shield holds (5D).** 10.9 million generated proposals in five minutes, none passed outside the bounds. Six bugs planted on purpose, one per bound, were each found by the fuzzer in under a second.

**Where a tin matters, and where it does not (5B). The finding of the night.** One flow was forced into each tin beside a call, a stream, a download and a peer-to-peer transfer, on a 100 Mbit/s link.

*Alone on its device*, a flow hardly cares:

- A voice call or an online game (about 100 kbit/s) is unharmed in any tin, Bulk included.
- A cloud game, a video call, a stream or a web page is fine in Voice, Video or Best Effort alike. Promotion buys nothing.
- Only Bulk hurts: a cloud game there loses 98% of its packets, a download 87% of its speed, web pages load four times slower.
- Nothing promoted wrongly hurt the others. CAKE's tin limits stop that.

The reason is that CAKE shares the link fairly between devices and, on one device, between flows. A flow alone on its device gets a whole device's share whatever tin it is in.

*With company on its own device*, everything changes. A cloud game on a PC that is also fetching a game update over eight connections:

| Tin the game is in | What gets through | Latency, p95 | Late frames |
| --- | --- | --- | --- |
| Best Effort (what plain CAKE does) | 0.6 of 16.7 Mbit/s | 1,104 ms | 2,456 |
| Video | 16.7 Mbit/s | 15 ms | 11 |
| Voice | 16.7 Mbit/s | 18 ms | 14 |
| Bulk (what the rules do) | 7.0 Mbit/s | 528 ms | 2,449 |

One flow among nine gets a ninth of the PC's share, and fair queueing cannot know which of the nine is the game. This is the case the project exists for: **plain CAKE cannot help, the threshold rules make it worse, and a classifier that knows which flow is the game fixes it.** It is also an ordinary evening: a game update, a cloud backup or a file-sharing client running on the machine someone is playing or calling on.

The measured table, mean of three conditions (alone, crowded, and a narrow uplink shared with a call on the same device; see below), one seed each so far. 0 is the class's best tin, 10 the worst cell in any condition:

| True class | Voice | Video | Best Effort | Bulk |
| --- | --- | --- | --- | --- |
| Voice call | 0 | 0 | 0 | 0 |
| Game, cloud game | 0 | 0 | 2.5 | 5 |
| Video call | 2 | 0 | 2 | 0.5 |
| Streaming | 0 | 1.5 | 0.5 | 2.5 |
| Web | 0 | 3.5 | 0 | 6 |
| Download | 0.5 | 4.5 | 0 | 7.5 |
| Background transfer | 3.5 | 3.5 | 3 | 0 |

Read by rows: games and calls gain from promotion; a download or a web page belongs in Best Effort and hurts a call if put in Video beside it; a background transfer belongs in Bulk, because anywhere else it takes upload a call on the same device needs. It is not the PRD's illustrative table, but it now agrees with RFC 4594's mapping in every row but two: streaming comes out best in Voice, and a video call cheaper in Bulk than in Best Effort. Both come from the narrow-uplink condition with one seed (two calls on one device, where sacrificing one saves the other) and should be looked at again when the other seeds are in.

What follows from it:

- The damage table is measured under both conditions, alone and crowded, and is their mean.
- With this table the decision engine promotes calls, games and streams, leaves downloads and web traffic in Best Effort, and sends background transfers to Bulk when it is sure of them.
- Scenarios S6 and S7 were rewritten to put the call and the game on a device with company. In the first versions they were alone on their devices, where every method would have tied.
- S1, S3 and S4 are expected to be ties between plain CAKE and the classifier, and are kept to say so.

**The whole system on that case, live (S6 and S7, first runs, draft model).** With the controller deciding, the classifier called the game "a game or other real-time traffic (98% confident)" at 0.0 s and sent it to Voice, and called the call "a video call (97% confident)" and sent it to Video. The bulk connections on the same device went to Best Effort as they opened.

S7, a cloud game on a PC that is also fetching a game update:

| | Plain CAKE (B2) | Threshold rules (B4) | Classifier, top label (O1) | Classifier, expected damage (O2) |
| --- | --- | --- | --- | --- |
| Game latency, p95 | 968 ms | 24 ms | 18 ms | 18 ms |
| Game packets lost | 77% | 17% | 0% | 0% |
| Game frames late | 83% | 19% | 0.2% | 0.2% |

S6, a 5 Mbit/s video call on a laptop that is also sharing files:

| | Plain CAKE (B2) | Threshold rules (B4) | Classifier, top label (O1) | Classifier, expected damage (O2) |
| --- | --- | --- | --- | --- |
| Call latency, p95 | 1,707 ms | 1,649 ms | 17 ms | 25 ms |
| Call packets lost | 91% | 84% | 0.1% | 0.2% |
| Call frames late | 94% | 94% | 1.6% | 4.1% |

S2, the link falling from 100 to 20 Mbit/s under a call and a download (here it is the capacity estimator that matters, not the classifier):

| | Plain CAKE (B2) | Threshold rules (B4) | O1 | O2 |
| --- | --- | --- | --- | --- |
| Call latency, p95 | 505 ms | 504 ms | 21 ms | 19 ms |
| Call frames late | 55% | 56% | 0.9% | 0.9% |
| Download, whole run | 29.4 Mbit/s | 29.5 Mbit/s | 28.7 Mbit/s | 28.6 Mbit/s |

S1 (a call and a download on different devices), S3 (three TVs and a game console) and S4 (a backup filling the upload during a game on another device): all four configurations tied. In S1 and S4 the numbers were the same to the decimal. Every interactive flow in these is alone on its device, and plain CAKE already protects it. These are the ties the damage measurements predicted, and they belong in the report beside the wins.

S5, a work call on a laptop that is also backing up, on a 25/5 Mbit/s link shared with three TVs and a console; a lease for the laptop is requested 60 s in. This one separated the two decision rules, and not in the direction the project expected:

| | Plain CAKE (B2) | Threshold rules (B4) | Top label (O1) | Expected damage (O2) |
| --- | --- | --- | --- | --- |
| Call packets lost | 42% | 54% | 0% | 26% |
| Call frames late | 94% | 93% | 0.7% | 93% |
| Console game latency, p95 | 19 ms | 560 ms | 18 ms | 19 ms |

Both put the call in Video. O1 put the backup in Bulk, because that is its class's tin. O2 left the backup in Best Effort, because the damage table said Bulk helps nobody. But the call needs slightly more upload than the Video tin's half of a 5 Mbit/s link gives it priority for, and for the excess it competes with whatever is in Best Effort. With the backup in Bulk the call wins; with the backup in Best Effort it loses a quarter of its packets. The lease made no difference: the classifier was already sure the flow was a call.

**The table was missing a case, and O2 is only as good as the table.** Neither measured condition had an interactive flow that needed more than its tin's priority share, so nothing in the table rewarded demoting a background transfer. A third condition was added: a narrow uplink shared with a call on the same device. The first attempt at it measured nothing, because with the usual second call in the background the two calls outgrew the Video tin by themselves and were broken whatever else happened. With one call it shows the effect plainly: a backup in Best Effort, Video or Voice breaks the call beside it; in Bulk the call is clean. With the new table O2 sent the backup to Bulk and the call in S5 lost no packets and 0.7% of its frames, the same as O1. This was found by a scenario and fixed in the table, so S5 is no longer an independent test of it.

S10, faults on purpose: two of three probe targets made unreachable for 30 s, then the controller killed.

- **The watchdog works in a real run.** Plain CAKE was back 2.02 to 2.03 s after the controller was killed, in all three controller configurations. The limit is 3 s.
- **The fallback for silent probes did more harm than the fault.** The link was fine; only the probe targets were gone. The estimator first read their silence as delay and cut the rate eight times in 3 s, then, as designed, held both rates at a tenth. The call lost a third of its upload for 30 s and then recovered at 10% per 10 s. The first part is fixed: a reflector answering on time now outvotes ones that say nothing, and on a rerun there were no cuts before the fallback. The second part is the design, and it is questionable: see section 8.

Over the eight scenarios, summing each one's flow costs (0 is fine, 1 is one flow unusable): plain CAKE 0.56, threshold rules 0.69, top label 0.22, expected damage 0.36.

The other traffic in S6 and S7 lost about 4% of its throughput under the classifier. One seed and a model trained on a draft dataset, so this is a first look and not a result; top label and expected damage cannot be told apart on it. It is the first time every part ran together on the case the project is for, and the outcome is the one predicted from the damage measurements. The full table of the first live matrix is `reports/live-first.md`.

**A tight link was tried and dropped as a condition.** On a 40/8 Mbit/s link the generated cloud game, which sends at a fixed rate, lost 50 to 86% of its packets in every tin and filled the link's own buffer for everyone else. A shaper on the receiving side cannot make a sender slow down; a real game would lower its picture quality. That measurement is about the generator, not about tins.

**The capacity estimator follows a drop (7C).** S2, the link falling from 100 to 20 Mbit/s under a call and a download, with the controller running: the drop was detected in 350 ms, the shaper was under the link 1.4 s later, and the call's p95 latency was back to 14 ms within 5 s. It then probes upward every 10 s and backs off, which shows as a 40 ms blip every 20 s. Over the 100 s after the drop the call had 69 late frames; with the first, broken version of the estimator it had about 3,000.

**Models against rules, first look on a draft dataset. Not to be quoted.** Draft dataset, placeholder damage table, validation split, 3,488 flows from 313 sessions:

| | Balanced expected damage |
| --- | --- |
| Rules, qosify's defaults | 2.68 |
| Rules, tuned on our data (which switched the bulk rule off) | 1.20 |
| Random forest, by top label / by expected damage | 0.38 / 0.21 |
| LightGBM, by top label / by expected damage | 0.35 / 0.22 |

qosify's default rules sent 69% of cloud-gaming flows, 56% of streaming flows and 15% of video-call flows to Bulk. Deciding by expected damage beat deciding by top label for both models. The difference between model and tuned rules was -0.99 with a 95% interval of -1.10 to -0.82 (sessions resampled).

**The model has partly learned the lab. This is the main caveat.** Trained without one source and tested on that source, the model does much worse on it: video calls 0.03 with Purdue in training and 1.52 without; ordinary web traffic 0.08 against 0.6 to 1.6. Scores on held-out sessions of sources the model has seen say little about a home it has not seen. More sources per class is the only real remedy, and the report must show this table.

**Real traffic confirms the premise.** In public captures, HD video calls run at 450 to 590 packets per second (Facebook, Hangouts, 2016). In our own captures, a cloud game on JioGamesCloud runs at about 1,160 packets per second and 10 Mbit/s. A rule that says "high packet rate means bulk" sends both to the slow lane.

## 4. Decisions, with reasons

**Conntrack mark selects the tin, not DSCP.** The PRD described rewriting DSCP. The mark is simpler: mark 0 falls through to CAKE's own handling, so an unlabelled flow is plain CAKE by construction, and removing two rules switches the whole system off, which is what the watchdog needs. Setting DSCP from a mark with a map is also not supported by this nftables.

**No `sudo`. The whole testbed is rootless.** One user namespace holds every network namespace. Each namespace is kept alive by a holder process, because `ip netns add` needs root. Everything needed later was checked to work this way: CAKE, tbf, netem, NAT, nflog, conntrack accounting, conntrack mark updates. Under `sudo` the testbed does not start at all; not investigated, not needed.

**Generators decide everything before the run.** A plan is a pure function of scenario, entry and seed. Client and server compute it independently. The same seed gives the same traffic on every configuration, which is what makes comparisons paired.

**No handshake of our own in generated traffic.** The UDP server starts sending when the client's first packet arrives. Nothing is added to a flow's first packets that a real application would not send, since those packets are what the classifier reads.

**One extractor for everything.** Training data and live data go through the same Go flow-table code: captures, the live router, and public datasets reduced to flow tables. Without this the model would be trained on one definition of a feature and run on another.

**Public datasets became the main source of real traffic (Oct 11, by the PRD's owner).** The PRD had them as an optional cross-check. Our first own captures were unusable, and public sets offer more applications and more sessions. Own captures now serve the final test.

**Flow tables, made on Kaggle.** The reference laptop has about 9 GB free and a 4 Mbit/s line. Raw public captures are 10 to 850 GB. A Kaggle notebook downloads a set at datacentre speed and keeps, per flow, the first 64 packets (time, size, direction) and per-second counts: 25 GB becomes 2.4 MB. No payload, no addresses. Kaggle is used as a fast pipe and a big disk; no training happens there.

**Every flow counts once, and classes count equally, when scoring.** Scoring by record let a few hour-long calls decide everything. Scores are now per flow, with a class-balanced figure used for tuning and selection, because the dataset's class mix is an accident of what could be collected.

**A flow is used only if the capture saw it start.** The classifier reads a flow's first eight packets. A flow already running when a capture began has none. Likewise a flow whose first packets include one larger than 1500 bytes: receive offload on the capturing machine merged it.

**Sessions are the unit of splitting.** Train, calibration, validation and test never divide a capture session. The two ends of one call recorded as two files are one session. Exact copies of a file under a second name are dropped.

**The final test set is written once.** It goes to its own file with a hash of its session list. A build that would change it is refused.

**Dataset drafts until the data is in.** `v1` is sealed only when the last downloads have landed.

**The bar for demotion is on "belongs in Bulk", not on one class (5C).** The design put the bar on the probability of low-priority data alone. It is now on the summed probability of every class whose least damaging tin is Bulk according to the damage table.

**Calibration is chosen by what it does to decisions (5A).** The design chose the calibration method with the lowest calibration error. That error is an average over flows, most of them ordinary web traffic, and on a draft dataset it picked the method that decided worst (0.29 against 0.21). The method with the lowest class-balanced expected damage is kept; calibration error is still reported.

**The controller refuses to start without the classifier (6A).** The design had it start anyway and leave flows unlabelled. An experiment that quietly ran with no classifier would be a plain-CAKE run filed under another name. Once running, losing the service only leaves flows unlabelled.

**The controller asks again every 5 seconds (6A).** The rules need a packet rate, and a flow's first eight packets have none, so the rules could not work on first packets alone. Every live flow is re-examined with a window record; a labelled flow is moved only when two answers in a row agree.

**What counts as damage for a download (5B).** A slower download is not the same harm as an unusable game. Costs for data transfers are the share of their usual speed lost, with thresholds: a download somebody is waiting for is fine down to three quarters of its speed; a backup or peer-to-peer transfer only suffers near starvation, since that is what Bulk is for. These are judgements, written in `configs/damage_scoring.yaml`. The raw measurements are kept, so other judgements can be tried without new runs.

**The damage table's background fills every tin, and sits in its own tins (5B).** A peer-to-peer transfer was added in Bulk, or a flow demoted there would be measured in an empty lane. Harm to the others is summed, not averaged: the damage is to the whole home.

**Three conditions for the damage table: alone, crowded, and a narrow uplink.** The first results had every subject flow alone on its device, where fair queueing protects it in any tin. A tin matters when the flow shares its device with many others, so that is measured too and the table is the mean. A tight link was tried first as the second condition and dropped (section 3). The third, a call on the same device on a 25/5 Mbit/s link, was added after S5 showed that nothing in the table rewarded demotion; its first design, with a second call in the background, was overloaded whatever the subject did and is kept out of the table.

**The damage table is rounded to half a point.** A few seeds cannot tell 0.05 from 0.10, and the decision engine picks the lowest number. With a finer table, noise chose between tins that are really the same; one draft would have sent backups to the Video tin. Equal cells go to Best Effort.

**When the table cannot separate tins, follow RFC 4594, but never into Bulk.** Measured alone on a device, a call does as well in Best Effort as in Video, so the table ties them. The first tie rule chose Best Effort, where the same call starves once its device gets busy. Promotion was measured to cost nobody anything, so a tie now goes to the class's standard tin. Bulk is the exception: demotion needs evidence.

**Scenarios S6 and S7 put the interactive flow on a busy device.** The PRD only says "HD video call" and "cloud gaming stream". Alone on their devices they are protected by plain CAKE and every method ties.

**Contracts added.** The request and response between controller and ML service; a `page_load_ms` metric, because a web page could not be scored from throughput; configurations `D1` to `D4`, which exist only to measure the damage table.

## 5. What was built

| Chunk | What | Where |
| --- | --- | --- |
| 1A, 1B | Pinned versions; seven JSON Schema contracts with Go types and tests on both sides | `VERSIONS.md`, `contracts/` |
| 2A | Rootless testbed: hosts, router, isp (rate), core (delay), server | `cmd/aqos-testbed`, `internal/testbed` |
| 2B | Eleven traffic profiles with exact QoE measurement | `ml/aqos_ml/gen` |
| 2C | Capture recording and review tool; one script for the sudo steps | `ml/aqos_ml/capture.py`, `scripts/wave2-sudo.sh` |
| 3A | Baselines B0 and B2; CAKE reader; Prometheus exporter; compose file | `configs/baselines`, `internal/cake`, `internal/exporter`, `dashboards/` |
| 3B | Feature extractor, offline and live; nflog and conntrack over netlink | `internal/extract`, `internal/netfilter`, `internal/capture`, `cmd/aqos-extract` |
| 3C | Experiment runner with validity checks, retry and resume | `ml/aqos_ml/run` |
| 4A | Dataset builder; training scenarios; Kaggle flow-table script and notebook | `ml/aqos_ml/dataset`, `scenarios/train`, `kaggle/` |
| 4B | Threshold rules; shared scoring code; placeholder damage table | `ml/aqos_ml/rules.py`, `ml/aqos_ml/evaluate.py`, `configs/damage_table.yaml` |
| 4C | Enforcer with read-back and restore; hand tool | `internal/enforce`, `cmd/aqos-ctl` |
| 4D | Watchdog, standard library only | `cmd/aqos-watchdog`, `internal/heartbeat` |
| 5A | Shared feature function; training with calibration; saved model; ML service | `ml/aqos_ml/features.py`, `train.py`, `model.py`, `serve.py` |
| 5B | Damage table measurement and scoring | `ml/aqos_ml/damage.py`, `configs/damage_scoring.yaml`, `scenarios/damage/` |
| 5C | Decision engine: expected damage and top label | `ml/aqos_ml/decide.py` |
| 5D | Safety shield with fuzz tests | `internal/shield`, `configs/shield.yaml` |
| 5E | Dashboard, generated and checked against the metric contract | `dashboards/build.py`, `dashboards/provisioning/` |
| 6A | The controller; live configurations B4, O1, O2 in the runner | `cmd/aqosd`, `internal/controller` |
| 6B | Head-to-head report tool (go/no-go) | `ml/aqos_ml/compare.py` |
| 6C | Scenarios S2 to S7 and S10; the runner's lease and fault events; the watchdog in every controller run | `scenarios/`, `ml/aqos_ml/run/runner.py` |
| 7D | Intent leases: store, API, form, runner event, scenario S5 | `internal/intent`, `scenarios/S5.yaml` |
| 7C | Capacity estimator: logic, delay prober, reflectors, the rate path through the shield. Follows a capacity drop in the testbed; the silent-reflector case is tested in simulation only | `internal/capacity`, `cmd/aqos-reflector` |
| Data | Reader for the Purdue video-call set | `scripts/vca_flow_tables.py` |

Checks: `make check` (about 200 Python tests and seven Go packages, about a minute), `make testbed-check` (16 tests against the real testbed, about 7 min) and `make fuzz` (the shield, 5 min).

## 6. The data story

This took far longer than the two days the plan gave "real-app captures", and taught the most.

**Own captures, first try: all three unusable.** A Meet call, Roblox and a download. The recording script told the user to start the app and then press Enter, so every main flow was already running and its first packets were missing. Receive offload on the Wi-Fi interface merged packets up to 14,548 bytes. Everything in each capture was accepted under the session's label, background traffic included. Roblox is also not cloud gaming. The tool was rewritten to record first, switch offload off, flag unusable flows and refuse them.

**Public datasets: each carries its lab's fingerprint.**

| Dataset | Use | What is wrong with it |
| --- | --- | --- |
| VNAT (MIT Lincoln Lab, 2020) | Not used for training | Lab LAN: connection reply in 0.13 ms. All streaming through a proxy. File transfers at 350 Mbit/s over SSH. Three voice sessions. "Skype chat" is mostly Windows domain traffic |
| ISCX VPN-nonVPN 2016 (UNB) | Calls: the best source | Real internet timing (38 ms). Streaming and bulk flows have merged packets. 2016 applications. `a`/`b` files are two ends of one call |
| ISCX Tor/non-Tor 2016 (UNB) | Browsing, P2P, streaming, file transfer | Everything goes through a gateway on the LAN: reply in 0.25 ms, 99.98% of bytes TCP, voice calls included. Its calls are left out. Six of 44 files are copies of others |
| Labayen 2020 (Mendeley, CC BY 4.0) | Browsing, bulk, video on a real access line | Mendeley refuses Kaggle's machines, so it is fetched at home. Its small per-packet CSV omits handshake packets, so the full captures are needed |
| UNSW 2025 cloud gaming | Cloud gaming on four platforms | 849 GB; a 157 GB sample is taken on Kaggle |
| Purdue VCA 2026 | Video calls on current Zoom, Meet, Teams, Webex: 1,334 calls | One CSV row per packet, not captures, so it has its own reader. No TCP flags, so only its UDP flows are used. Sizes include the Ethernet header. Recorded under emulated bandwidth limits, some very low |
| 5G Traffic Datasets, GAViST5G | Online gaming | Behind an IEEE DataPort subscription; not obtained |

The lesson for the report: "public" does not mean "better". If streaming always arrives through a proxy and downloads always over SSH, a model learns the lab. Hence several sources per class, per-source checks before use, and the generators and own captures kept.

**Own captures, second try: three good cloud-gaming sessions.** JioGamesCloud, played in the laptop's browser: a racing game (10.6 Mbit/s), Ghostrunner (10 Mbit/s) and a puzzle game (0.5 Mbit/s), each with its video, audio and input flows. One went to training, one to validation, one to the final test. They are the only data from a real home line and a 2026 application.

**Purdue changed the weakest class into the strongest (Oct 12).** Video calls had about 60 training flows from 2016 applications. The Purdue set added 2,185 flows from 1,304 calls on the four applications people use now. The Labayen video captures finished downloading and added streaming on a real access line.

**Labayen bulk arrived overnight (Oct 12).** Nine real downloads at 5 to 55 Mbit/s on an access line, every one seen from its start and none with merged packets: the first usable real bulk data. The second of the two bulk archives is not a gzip file as published (our copy's hash matches the publisher's), so it is unused. Draft 4 of the dataset includes the nine. Only the UNSW cloud-gaming table, waiting in Kaggle's Output tab, is missing before `v1` can be sealed.

**The dataset at draft 3.** About 25,000 flows from six sources. Still thin: online gaming and cloud gaming (the UNSW table from Kaggle fixes cloud gaming), voice calls (about 120 flows, all 2016), and bulk downloads on a real line (Labayen bulk is still downloading).

**The dataset at draft 2.** 21,900 flows from five sources: the testbed (95 runs of 48 randomised scenarios with the live extractor on), ISCX VPN-nonVPN, ISCX Tor/non-Tor, Labayen (web and interactive so far) and own captures. Every class has real captures in the final test. Still thin: a hundred or so flows each for calls, gaming, streaming; the final test has three flows each for gaming and video calls.

## 7. Mistakes worth remembering

- The capture script recorded after the activity started. Cost: the first three captures.
- The same script deleted a capture when the user pressed Enter at the keep prompt. Cost: one good, paid-for cloud-gaming session. It now deletes nothing.
- The same script ran `aqos-testbed down` as a side step and killed a training run in progress. That step is gone.
- Scoring counted records, not flows.
- The web-browsing generator could end without loading a page; it now loads one at once.
- The runner's `results/current` link made the dataset count the latest run twice.
- A test helper called conntrack's set-mark inside a dump on the same socket and hung.
- Python's zipfile cannot read Deflate64, which large Windows-made zips use; the first Kaggle run died on the second archive.
- `pkill -f` with a pattern that also matched the shell running it. Several times.
- TCP remembers the last connection's slow-start threshold per destination; a second bottleneck check read 9% low until the metrics were flushed.
- The Purdue reader measured "did the capture see this flow start" from the first UDP packet, not from the start of the capture. 58% of flows were wrongly marked as already running. Found by auditing the output before using it.
- Choosing the calibration by calibration error picked the worst-deciding one (see section 4).
- The first damage scoring counted a download slowed in Bulk the same as a ruined game, and counted two downloads sharing a link fairly as each harming the other. Found by reading the first seed's table before the full run finished.
- The decision engine's reason said "not sure enough for Best Effort" when it had chosen Bulk for a download. Seen in the first live run.
- The capacity estimator passed every test against a simulated link and failed four times on the real one. (1) Answers slower than the probe interval were counted as lost, so a full buffer looked like dead reflectors and both rates were sent to the minimum. (2) The shield's limit of half per step left the rate at 20.5 Mbit/s on a 20 Mbit/s link, and a rule meant to stop over-cutting ("the shaper is already the limit") then refused to cut the last half megabit; the queue stayed full for 100 s. (3) It read CAKE's counters once a second, and when a download's eight connections started at once it sized its cut by the quiet second before the burst, halving the rate on a healthy link. (4) Found by S10: probe targets that were unreachable read as ever-growing delay for the 3 s before they counted as silent. All four are now regression tests. The simulation was written by the same person as the code and shared its blind spots.
- A Unix socket path may be at most 108 bytes. The ML service's socket sat in the run directory and stopped working when a results directory had a long path.
- A unit test that expected a configuration to be refused started a real run once that configuration existed, and took the testbed away from the damage measurement running at the time. Cost: one cell, rerun later. There is one testbed; the runner now takes a lock on it and a second run is refused.

## 8. Known gaps and open questions

- **The damage table has one seed per cell.** `configs/damage_table.yaml` is the measured table of three conditions (the PRD's illustration is kept as `damage_table.illustrative.yaml`). Seed 2 is running. The offline comparison in `reports/` was made with the two-condition table and is to be redone.
- **The damage table depends on which conditions are measured.** Two conditions said never demote; a third reversed that. The table is a model of what matters, chosen by us, and a scenario found its first gap within hours. The report should present the conditions as part of the method and say so.
- **`reports/live-first.md` was made with the two-condition table.** Only S5 on O2 was rerun with the three-condition one. The whole matrix is to be rerun.
- **Expected damage did not beat top label in the first live matrix.** They tied on five scenarios and top label won S5. The decision rule's advantage rests entirely on the table being right about what matters. That is a finding to report, not to hide: O1 follows RFC 4594's fixed mapping, which already encodes decades of judgement about what belongs where.
- **The claim is narrower than the PRD's, and sharper.** The classifier beats plain CAKE when an interactive flow shares its device with many bulk flows, and beats the rules whenever they mistake a call or a game for bulk. Elsewhere it ties with plain CAKE. The report should say exactly that.
- **The generated calls and games do not adapt.** Short of bandwidth, a real one lowers its quality; ours lose packets. So "plain CAKE breaks the game" in S7 means "plain CAKE leaves the game a ninth of what it needs". True, but the size of the damage is the generator's.
- **The model has partly learned the lab** (section 3). Needs more sources per class, and the leave-one-source-out table in the report.
- **The random forest takes about 30 ms to classify one record alone**; LightGBM about 2 ms. The service answers requests in batches, which hides this, but the forest has little room inside the 50 ms deadline.
- **The dashboard has never been seen running.** Grafana and Prometheus are about 700 MB to pull.
- **The generated cloud game does not adapt its rate.** On a link that cannot carry it, it is broken in every tin and floods the link's buffer, which a real game would not do. Those measurements are left out of the damage table. It also means scenario S7 must be run on a link that can carry the game, or the result is about the generator, not about QoS.
- **The estimator probes upward into the link every 10 s** once it is below the configured rate, and each probe that overshoots costs a 40 ms blip. Whether that is acceptable, or the raise should be gentler near a rate that failed before, is for S2's full analysis.
- **What should the estimator do when it cannot hear its probes?** The design says go to a low fixed rate and hold it. In S10 that broke a call on a healthy link, and climbing back at 10% per 10 s takes minutes. The project's first principle is that with nothing to go on the system behaves like plain CAKE; by that principle it should return to the configured rate. The design's answer is safer if the link really has failed, worse if only the probe targets have. To decide.
- **Recovery after any deep cut is slow by design.** The shield allows one 10% raise per 10 s. From a tenth of the rate back to all of it is four minutes.
- **S8 is not written:** it needs generators that change during a run.
- **The controller does not post its actions to Grafana**, and the starvation detector has no input yet.
- **Ordinary online gaming has no real data**, only the testbed's imitation.
- **Video calls in the final test are three flows from 2016 applications.** A Meet or WhatsApp call capture would help more than any other single addition.
- **A flow that loses first packets to a full buffer gets no label.** The contract cannot say "first packets unknown". Needs a contract change or stays.
- **Contract changes awaiting the ML owner's agreement:** `frame_miss` carries a direction; the run manifest has no field for the testbed mode, which is in `environment.json` instead.
- **CPU pinning** exists in the testbed tool and is not used by the runner.
- **Licences.** Labayen is CC BY 4.0. The UNB sets ask for citation. VNAT, Purdue and UNSW state none. All are to be cited; none is redistributed, only derived flow tables are kept.
- Watchdog was tried 10 times each way; the plan says 20.

## 9. Running things

```
make check                    # unit tests
make testbed-check            # tests against the real testbed
make build                    # binaries into bin/

bin/aqos-testbed up scenarios/S1.yaml ; bin/aqos-testbed down
PYTHONPATH=ml .venv/bin/python -m aqos_ml.run one --scenario S1 --config B2 --seed 7
PYTHONPATH=ml .venv/bin/python -m aqos_ml.run matrix scenarios/train/matrix.yaml --results results-train
PYTHONPATH=ml .venv/bin/python -m aqos_ml.dataset build --name v1

PYTHONPATH=ml .venv/bin/python -m aqos_ml.train --dataset data/dataset/v1.jsonl.gz     # both models, into models/
PYTHONPATH=ml .venv/bin/python -m aqos_ml.compare --dataset data/dataset/v1.jsonl.gz   # go/no-go report, into reports/
PYTHONPATH=ml .venv/bin/python -m aqos_ml.damage run --seeds 1-3 ; ... damage score    # measure the damage table
PYTHONPATH=ml .venv/bin/python -m aqos_ml.run one --scenario S6 --config O2 --seed 1   # a live run with the controller
make fuzz                     # the shield's fuzz tests, 5 minutes
.venv/bin/python -I scripts/vca_flow_tables.py data/ext/purdue-vca/pam2026.zip data/public/purdue-vca

scripts/fetch-data.sh status  # resumable downloads (datasets.tsv), survive Wi-Fi drops
scripts/labayen-local.sh      # flow table from the Labayen archives that have finished
scripts/wave2-sudo.sh cloud_gaming video_call   # record own captures
.venv/bin/python kaggle/build_notebook.py       # rebuild the Kaggle notebooks
```

Where data lives:

| Path | Contents |
| --- | --- |
| `data/public/<set>/` | Flow tables and audits from Kaggle or made locally |
| `data/ext/` | Link to `aqos-data/` on the Windows partition (312 GB free); large raw downloads. Must be mounted from the file manager after each boot |
| `data/raw/` | Own captures and their session files |
| `results-train/` | The 95 training runs |
| `results-damage/` | The damage table's runs; `run.log` shows progress |
| `models/` | Trained models, one directory per version; `chosen-<dataset>.txt` names the one to use |
| `data/dataset/` | Built datasets; `draft*` are throwaway, `v1` is to be sealed |
| `kaggle/registration.local.json` | Details for dataset registration forms; git-ignored |

## 10. Next steps

**Running when this was written (the night of Oct 12).** Two user services, both resumable and safe to stop:

- `aqos-live`: the first live comparison, `configs/matrix-live.yaml`, into `results-live/`. Finished at 05:27, all 32 runs valid; the table is `reports/live-first.md`.
- `aqos-after`: measuring seed 2 of the damage table's three conditions (120 cells, about 2.6 hours, started 07:21), then writing `configs/damage_table.measured.yaml`. Progress in `results-damage/run.log`.

`systemctl --user status aqos-after` shows it; `systemctl --user stop aqos-after` frees the testbed, and `python -m aqos_ml.damage run --seeds 1-2` picks up where it stopped. There is one testbed: a second run started while one of these is going is refused.

**Then, in order:**

1. Collect the UNSW cloud-gaming flow table (`flows-unsw-cloud-gaming-2025.jsonl.gz` and its audit) from Kaggle's Output tab into `data/public/unsw/`, and look at the audit before using it. Everything else is in. Then build and seal dataset `v1`: `python -m aqos_ml.dataset build --name v1`.
2. When the damage run has finished, read `configs/damage_table.measured.yaml` (two seeds) against the one-seed table in section 3, in particular the two odd cells named there, and copy it over `configs/damage_table.yaml`.
3. Train and compare on `v1`: `aqos_ml.train`, then `aqos_ml.compare`. That report is the go/no-go. Open the test set (`--final`) once, at the very end.
4. Decide what live B4 is. On draft data, tuning the rules switched the bulk rule off. The matrix so far ran qosify's own thresholds, the documented failure. `python -m aqos_ml.rules` writes tuned ones to `configs/rules.yaml` if both are wanted.
5. Read `reports/live-first.md`, then run the matrix again with the `v1` model and more seeds.
6. Pull the dashboard containers and look at it during a run.
7. Still to build: the reclassifier by class envelopes (7A), the verifier and rollback (7B), S8, and the stretch items.

**Worth a decision from both of you before the report is planned:** the claim (section 8, first item), and whether the generated calls and games should adapt their rate, since every "plain CAKE breaks it" number depends on them not doing so.

## 11. Oct 13: wave 7 finished, the smoke run, and what it changed

**Built.** S8 and the generators' planned changes; the reclassifier by class envelopes (7A); the QoE verifier and rollback (7B); the statistics tool for a matrix (`aqos_ml.report`); matrix files for the smoke gate, the full matrix and the A/A runs; `scripts/watch-matrix.sh` to watch a matrix from a terminal. The details are in the technical specification's "As built" notes for each.

**Dataset `v1` and the go/no-go.** Sealed Oct 12, with the UNSW cloud-gaming table in. LightGBM chosen. On the validation split, with the two-seed damage table and the Voice bar below: balanced expected damage 0.141 against the tuned rules' 0.745 (difference -0.604, interval -0.693 to -0.517). Go. The final test set is still unopened.

**The damage table, second seed.** All 240 cells valid. Scored as before, it said a video call belongs in Bulk: in the narrow-uplink condition two calls share a device, ruining the subject call frees room for the other, and that relief outweighed its own loss. Relief to the others is now credited only to a flow that gives up next to nothing for it (`relief_free: 0.1`). A backup in Bulk still earns it; a call does not. The table now agrees with RFC 4594 in every row except streaming, which it puts in Voice.

**The smoke run: every scenario once on B0, B2, B4, O1, O2.** 45 of 45 valid. One seed, so a first look. Summed cost per scenario, 0 for every flow fine and 1 for each flow unusable:

| Scenario | No QoS | Plain CAKE | Rules | Top label (O1) | Expected damage (O2) |
| --- | --- | --- | --- | --- | --- |
| S1 download during a call | 1.00 | 0.00 | 0.00 | 0.00 | 0.00 |
| S2 link drops to a fifth | 1.00 | 1.00 | 1.00 | 0.00 | 0.00 |
| S3 three TVs and a console | 1.19 | 0.19 | 0.19 | 0.19 | 0.19 |
| S4 backup during a game | 1.00 | 0.00 | 0.00 | 0.00 | 0.00 |
| S5 work call on a busy evening | 2.19 | 1.19 | 2.23 | 1.19 | 0.20 |
| S6 call on a laptop sharing files | 1.29 | 1.00 | 1.00 | 0.49 | 0.03 |
| S7 cloud game during an update | 1.13 | 1.12 | 1.12 | 0.12 | 0.12 |
| S8 flows that change | 2.16 | 1.12 | 0.74 | 0.12 | 0.12 |
| S10 probes silent, controller killed | 1.00 | 0.00 | 0.00 | 0.00 | 0.00 |
| All nine | 11.96 | 5.61 | 6.28 | 2.11 | 0.66 |

The full system is never worse than plain CAKE and better in five of nine scenarios; in the other four every method ties, as the damage measurements predicted. The threshold rules are worse than plain CAKE overall. For the first time expected damage beats top label on live traffic, in S5 and S6. The O2 column is the second attempt; see next.

**O2 lost S7 on the first attempt, and why.** The game had 51% of its frames late under O2 and 1% under O1. The measured table's Voice column is nearly all zeros: each kind of flow was measured there alone, found harmless, and so the table cannot say what happens when all of them are sent there together. O2 put a stream and a flow it called "ordinary web traffic (54%)" in Voice beside the game; together they outgrew Voice's quarter of the link. The engine now needs to be over 50% sure a flow is a call or a game before Voice is allowed (`voice_bar`), the mirror of the bar on Bulk. With it O2 matches O1 on S7 and beats it on S5, where the streams had also gone to Voice. On paper the bar costs score (0.141 against 0.075 offline), because the table itself rewards Voice. This is the third time a scenario found a gap in the table; the earlier runs are kept in `results-smoke-before-voice-bar/`.

**S5 under O1: one flow under the bar.** The backup's two connections were both called background transfers, one at 85% and one at 70%. Top label demotes only above 80%, so one stayed in Best Effort and took the upload the call needed. O2 demoted both, because for it the question is the expected damage, not one threshold. The first live run had both above the bar with the draft model, which is why O1 won S5 then.

**The estimator and silent probes: decided.** With fewer than two reflectors answering it now leaves the rates where they are. S10 costs nothing under O1 and O2 any more (it cost 1.19 in the first live run). `aqosd -deaf minimum` gives the old behaviour back.

**Envelopes fit unchanged flows badly.** Checked against the range of their own true class, 38% of validation video-call flows and 48% of game flows would be sent back to the classifier at least once though nothing changed (`reports/envelopes-v1.md`). A back-off was added: a flow that stays outside is asked about after 5 s, then 10, up to 40. In a live S8 run 5,378 window records led to 161 questions.

**S8 does not stress the reclassifier.** Camera off leaves the call in Video; the game is in Voice in the lobby and in the match; the paused download sends nothing to look at. The relabels in S8 are the reclassifier correcting first labels (a file-sharing UDP flow called a voice call, moved to Bulk 2.3 s later). A scenario where the right tin changes is still to be written.

**The verifier in real runs.** Judged up to 31 changes per run; rolled back a rate raise in S7 after the Voice and Video tin delay rose from 26 ms to 104 and 67 ms. No sign of it undoing a good promotion, but one seed says little; the A/A runs are what will.

**Mistakes, added.**
- `pkill -f` and `pgrep -f` with a pattern that matched the shell running them. Twice more.
- Python's output is buffered when it goes to a file: a matrix's log stayed empty for hours. `python -u`, or watch the result directories.
- A test wrote `voice_bar=0.0` to mean "no bar"; the comparison is "not above the bar", so zero still bars everything with no probability.

**Still open.**
- The damage table keeps being wrong about what it did not measure. Three gaps found by scenarios so far (no reward for demotion; a call sacrificed for another; Voice as free). The report must say the table is a model of what matters and show these.
- O1's 80% bar and the model's calibrated 70% for an upload's first packets: top label is fragile wherever one flow sits near the bar.
- No own video-call capture in the final test; the dashboard has not been seen live; no charts (matplotlib is not installed).

## 12. Oct 14: an outside review of the interim case study, and the plan it set

A review of the interim page (six seeds) raised points that hold whatever the seed count. Accepted, and now the plan for the final numbers:

1. **Add cake-autorate as B3** on S2 and S5. S2 compares an adaptive shaper with a fixed one, which any adaptive shaper wins.
2. **Tuned on the test.** The relief rule, the Voice bar and the download-first estimator fix were all found by evaluation scenarios; seed 1 was the smoke seed and is also a matrix seed. After freezing: rerun every configuration on fresh seeds 10 to 19, and run one or two scenarios written after the freeze, once, reported whatever happens. Seed 1 is marked as a development seed.
3. **Self-graded metric.** The cost scale is what the damage table, and so O2, is built from. Raw measurements (late frames, p95/p99 latency, jitter, loss, throughput, Jain's index) are the primary evidence; cost is the summary. The page now does this for late frames and latency.
4. **Non-adaptive generators** overstate how badly CAKE treats a squeezed call. Drive one call generator through a real congestion controller (Pion's GCC-style estimator in Go was suggested).
5. **Generalisation.** The leave-one-source-out table is now on the page (video calls 1.63, cloud gaming 2.13, downloads 2.23 with the source left out). Measure how often a relay switch starts a new flow with probe packets, and how long the call sits in Best Effort until the reclassifier catches it.
6. **Expected damage against top label** rests on S5 alone live. The weakened-classifier ablation is essential, not optional. The Voice bar is a hand rule and the page now says so.
7. **Statistics on six seeds** are unstable; ten seeds, Wilcoxon with Holm, and the A/A band on every chart.

Also missing against the judging criteria: CPU per component at 25, 50 and 100 Mbit/s; S9; fairness and starvation evidence; IPv6 end to end; the VPN case. Done today: classifier accuracy, macro-F1, per-class recall and calibration error on the page; a sensitivity check of the damage table (`python -m aqos_ml.sensitivity`, `reports/sensitivity-v1.md`): scaling every cell by 0.75 to 1.25 moves at most 2.1% of placements, but moving every cell by up to one point moves up to 39%, because the many zero cells are where the table is weakest.

## 13. Run plan to Oct 16 (deadline for submission: Oct 23)

Engineering is to finish by the evening of Fri Oct 16, so the writing team has a week with final numbers. Worked through autonomously; each step is ticked here when done.

- [x] Offline, while the matrix runs: demotion to Bulk needs evidence besides the model's confidence, tested on held-out sources. Done: no demotion on a flow's first packets, none for UDP. Held-out video calls, cloud games and downloads went to Bulk 0% of the time (16%, 2%, 10% before). Cost: UDP peer-to-peer stays in Best Effort; offline balanced damage 0.141 to 0.242, the rules 0.745
- [x] Offline: trees, calibration and decision rule ported to Go (`internal/classify`, `cmd/aqos-classify`). On 2,000 dataset records the probabilities agree within 4.4e-16 and every tin is the same; over the socket, the same tin and reason for 300 of 300. About 6 µs per classification against 2 ms in Python. The final runs use it for O1 and O2
- [x] Offline: dataset v2 with the six WhatsApp calls; LightGBM retrained (accuracy 0.972, macro-F1 0.80, temperature calibration); go/no-go go (0.239 against 0.745)
- [x] Offline: cake-autorate v3.2.2 set up as B3, unmodified, in a private mount namespace; ran and set rates in a throwaway namespace. First testbed run is in the freeze's smoke check
- [x] Offline: S11 written (50/10 line, call beside a download, console game beside an update, voice call beside a backup), not run before the freeze. Also: CPU per component recorded in every run, the weaker-classifier ablation option, the final-run matrix files and `scripts/final-runs.sh`
- [x] Matrix (`results-core`) finishes, about Thu 02:30; A/A runs (`results-aa`) about Thu 07:45. Done: 450 of 450 and 90 of 90 valid (Thu 07:28)
- [x] Freeze: unattended, by `scripts/freeze.sh`, started Wed 17:38 and waiting for the matrix and A/A runs. It fast-forwards main to the `freeze` branch, builds, runs `make check`, three smoke runs, then `scripts/final-runs.sh`. Its log is `freeze.log`; a failure is written to `FREEZE-STATUS`. Done: frozen at 4988d91, Thu 07:41
- [x] Combined run, about 23 h: O1 and O2 on seeds 0-9 (they replace the pre-freeze ones; B0, B2, B4 are unaffected and kept), B2/B4/O1/O2 on fresh seeds 10-14, B3 on S2 and S5, the new scenario once. Done Fri 06:46; 0 invalid. Frozen system on seeds 0-9: against plain CAKE 49 better, 41 tied, 0 worse; against the rules 0 worse; against cake-autorate 20 of 20 better. Fresh seeds: 21 better, 29 tied, 0 worse. S11 ties CAKE (CAKE already fine); the rules ruin its call. One fresh S7 seed: a 24.9 Mbit/s cloud game outgrew the Voice tin's quarter share (72% late, the same as CAKE)
- [x] Weakened-classifier ablation; CPU per component. Done. Offline expected damage beats top label at every noise level; live (3 seeds) it does not, so no live benefit is claimed. CPU: controller 3.6% of one core, classifier 0.04%
- [x] Open the sealed test set once; final report, charts, case study page and handover marked final. Done Fri 12:00. Sealed test: 1,213 flows, 183 sessions, accuracy 0.969, macro-F1 0.73, balanced damage 0.27 against the tuned rules' 0.64; no video call or cloud game to Bulk. Case study page and team briefing final
- [x] Full project report as LaTeX, `reports/paper/aqose-report.tex`, compiled to PDF with `latexmk -pdf` (pdflatex is installed): the problem, the system, the classifier and why LightGBM, the decision rule, the method, final results with the charts, ablation, CPU, limits. Asked for by the owner early on Oct 15: a draft by that afternoon with interim numbers marked, then filled with the final numbers once the final runs end (Oct 16). Done: 13 pages, `reports/paper/aqose-report.pdf`
- [x] One-slide reference for the design team (artifact https://claude.ai/artifact/8CVcLAHVWMav3HLAtYp11x; source in `reports/one-slide/`): final numbers in place of the interim ones, the "interim" labels removed, the QR code and the link to the report PDF in the header's report box. Done; the QR code goes to the public case-study page, which links nothing private
- [x] Real calls replayed live, added Oct 15 after a review of the 10-seed page: its strongest objection is that the live wins may only show the classifier recognising its own generators, which are in its training data. `video_call` takes `params: {trace: <name>}` and then replays a recorded call packet for packet (`aqos_ml.trace`, traces in `scenarios/traces/`, sizes and timing only). S12 replays WhatsApp video call 005937 (validation split: the frozen model never trained on it), S13 call 010544 (in v2 training), both on a laptop sharing files on a 50/4 line. 40 runs with B2/B4/O1/O2 on seeds 0-4, then 10 runs of O2 with lgbm-v1, which never saw WhatsApp. The system under test is unchanged: only the generator and the scoring of a replayed call's frame rate are new. Branch `replay`; `scripts/realcall-runs.sh` (detached, status in `REALCALL-STATUS`) waits for the final runs, merges, tests, runs, writes `reports/realcall.md` and `reports/realcall-v1.md`. About 3 hours after the final runs. Done Fri 11:46, 50 of 50 valid. Frozen model: both calls labelled video call within 0.13 s in all 20 runs, kept in Video. S12 late frames CAKE 57%, rules 43%, AQoSE 2.4%; S13 79%, 97%, 14% (the rest is the call's keyframe bursts above the Video tin's share of a 4 Mbit/s upload). v1 model: labelled voice call, still protected (6.6%, 31%), reclassifier churn
- [x] Push notification to the owner when all of the above is done. Done

Cut, to be stated as limits: adaptive call generator, real router, S9, fairness index, IPv6 and VPN scenarios, dashboard and demo.
