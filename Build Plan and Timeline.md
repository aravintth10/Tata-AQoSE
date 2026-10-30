# Adaptive QoS Engine: Build Plan

Companion to [the PRD](<Adaptive QoS Engine Product Requirements (Teliport S4).md>). Module numbers (M1 to M14), scenarios (S1 to S10) and configurations (B0 to B4, O1, O2) refer to the tables there.

**Team.** Two people. The PRD still lists three members, so its owner columns are out of date; this plan replaces them.

- **Systems**: everything except ML. Testbed, Rust controller, Linux networking, enforcement, safety, dashboard, demo.
- **ML**: data, classifier, decision rule, reclassification logic, QoE verifier, experiments, statistics, report.

## How to read this plan

- The work is split into **chunks**, grouped into **waves**.
- Chunks in the same wave do not depend on each other and can be done in parallel.
- Every wave comes after the waves above it. A chunk depends only on chunks in earlier waves.
- A chunk can start as soon as the chunks in its "Depends on" column are done, even if the rest of the previous wave is still open. Nobody has to wait for a whole wave to close.
- Sizes are rough working days for one person. They are estimates to correct as the work goes, not commitments.
- Each wave has a summary table, then a section per chunk saying what the chunk is, what gets built in it and who needs the result. The full design of each chunk is in the [technical specification](<Technical Specification.md>).

## Terms used in the chunks

| Term | Meaning |
| --- | --- |
| Flow | One connection between two programs, for example one video call stream or one download |
| CAKE | The Linux queue manager the whole project sits on. It limits traffic to a set rate and shares the link fairly between hosts and flows |
| Tin | One of CAKE's four priority queues: Bulk, Best Effort, Video, Voice. "Labelling a flow" means choosing its tin |
| Shaper rate | The rate CAKE limits traffic to. It must sit just below the real link rate, or the queue builds in the modem where CAKE cannot manage it |
| Conntrack mark | A number the kernel stores for each connection. We store the flow's label in it, and CAKE reads it for every packet |
| Namespace | An isolated network stack inside Linux. The testbed uses namespaces as pretend hosts, router, ISP and server on one laptop |
| Feature record | The numbers that describe one flow: sizes and gaps of its first 8 packets, plus per-second statistics. No payload and no addresses |
| QoE | Quality as the application experiences it: latency, jitter, loss, missed frames, video stalls |
| Seed | The number that fixes the random choices of a run. The same seed gives the same traffic, so two configurations can be compared on identical traffic |
| Controller | `aqosd`, the Rust program on the router that runs the pipeline |

**Configurations** are the router setups being compared.

| ID | Router setup | In the core runs |
| --- | --- | --- |
| B0 | No QoS at all | Yes |
| B1 | Static priority by port | Stretch |
| B2 | Plain CAKE at a fixed rate | Yes |
| B3 | CAKE with cake-autorate | Stretch |
| B4 | CAKE plus hand-written threshold rules | Yes |
| O1 | Ours, choosing the tin of the most likely class | Yes |
| O2 | Ours, choosing the tin with the lowest expected damage (the full system) | Yes |

**Scenarios** S1 to S10 are the test situations, for example S1 is "a large download starts during a video call". They are listed under chunk 6C.

## Wave 1: Foundations

| Chunk | Owner | Depends on | Done when | Size |
| --- | --- | --- | --- | --- |
| 1A. Versions and repo | Both | Nothing | Kernel, iproute2, Go, Python libraries and Grafana pinned; repo builds on both laptops | 0.5 each |
| 1B. Interface contracts | Both | Nothing | Scenario YAML schema, feature record, policy proposal and metrics format written down and agreed | 1.5 each |

### 1A. Versions and repo

**In short:** make both laptops run the same software, so a result on one reproduces on the other.

- Pin the kernel, iproute2, nftables, Go, Python and its libraries, Grafana and Prometheus, and record them in [VERSIONS.md](VERSIONS.md).
- `make env` checks a machine against those pins and fails on a missing tool, a version mismatch or a missing kernel module.
- `make check` runs Rust format/clippy/tests, Go reference tests and Python contract tests. Every later chunk adds its tests to this command.
- **Status:** built on the reference laptop. The second laptop has not been checked yet.

### 1B. Interface contracts

**In short:** agree on the exact shape of every piece of data the Go side and the Python side exchange, before either side writes code. This is what lets Systems and ML work in parallel from wave 2 onward.

There are seven contracts, each a JSON Schema with an example in [contracts/](contracts/):

| Contract | What it describes |
| --- | --- |
| Scenario | One experiment: the hosts, the link rate and impairments, which traffic runs when, and timed events |
| Feature record | The numbers describing one flow, which the classifier takes as input |
| Classification | The classifier's answer: a probability for each class |
| Policy proposal | A request to change something: "put this flow in this tin" or "set the shaper to this rate", with a reason |
| Policy verdict | The safety shield's answer to a proposal: accept, clamp or reject, with a reason |
| Metric sample | One measurement (latency, throughput, queue delay and so on), one line in `metrics.jsonl` |
| Run manifest | The record of one run: scenario, configuration, seed, and whether the run was valid |

- A shared `taxonomy.json` lists the traffic classes, their DSCP values and their tins.
- Tests on both sides read the same example files, so a change one side makes and the other does not follow fails `make check`.
- **Status:** built, waiting for ML to agree.

## Wave 2: Ground

| Chunk | Owner | Depends on | Done when | Size |
| --- | --- | --- | --- | --- |
| 2A. Testbed (M1) | Systems | 1B | ping4 and ping6 pass; bottleneck rate within 3% of config; teardown is idempotent | 3 |
| 2B. Traffic generators (M2) | ML | 1B | Call, voice, gaming, ABR video, bulk and update profiles; same seed gives the same schedule; QoE logs written | 4 |
| 2C. Real-app captures | Systems | 1A | Headers-only captures (`tcpdump -s 128`) of each activity, one at a time, labelled by session | 2 |

### 2A. Testbed (M1)

**In short:** a pretend home network on one laptop, built from a scenario file. Everything that sends traffic later runs inside it.

- The program `aqos-testbed` creates one namespace per home device, plus four fixed ones:
  - `router`: the device under test. CAKE and the controller run here.
  - `isp`: the bottleneck. It limits the rate in both directions with a deep queue, standing in for the modem.
  - `core`: adds delay, jitter and loss.
  - `server`: the far end that the home devices talk to.
- Commands: `up` builds the network and checks it with ping, `down` removes everything (and is safe to run twice, or after a crash), `event` changes something mid-run such as the link rate, `exec` runs a command inside a namespace.
- IPv4 goes through NAT on the router, IPv6 is routed, as in a real home.
- This chunk also settles whether the testbed can run without `sudo`.
- **Needed by:** every chunk that runs traffic, starting with 3A, 3B and 3C.
- **Status:** built. No `sudo` is needed. The bottleneck measures within 0.01% of the configured rate.

### 2B. Traffic generators (M2)

**In short:** programs that imitate real applications, so experiments are repeatable and the true quality of each flow is known.

- One profile per kind of traffic: voice call, video call, gaming, cloud gaming, adaptive video streaming, web browsing, bulk download, game update, backup upload, P2P and flow storm.
- Each profile is a client in a home-device namespace and a server in the `server` namespace.
- Every packet carries a sequence number and a send time, so latency, jitter and loss are measured exactly. This is the QoE written to `metrics.jsonl`.
- The send schedule is computed from the seed before the run starts, so the same seed always gives the same traffic.
- Ports are random per run. With fixed ports the model would learn port numbers instead of behaviour.
- Each generator also writes which flows it created to `flows.jsonl`. These are the true labels used for training in 4A.
- A generator that crashes makes the whole run invalid, never silently incomplete.
- **Risk:** Python may be too slow for cloud-gaming packet rates. If so, that one sender moves to Go.
- **Needed by:** 3A, 3C, 4A and every experiment.
- **Status:** built, all eleven profiles. The risk did not materialise: at 2,600 packets per second the sender is 113 µs late at the 99th percentile, so it stays in Python.

### 2C. Real-app captures

**In short:** record real applications on our own devices. Generators only imitate; these captures are the honest test of whether the model works on real traffic.

- One activity at a time (for example only a video call), captured with headers only, so the label for the whole capture is known.
- A small session file records the activity, app, device, network and times.
- An HD video call and a cloud gaming stream must be captured, because scenarios S6 and S7 depend on them.
- Background flows such as DNS and system updates are reviewed per session, since they would otherwise get the wrong label.
- Raw captures stay out of git.
- **Needed by:** 4A.
- **Status:** the recording and review tool is built. The captures themselves are not taken yet: they need real devices and `sudo`.

## Wave 3: Baseline and plumbing

| Chunk | Owner | Depends on | Done when | Size |
| --- | --- | --- | --- | --- |
| 3A. Baselines B0 and B2, metrics exporter | Systems | 2A, 2B | S1 runs on B0 and B2 and the metrics show CAKE beating the FIFO | 2 |
| 3B. Feature extractor (M3) | Systems | 1B, 2A | One code path produces the same feature record from a pcap and from live traffic; no payload reads; stops after a flow is labelled | 4 |
| 3C. Experiment runner v0 (M14) | ML | 2A, 2B | One command resets, runs a scenario, collects results and can resume after a crash | 3 |

### 3A. Baselines B0 and B2, metrics exporter

**In short:** set up the two reference points everything is compared against, and get measurements flowing to a dashboard. This is the first proof that testbed, generators and metrics work together.

- **B0:** the router does nothing. The deep queue in `isp` fills up and the call suffers.
- **B2:** plain CAKE on the router at 95% of the link rate, in both directions. This is the floor our system must never fall below.
- A baseline is a script that configures the router. Later configurations (B4, O1, O2) are B2 plus our marking rules and the controller.
- **CAKE reader:** once per second, reads CAKE's statistics (queue delay, backlog, drops, shaper rate) and writes them as metric samples.
- **Exporter:** the program `aqos-exporter` serves the run's `metrics.jsonl` to Prometheus, so the dashboard and the final report read the same data.
- A compose file starts Prometheus and Grafana at the pinned versions.
- **Needed by:** 4C, 4D and 5E.
- **Status:** built. On S1 the call's p95 latency is 507 ms on B0 and 10.4 ms on B2. The Prometheus and Grafana containers are configured but not yet pulled or started.

### 3B. Feature extractor (M3)

**In short:** turn packets into feature records. It is the classifier's only view of the traffic.

- A kernel rule hands the controller only the headers of the first 8 packets of each flow. After the eighth packet the extractor no longer sees the flow, so its cost depends on new flows per second, not on bandwidth.
- A flow table holds each flow's first-8 sizes and gaps, its current label and its counters. When the table is full, new flows are simply not labelled.
- Flow and host identifiers are keyed hashes, so no address ever leaves the controller.
- Once per second the kernel's per-connection counters are read, and nine "window" statistics are computed from the last five seconds. These feed reclassification in 7A.
- **Offline mode:** `aqos-extract` runs the same code over a capture file. Training data and live data are therefore computed by one code path and cannot drift apart.
- Payload is never read. A check in `make check` enforces it.
- **Needed by:** 4A, 4B and 6A.
- **Status:** built, offline and live. In the testbed the live and offline records were identical for all 120 flows.

### 3C. Experiment runner v0 (M14)

**In short:** one command that performs a whole experiment run, so a run is identical every time and hundreds can be left running unattended.

- One run, in order: tear down, bring the testbed up, apply the configuration, start servers and generators, apply timed events, stop, collect results, tear down, validate, write the manifest.
- Each run gets a directory under `results/` holding its manifest, metrics, true flow labels, the controller's action log and a copy of the files it used.
- **Validity checks:** every generator exited cleanly, every traffic entry produced measurements for the whole run, and the link rate matched the scenario. A failed check marks the run invalid and schedules one retry.
- **Resume:** the matrix command skips runs that already have a valid manifest, so it can be interrupted and restarted.
- It is "v0" because the full matrix settings arrive in 8A.
- **Needed by:** 5B, 6C and all of wave 8.
- **Status:** built for B0 and B2. Killing it part way and running again completes only what is missing.

## Wave 4: Data and enforcement

| Chunk | Owner | Depends on | Done when | Size |
| --- | --- | --- | --- | --- |
| 4A. Dataset v1 | ML | 2B, 2C, 3B | Generator and real-capture flows turned into features, split by capture session, with a held-out real test set | 2 |
| 4B. qosify-style rules (B4) | ML | 3B | Rules run behind the same interface as the classifier and are scored on the same features | 1 |
| 4C. Enforcer (M10) | Systems | 3A | nftables marks, ctinfo and tc CAKE changes applied transactionally; readback equals desired state; mismatch restores the snapshot | 3 |
| 4D. Watchdog (M12) | Systems | 3A | Missed heartbeats for 3 s apply the safe CAKE config; no dependencies on the controller | 1 |

### 4A. Dataset v1

**In short:** build the training data for the classifier.

- Two sources, both passed through `aqos-extract` from 3B:
  - Testbed runs with randomised settings (link rate, delay, loss, bitrates, packet sizes, ports). Labels come from `flows.jsonl`.
  - The real captures from 2C. The label is the session's activity.
- Stored as one compressed file of feature records, each with a label, a session and a source.
- **Split by session, never by flow.** Flows from one session look alike, so splitting by flow would let the model see its test data.
- Four parts: train, calibration, validation, and a final test set made only of real captures. The final test set is written once and not opened until the report.
- **Needed by:** 5A.
- **Status:** the pipeline is built and a first dataset exists with all seven classes. Real traffic now comes mainly from public datasets, reduced on Kaggle to small per-flow tables. The data is thin: gaming and low-priority traffic come only from the testbed so far (10 and 31 flows), and neither has real captures for the final test. More testbed runs and more public sets are on the way.

### 4B. qosify-style rules (B4)

**In short:** build the competitor. These are the hand-written threshold rules that existing tools use, and "does ML beat them" is the project's main question.

- The rules: a flow with a high packet rate is bulk, a flow with small packets is priority, everything else is default.
- They sit behind the same interface as the model and return the same classification record, so both are scored by identical code.
- The two thresholds are tuned on the same data the model trains on, so the comparison is fair. The tuned values are published.
- Running the controller with these rules is configuration B4.
- **Needed by:** 6A and 6B.
- **Status:** built, with the shared scoring code. Scored on the first dataset; the numbers wait for the full dataset and the measured damage table before they mean anything.

### 4C. Enforcer (M10)

**In short:** the only code allowed to change the network. Every decision the system makes becomes real here.

- Operations: label a flow (write its conntrack mark), set a shaper rate, install or remove the marking rules, take a snapshot, restore a snapshot.
- Every change is a transaction: snapshot, apply, read the state back, compare. If what was read back differs from what was asked for, the snapshot is restored and the change reports failure.
- **First test to write:** send marked traffic and confirm it lands in the expected CAKE tin. The marking design assumes this and it is not yet verified.
- **Needed by:** 5B, 5D, 6A and 7C.
- **Status:** built. The assumption holds: each label value puts a flow in its tin, upload and download, IPv4 and IPv6. A deliberately corrupted change is caught and undone.

### 4D. Watchdog (M12)

**In short:** a tiny separate program that returns the router to plain CAKE if the controller crashes or hangs. It guarantees a failure of our system is never worse than not having it.

- The controller touches a heartbeat file every 250 ms.
- The watchdog checks the file's age. If it is older than 2 s, it removes the marking rules and sets both CAKE instances to safe fixed rates.
- The timeout is 2 s so that the safe state is reached within 3 s in total, which is the PRD's limit.
- It is an independent Rust crate with no controller/classifier dependency; a controller crash cannot take the watchdog down.
- Tested by killing and by freezing the controller, 20 times each.
- **Needed by:** scenario S10 and the fault demo in 8C.
- **Status:** built. Safe state reached 2.06 to 2.14 s after the controller is killed or frozen, over 20 trials (10 of each).

## Wave 5: Model and safety

| Chunk | Owner | Depends on | Done when | Size |
| --- | --- | --- | --- | --- |
| 5A. Classifier (M4) | ML | 4A | RF and LightGBM trained and calibrated; class envelopes derived; inference runtime chosen after the `leaves` version check | 4 |
| 5B. Damage table | ML | 3C, 4C | Each class placed in each tin under load; latency, jitter, stalls and throughput loss converted to scores | 2 |
| 5C. Decision engine (M5) | ML | 1B | Top-label and expected-damage modes; damage table read from config (the PRD's illustrative table until 5B lands); any error returns Standard | 1 |
| 5D. Safety shield (M9) | Systems | 1B, 4C | Accept, clamp or reject on every proposal; every fuzzed invalid proposal rejected | 2 |
| 5E. Core Grafana panels (M13) | Systems | 3A | Latency, jitter, loss, throughput, queue depth and fairness live; alert panel when the exporter is down | 2 |

### 5A. Classifier (M4)

**In short:** the model that looks at a feature record and says what kind of traffic the flow probably is, as a probability for each of the seven classes.

- **Inputs:** 28 numbers: sizes of the first 8 packets, the 7 gaps between them, IP version, protocol, port range, and the 9 window statistics. Raw port numbers are not used.
- **Two models** are trained on the same data: a random forest and LightGBM. The one with lower expected damage on the validation data is kept.
- **Calibration:** adjust the probabilities so that "70% call" is right about 70% of the time. The decision rule in 5C is only as good as these probabilities.
- **Envelopes:** for each class, the normal range (5th to 95th percentile) of each window statistic. The reclassifier in 7A uses them.
- **Evaluation code:** one function that scores any classifier, including the rules from 4B.
- **Service:** `aqos_ml.serve`, a small Python program on the router that the controller sends requests to. If it is slow or down, flows stay unlabelled.
- **Runtime choice (updated after migration):** the Rust service is the default. Python remains available explicitly for rules baselines and research ablations; Go is a comparison reference.
- **Needed by:** 6A, 6B and 7A.
- **Status:** the code is built and tested: the feature function, training with calibration, the saved model, and the service with its request format in `contracts/`. It was tried on a draft dataset only. The real models are trained when dataset `v1` is sealed, with one command.

### 5B. Damage table

**In short:** measure how bad each possible mistake is, instead of guessing.

- The table has 28 cells: 7 classes by 4 tins. A cell answers "what happens when a flow of this class is put in this tin?"
- For each cell, a flow of that class is forced into that tin while a standard background load runs (a call, a stream and a download on other devices).
- Two things are measured: the harm to the flow itself, and the harm it does to the others. A download put in Voice hurts mostly the others, which is why both are needed.
- The measurements are turned into one score per cell, scaled so the worst is 10.
- 5 seeds per cell is 140 runs, about 2.7 hours of machine time.
- **Output:** `configs/damage_table.yaml`.
- **Needed by:** 6B. It replaces the placeholder table in 5C.
- **Status:** the measurement is built and partly run. Ten profiles cover the seven classes. Each cell is measured under three conditions: the flow alone on its device; the same device also fetching a game update over eight connections; and the same device on a video call over a narrow uplink. Alone, only Bulk hurts anything, because CAKE shares the link between devices and then between flows. With company, the tin decides: a cloud game got 0.6 of 16.7 Mbit/s in Best Effort and all of it in Video. This is the case where a classifier beats plain CAKE. The third condition is where demoting a background transfer saves a call. One seed per cell is in and adopted as `configs/damage_table.yaml`; the second is running.

### 5C. Decision engine (M5)

**In short:** turns the classifier's probabilities into one tin for the flow.

- **Expected-damage mode (O2):** for each tin, add up probability times damage over all classes, and pick the tin with the lowest total. When the model is unsure, this picks the safe middle instead of gambling.
- **Top-label mode (O1):** pick the tin of the most likely class. This is the simpler rule we compare against.
- Example with the PRD's table: at 45% call and 55% download, top label sends the flow to Bulk, which is a disaster if it is a call. Expected damage picks Best Effort.
- A flow goes to Bulk only when the model is very sure it is low priority (starting value 80%).
- An active lease from 7D raises the damage of demoting that host's interactive traffic.
- Every decision carries a plain-language reason.
- Any error gives Best Effort, which is plain CAKE behaviour.
- It is built against the PRD's illustrative table, so it does not wait for 5B.
- **Needed by:** 6A and 6B.
- **Status:** built, in `ml/aqos_ml/decide.py`. The PRD's worked example passes as a test, and 4,000 random classifications all gave proposals that satisfy the contract.

### 5D. Safety shield (M9)

**In short:** the gatekeeper. Every proposal, from any source, passes through it before reaching the enforcer.

- It returns one of three verdicts: accept, clamp (accept with a safer value) or reject.
- **Checks on every proposal:** valid format, known flow, the flow has not changed label too often, and the total number of label changes per second is within budget.
- **Checks on shaper rates:** within the configured minimum and maximum, increases in small steps, and no more than one increase per 10 s. Decreases are never delayed because lowering the rate is the safe direction.
- **Starvation detector:** if a host has a queue but almost no throughput for 2 s, promotions and leases are suspended.
- It has no side effects, so it can be fuzzed: a program generates huge numbers of random proposals and checks that none gets through outside the bounds.
- **Needed by:** 6A and 7D.
- **Status:** built, in `internal/shield`. 10.9 million fuzzed proposals, none outside the bounds. Six bugs planted on purpose were each found by the fuzzer in under a second. `make fuzz` repeats the run.

### 5E. Core Grafana panels (M13)

**In short:** the live dashboard for watching runs and for the demo.

- Panels: latency (p50, p95, p99), jitter and loss per traffic entry, throughput per host, queue delay and backlog per tin, shaper rate against true capacity, and fairness.
- **Action log:** every accepted, clamped or rejected change appears as a marker on the graphs with its reason. These are the plain-language "why" lines.
- An alert panel shows when the exporter is down, so a blank graph is never mistaken for a quiet network.
- Dashboards are files in the repo and load automatically.
- **Needed by:** 8C.
- **Status:** the dashboard is written and checked against the metric contract, but has not been seen live: the Grafana and Prometheus containers are not pulled yet (about 700 MB on a slow line that the dataset downloads were using).

## Wave 6: First end-to-end

| Chunk | Owner | Depends on | Done when | Size |
| --- | --- | --- | --- | --- |
| 6A. Controller integration | Systems | 3B, 4B, 4C, 5A, 5C, 5D | Extractor, classifier, decision engine, shield and enforcer run as one pipeline; B4, O1 and O2 selectable as live configurations; a flow in S1 is labelled, marked and lands in the right tin | 3 |
| 6B. Offline head-to-head (go/no-go) | ML | 4B, 5A, 5B, 5C | Classifier and rules scored on expected damage, calibration and per-class recall on the held-out real captures, including the S6 and S7 traffic | 1 |
| 6C. Scenario definitions | ML | 3C | S1 to S4, S6 to S8 and S10 written as scenario YAML, including fault injection for S10 | 3 |

### 6A. Controller integration

**In short:** connect the parts built so far into one running program. This is the first time a live flow is labelled from end to end.

The life of a flow inside the controller:

1. The first packet arrives. The flow is unlabelled and CAKE treats it as plain CAKE would.
2. After the eighth packet, the extractor (3B) produces a feature record.
3. The controller sends it to the service. The classifier (5A) returns probabilities and the decision engine (5C) returns a proposal.
4. The shield (5D) gives a verdict.
5. The enforcer (4C) writes the mark and reads it back.
6. When the connection ends, the flow is removed.

A failure at any step leaves the flow unlabelled, which is plain CAKE.

- B4, O1 and O2 are the same controller with one setting changed: rules, model with top label, or model with expected damage.
- **Flow-storm guards:** flows shorter than 8 packets are never classified, and classifications per second are capped. Anything over the cap stays unlabelled and is counted.
- **Needed by:** all of wave 7.
- **Status:** built (`aqosd`). In the testbed all three configurations labelled every flow of a short S1 and CAKE obeyed the labels. One addition: the controller asks about each live flow again every 5 s, because the rules need a packet rate that the first eight packets cannot give; a flow is moved only when two answers in a row agree.

### 6B. Offline head-to-head (go/no-go)

**In short:** the checkpoint that decides whether the project's main claim holds, before more is built on top of it.

- No live traffic. The rules, the random forest and LightGBM are scored by the shared evaluation code on data none of them trained on, using the measured damage table from 5B.
- **The question:** does the model have lower expected damage than the tuned rules, especially on the HD video call and the cloud gaming stream?
- **If not:** wave 7 does not start. The causes to check, in order: wrong labels in the real captures, features that work on generator traffic but not real traffic, too few real sessions.
- **Output:** a one-page table of scores and a written decision.

### 6C. Scenario definitions

**In short:** write the test situations as scenario files that the runner can execute.

| Scenario | Situation | What it shows |
| --- | --- | --- |
| S1 | A large download starts during a video call | The basic case |
| S2 | The link drops from 100 to 20 Mbit/s | Whether the shaper rate follows (needs 7C) |
| S3 | Three TVs streaming plus a gaming device | Fairness under mixed load |
| S4 | A cloud backup fills the upload during gaming | Upload direction |
| S6 | An HD video call with a high packet rate, on a laptop that is also sharing files | Rules mistake the call for bulk; plain CAKE cannot tell it from the laptop's other flows |
| S7 | A cloud gaming stream, on a PC that is also fetching a game update | The same, on a documented case |
| S8 | Camera on and off, game lobby to match, download pause and resume | Reclassification (needs 7A) |
| S10 | The controller is killed; probe targets go silent | The fail-safe (needs 4D) |

- S5 (a priority lease) is defined in 7D. S9 (flow storm) is a stretch item in 8D.
- This chunk also fixes the list of mid-run events: link rate change, impairment change, traffic parameter change, kill the controller, silence the probe targets.
- Each scenario must complete one run on B2.
- **Needed by:** the smoke run and wave 8.
- **Status:** S2 to S8 and S10 are written, and the runner can kill the controller, silence the probe targets and request a lease mid-run. All but S8 have run once on B2, B4, O1 and O2 (`reports/live-first.md`). S8's changes (camera off and on, lobby to match, download paused and resumed) are part of each generator's plan, computed by both ends before the run, so the same seed still gives the same traffic. S6 and S7 were changed after the damage measurements: the call and the game now share their device with many bulk connections. Alone on a device they are protected by plain CAKE and every method would tie.

## Wave 7: Full system

| Chunk | Owner | Depends on | Done when | Size |
| --- | --- | --- | --- | --- |
| 7A. Reclassifier (M6) | ML for logic, Systems for plumbing | 5A, 6A | Envelope check once per second, hysteresis, flip cap, safety recheck, re-mark on change; each reclassification logs its reason | 3 ML, 1 Systems |
| 7B. QoE verifier and rollback (M11) | ML | 6A | Improved, neutral or regressed verdict after each change; two bad windows trigger rollback; too few samples gives neutral | 3 |
| 7C. Capacity estimator (M7) | Systems | 4C | Tracks the emulated capacity; cuts fast, recovers slowly; holds the minimum rate if reflectors are lost | 3 |
| 7D. Intent service (M8) | Systems | 5D, 6A | Lease API with a minimal form; limits enforced; lease applied within 1 s and always expires; S5 defined | 2 |

### 7A. Reclassifier (M6)

**In short:** flows change behaviour after they are labelled (a camera turns on, a game leaves the lobby, a download pauses). This watches labelled flows cheaply and asks for a new classification only when one stops looking like its class.

- Once per second, each labelled flow's nine window statistics are compared with the envelope of its current class (from 5A).
- A second counts as "outside" when at least two statistics are out of range. Three outside seconds in a row trigger a reclassification, or two for flows in Voice and Video.
- A reclassification goes through the normal path: classify, decide, shield, enforce.
- **Flip cap:** a flow whose label changes more than twice in a minute is left unlabelled for good, so a flow cannot bounce between tins.
- **Safety recheck:** every 30 to 60 s each flow is reclassified anyway, to catch a wrong label that happens to sit inside the envelope.
- Every reclassification logs its reason, for example which statistic left which range for how long.
- **Status:** built. The check is defined on the ML side (`ml/aqos_ml/envelope.py`), which writes the test cases; the Go check in `internal/reclass` passes them. One addition: after a trigger a flow is left alone for 5 s, doubling up to 40 s while it stays outside its range. On validation data 38 to 48% of calls and games that did not change would be sent back at least once (`reports/envelopes-v1.md`), so without that a badly fitting flow would be asked about every two seconds.
- **Split of work:** ML supplies the envelopes, the thresholds and a file of test cases. Systems writes the check in Go inside the controller and must pass those test cases.

### 7B. QoE verifier and rollback (M11)

**In short:** after a risky change, check whether things got better or worse, and undo the change if they got worse.

- It watches three kinds of change: a shaper rate increase, a lease starting, and a flow promoted into Voice or Video.
- It compares the 10 s before the change with two 5 s windows after it.
- It uses only what a real router can see: CAKE's queue delay, drops and backlog, probe delay, and per-flow throughput. The generators' true QoE is used only afterwards, to score how often the verifier was right.
- Verdicts: improved, neutral or regressed. Worse in both windows means regressed. Too little data means neutral.
- On "regressed", a rollback proposal restores the previous value. It passes through the shield like any other proposal.
- "Worse" means worse by more than normal run-to-run noise, which is measured with the A/A runs in 8A.
- **Status:** built, in Go inside the controller (`internal/verify`, `internal/controller/verifier.go`); every verdict is logged to the run's `verifier.jsonl`. It watches the delay in the Voice and Video tins and the probe delay. The noise band is a starting value (5 ms and half as much again) until the A/A runs exist. A promotion into an empty tin has no "before" and is never judged.
- The technical specification gives the timing part to Systems; the logic is ML's.

### 7C. Capacity estimator (M7)

**In short:** a real link's speed changes. This keeps CAKE's shaper rate just under the true rate, so the queue stays where CAKE can manage it.

- The controller sends a small probe to three targets in the `server` namespace every 100 ms and tracks the delay.
- Delay rising on most targets for three probes in a row means the link is saturated: cut the rate to 90% of what actually got through.
- Delay low while the link is busy: raise the rate by one small step.
- Fewer than two targets answering: go to the configured minimum rate and stay there.
- Cuts are fast and increases are slow. All changes go through the shield.
- The testbed knows the true rate, so the estimator's error is measured exactly.
- It depends only on 4C, so Systems can start it early.
- **Needed by:** S2. If this chunk is cut, the shaper stays at a fixed rate.
- **Status:** built. On S2 in the testbed the drop is detected in 360 ms, the shaper is under the link 1.1 s later and the call's latency is back to 16 ms within 5 s. Four faults that the simulated link did not show were found on the real one and fixed. Whether a low fixed rate is the right fallback when the probe targets go silent is an open question after S10.

### 7D. Intent service (M8)

**Status:** built: leases for a device, the limits, expiry across restarts, the API and a one-page form, and scenario S5, in which a lease was requested and granted during a live run. A lease takes effect at a flow's next window (up to 5 s), not within 1 s.

**In short:** lets a user say "prioritise this device for the next 45 minutes". The result is a lease: a temporary, limited boost that always expires.

- A small HTTP API on the router: create a lease, list leases, end a lease.
- **Limits:** at most two leases at once, at most two hours each. A request over a limit is rejected with a reason.
- A lease does not force anything. It raises the damage of demoting that device's interactive traffic in the decision engine (5C).
- CAKE itself caps how much the Voice and Video tins can take, and keeps every other host's fair share, so no extra limiting is built.
- The expiry time is held by the controller and written to disk, so it survives a restart and does not depend on the web page staying open.
- One static web page: a device list, a duration and a button.
- Scenario S5 is defined here.

**Gate before wave 8: smoke run.** Every core scenario runs once on the five core configurations (B0, B2, B4, O1, O2) without an invalid run. This catches a broken scenario or configuration in minutes, before it wastes hours of the full matrix.

## Wave 8: Evidence

| Chunk | Owner | Depends on | Done when | Size |
| --- | --- | --- | --- | --- |
| 8A. Full matrix and A/A runs | ML | All of wave 7 | 10 seeded runs per scenario and configuration; invalid runs retried and logged | 3 |
| 8B. Ablations | ML | All of wave 7 | The four ablations in the PRD run and collected | 2 |
| 8C. Demo | Systems | All of wave 7 | One-command reset, fault demo (S10) and a recorded video | 2 |
| 8D. Stretch items | Systems | All of wave 7 | Any of: B1 and B3 baselines; S9 flow storm with the gigabit CPU curves; local model for intent text; public dataset cross-check | Not sized |

8A and 8B share one machine, so they run one after the other in machine time even though both are open at once.

### 8A. Full matrix and A/A runs

**In short:** produce the numbers the report is built on.

- Every scenario on every core configuration, 10 seeds each. The same seed gives the same traffic on every configuration, so each comparison is like against like.
- **A/A runs:** two sets of identical B2 runs that differ only in seed. Any difference between them is noise. A real improvement has to be larger than this, and "no worse than CAKE" is judged against it.
- Before starting, the model, the damage table and all configuration files are frozen and committed, and each run records that commit.
- Invalid runs are retried and logged, never dropped.
- **Status:** not run. The matrix files are written: `configs/matrix-smoke.yaml` (the gate, one seed), `configs/matrix-core.yaml` (450 runs, whole seeds first) and `configs/matrix-aa.yaml` (plain CAKE again on the same seeds, into its own directory).
- This is mostly machine time (25 to 28 hours). The person's work is starting it, watching for failures and checking the results are complete.

### 8B. Ablations

**In short:** experiments that switch one part off or weaken it, to show that each part earns its place.

| Ablation | Question it answers |
| --- | --- |
| Random forest, LightGBM, rules | Which classifier is best on the same data? |
| Expected damage against top label, with the classifier deliberately made worse | Does the decision rule protect calls when the model is unsure? |
| Hysteresis on and off | Does waiting for several outside seconds stop labels flapping? |
| Classify once against classify every second | How much CPU does the envelope check save, and at what cost in accuracy? |

### 8C. Demo

**In short:** a demonstration that works on the day.

- One command resets everything, starts the dashboard and runs S1 on plain CAKE, then on the full system.
- A fault demo: the controller is killed during S1 and the dashboard shows the watchdog recovering.
- A recorded video of both, in case the live run fails.

### 8D. Stretch items

**In short:** only if time remains. None of them is needed for the main result.

- **B1 and B3:** two more baselines and 200 more runs.
- **S9 and CPU curves:** a flood of short flows to find where the classifier saturates, and CPU cost per component at 100 Mbit/s, 500 Mbit/s and 1 Gbit/s.
- **Intent from text:** a small local model that turns a sentence into a lease.
- **Public dataset:** an outside cross-check of the classifier, only once the licence is confirmed.

## Wave 9: Delivery

| Chunk | Owner | Depends on | Done when | Size |
| --- | --- | --- | --- | --- |
| 9A. Statistics, charts and report | ML | 8A, 8B | Medians with bootstrap intervals, paired Wilcoxon tests, charts and the written report, ties reported honestly | 3 |
| 9B. Slides and rehearsal | Both | 8C; results from 8A | Deck with a limitations slide; one full rehearsal against the recorded video | 2 Systems, 1 ML |

### 9A. Statistics, charts and report

**In short:** turn the raw results into claims that can be defended.

- The warm-up at the start of each run is discarded.
- Each run is reduced to one number per metric, for example the call's p95 latency.
- For each scenario and configuration: the median over the 10 seeds, with a 95% interval.
- Configurations are compared seed against seed with the Wilcoxon signed-rank test. With only 10 pairs, effect sizes and intervals are reported first and p-values second.
- The two key comparisons are named in advance: O2 against B4 (does ML help) and O2 against O1 (does the decision rule help).
- Scenarios where every method performs the same are reported as ties, not hidden.
- One command regenerates every chart and table from `results/`.
- **Status:** the statistics are built: `python -m aqos_ml.report --results <dir> --aa <dir>` writes the tables. Charts and the written report are not started.

### 9B. Slides and rehearsal

**In short:** the presentation.

- A deck with the pitch, the head-to-head result, the expected-damage ablation, the fault demo and a limitations slide.
- One full rehearsal using the recorded video.

## Totals

| | Systems | ML |
| --- | --- | --- |
| Rough working days, stretch excluded | 34 | 38 |

Run strictly wave by wave, the plan takes about 43 working days. Because chunks can start as soon as their own dependencies are done, the realistic figure is closer to the larger personal total, about 38 working days. That is roughly eight weeks at five days a week, with no buffer. If the Teliport dates leave less than that, use the cut order below.

## Checkpoints and risks

- **Go/no-go is chunk 6B.** The classifier against the qosify-style rules is the project's contribution. If 6B shows no gain, fix features or data before starting wave 7, not after the matrix.
- **The damage table (5B) arrives late for its consumers.** The decision engine (5C) is built against the PRD's illustrative table so that it and the integration (6A) are not blocked. The measured table replaces it as config before 6B.
- **Shared feature code.** The extractor in 3B must serve both training and live classification. Otherwise dataset v1 (4A) has to be regenerated.
- **Matrix run time.** 10 scenarios × 5 core configurations × 10 runs is 500 runs, roughly 25 to 28 hours run serially on one machine, before A/A runs and ablations. With all seven configurations it is 700 runs and 35 to 39 hours. The five-configuration core is a deviation from the PRD, which runs every scenario against all seven.
- **Wave 5 is the heaviest for ML** (7 days against 4 for Systems). Systems can start 7C early, since it depends only on 4C.

## Cut order if time runs short

The stretch items in 8D go first. After that, cut from the top:

1. Intent service (7D) and S5
2. Explainer "why" lines
3. Capacity estimator (7C) and S2, falling back to a fixed shaper rate
4. S3 and S4

Never cut: S1, S6, S7 and S8 on B2, B4, O1 and O2, plus the watchdog and S10. That set alone demonstrates the CAKE floor, ML against rules, the expected-damage rule against top label, and the fail-safe.

## Open items that affect this plan

- [ ] Confirm Round 1 and Round 2 dates and compare them against the totals above
- [ ] Update the team and owner columns in the PRD to two people
- [ ] Choose the inference runtime (part of 5A)
- [ ] Confirm public dataset licences before use (only if that stretch item is picked up)


### Rust runtime migration validation

The complete runtime and classifier default to Rust. `AQOS_RUNTIME` and
`AQOS_SERVICE` remain explicit overrides for reference experiments. This migration retains Python research tooling and the Go
testbed. Runtime crates forbid unsafe Rust; the watchdog is a separate crate with
no controller/model dependencies. Build and validation commands are in README.

- [x] Port extraction, shield/enforcement, controller, capacity probing,
  reclassification, verifier/rollback, intent API and supporting runtime commands.
- [x] Preserve feature/proposal contracts; compare Go/Rust extraction on captures
  and public flow tables, including IPv6 and pcapng timestamp formats.
- [x] Validate real-kernel marking, restoration, partial cleanup and independent
  watchdog recovery, including a failed recovery command and absent first heartbeat.
- [x] Run B4/O1/O2 controller scenarios with the default Rust runtime, all 22
  existing integration checks, and a combined capacity-drop/intent/verifier test.
- [x] Validate staged installation and 5.95 million seeded Rust stress cases;
  preserve the Go comparison fuzzer as `make fuzz-go-reference`.
- [x] Measure seven matched extractor trials and three live controller CPU pairs;
  record raw results and binary hashes (1.34× extraction speed, 43.2% less controller CPU).
- [x] Add a continuous Rust stability harness with process resource samples,
  disk reserve, one attempt, and failure evidence. Validate short runs, flow churn,
  exclusive testbed ownership and SIGTERM cleanup.
- [x] Complete a six-hour Rust stability run: S903/O2, seed 23; 53,251 label
  applications, 2,039 rate changes, no enforcement errors, capture overflows,
  service timeouts or process replacements. See `reports/rust-six-hour-validation.json`.
- [ ] Complete the full Rust research scenario matrix.
- [ ] Validate memory, latency and throughput on target OpenWrt hardware.
- [x] Make Rust the default runtime, classifier, canonical CLI commands and dataset
  extractor; isolate Go references under `bin/go/` and include Rust in `make check`.
- [x] Add `install-runtime` staging without the Go harness or Python research tools.

The previously scheduled 4am VPN-test reminder remains cancelled. The first
Rust stability attempt stopped after about 31 minutes at the disk-space guard;
its result remains in `results-rust-soak-6h/soak.json`. After cleanup, the release
build reproduced the existing binaries and 35 Rust tests plus eight harness
checks passed. A fresh six-hour run uses `aqos-rust-soak-retry.service`, with its
result in `results-rust-soak-6h-retry/soak.json`. This retry completed all six
measured hours and passed; its final summary is `reports/rust-six-hour-validation.json`.
