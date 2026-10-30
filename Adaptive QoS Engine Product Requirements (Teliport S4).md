# Adaptive QoS Engine: Product Requirements (Teliport S4)

Oct 9, 2026 · @Shreyash

## Overview

We are building an intent-aware QoS controller that sits on top of Linux CAKE and adds one thing CAKE lacks: knowing what each flow is. A calibrated tree model labels flows from packet sizes and timing only, an expected-damage rule turns those probabilities into a CAKE tin, and a safety layer guarantees the system never does worse than plain CAKE.

**Problem.** A home link carries calls, games, streaming, downloads and backups at once. When a big download starts, queues build and interactive traffic suffers. Static priority fails, and vanilla CAKE cannot tell a call from a download: it relies on DSCP marks that are often missing, plus a crude "sparse flow" heuristic.

**Pitch.** Plain CAKE is our floor. Our classifier supplies the missing information about what each flow needs, and every decision is bounded, verified and reversible.

**Context.** Entry for Tata Elxsi Teliport Season 4, problem statement "Adaptive QoS Engine for Mixed Home Broadband Traffic". Team of three: Member A (Go, Linux networking), Member B (ML), Member C (presentation, dashboard, demo).

## Goals, non-goals and success criteria

The project succeeds if it beats the best deterministic classifier (qosify-style rules) on user experience, with honest statistics, while never falling below plain CAKE.

**Goals**

1. Classify flows into standard RFC 4594 classes without reading payloads, with calibrated confidence and user correction.
2. Turn class probabilities into tin choices with an expected-damage rule that is safer than top-label picking.
3. Track link capacity so the shaper adapts when the WAN rate drops.
4. Support temporary user intent ("prioritize my work call for 45 minutes") without starving anyone.
5. Verify every change against user experience and roll back automatically when it makes things worse.
6. Prove all of it with reproducible, scripted experiments against strong baselines.

**Non-goals**

- Replacing CAKE or writing a new qdisc.
- Deep packet inspection, SNI reading or any payload access.
- Cloud-hosted inference. Everything runs locally and offline.
- Deep RL trained online in the home.
- Real router firmware, certification or Wi-Fi airtime control.

**Success criteria** (targets to test, not results)

| Area | Criterion |
| --- | --- |
| Floor | No scenario where our system is worse than plain CAKE beyond run-to-run noise |
| Classifier | Beats qosify-style rules on expected damage on held-out real captures |
| Decision rule | Expected damage beats top label as the classifier is deliberately degraded |
| Interactive QoE | Lower call and gaming p95/p99 latency and jitter than plain CAKE in the download and multi-TV scenarios |
| Fairness | No host starved; Jain's index reported per scenario |
| Stability | Reclassifications per flow per minute stay low; no flapping with hysteresis on |
| Safety | Controller crash reaches the safe state within 3 s |
| Scale | Classifier CPU stays nearly flat from 100 Mbps to 1 Gbps; flow-storm limit measured |

## Design principles

Seven rules decide every design question in this project.

1. **CAKE is the floor.** CAKE does all scheduling, fairness, delay control and anti-starvation. Our system only supplies hints (tin labels and shaper rates). With no hints, or low confidence, behavior equals plain CAKE.
2. **Classify per flow, not per packet.** Each flow is labelled once from its first few packets. Later packets only carry the stored label. Cost scales with new flows per second, not with bandwidth.
3. **Reclassify on change only.** A flow is rechecked when it leaves its class envelope, not on a timer per packet.
4. **Decide by expected damage.** Mistakes are not equal. A call in Bulk costs far more than a download in Voice, so decisions use full probabilities and a damage table.
5. **Privacy by construction.** Only L3/L4 headers, packet sizes and timing are read. No payload, no SNI. All inference is local.
6. **Bounded, verified, reversible.** Every change passes a safety shield, is verified against QoE and rolls back on regression. A watchdog restores a known safe state if the controller dies.
7. **Degrade to CAKE, never below it.** Overload, low confidence, unstable flows and component failures all fall back to plain CAKE behavior.

## Prior art and lessons

ML classifiers for home QoS exist and deterministic classifiers feeding CAKE exist, but we found no reproducible head-to-head between them. That comparison is our contribution.

**Deterministic classifiers we must beat**

| Tool | How it classifies | Weakness we target |
| --- | --- | --- |
| [qosify](https://git.cdn.openwrt.org/project/qosify/tree/README) (OpenWrt) | eBPF classifier setting DSCP for CAKE; bulk by packets per second, priority by max average packet length | Two fixed thresholds; a user reported cloud gaming (GeForce Now) classified as bulk ([forum](https://forum.openwrt.org/t/dscp-classify-a-service-for-applying-dscp-to-connections/147430?page=3)) |
| [dscpclassify](https://forum.openwrt.org/t/dscp-classify-v2-0-service-for-applying-dscp-class-to-connections-22-03-25-12-x/147430) | Threaded-download and P2P detection by connection counts in 5 s windows | Only detects bulk; says nothing about calls vs games vs video |
| [QoSmate](https://github.com/brada4/qosmate) | nftables rules with CAKE, HFSC or HTB, threshold up/downgrades | Mostly manual rules |
| [Telcom](https://github.com/XPDevs/telcom) | Packet-length variance at XDP plus a PID shaper | Self-reported claims, unverified |

**ML classification for QoS**

| Work | What it showed | Lesson |
| --- | --- | --- |
| [DIFFUSE](https://researchportal.murdoch.edu.au/esploro/outputs/report/Real-time-traffic-classification-and-prioritisation/991005540901207891) (CAIA, 2012) | ML flow classification on an OpenWrt router; 5,000 flows, 24.5 Mbps upstream with prioritisation | Abandoned; one user found [CoDel worked well enough](https://forum.archive.openwrt.org/viewtopic.php?id=36276). Our cautionary tale |
| [FlowQoS](https://repository.gatech.edu/entities/publication/5bcce650-b37b-4f2e-9113-bb2107beef13) (2015) | Classification offloaded to an SDN controller; improved ABR video and VoIP | Classifies via DNS, not traffic statistics |
| [FPHTC](https://arxiv.org/pdf/2105.00074) (2021) | Flow classifier outside the router transferred into a cheap per-packet policy | Same split as our design; cite it |
| [DCTPQ](https://zenodo.org/records/15595145) (2025) | DT and RF identify cloud gaming at 97.6%, evaluated with QoE (VMAF) | Tree models plus QoE evaluation is credible |
| [Online RTC classification](https://iris.polito.it/retrieve/handle/11583/2874753/436790) (Polito) | Decision tree, 97% accuracy from 1 s of call traffic | 1 s windows are enough for calls |

**What the CAKE community says.** Unless a scheduler has more reliable information about which flows matter, fair queueing is the best strategy ([forum](https://forum.openwrt.org/t/nftables-and-qos-in-2021/112013?page=27)). QoSmate's author advises prioritising sparingly ([forum](https://forum.openwrt.org/t/qosmate-yet-another-quality-of-service-tool-for-openwrt/207614/34)). Our pitch must be "we supply that more reliable information", never "CAKE is bad".

## Traffic classes

We borrow the user-facing classes of [RFC 4594](https://www.rfc-editor.org/rfc/rfc4594.html), the IETF guideline for DiffServ service classes, instead of inventing our own. Each class has a standard DSCP, and CAKE diffserv4 already maps those DSCPs to tins ([mapping](https://forum.openwrt.org/t/qosmate-yet-another-quality-of-service-tool-for-openwrt/207614/34)).

| RFC 4594 class | DSCP | CAKE diffserv4 tin | Typical home traffic |
| --- | --- | --- | --- |
| Telephony | EF | Voice | Voice calls |
| Real-Time Interactive | CS4 | Voice | Online gaming, interactive video |
| Multimedia Conferencing | AF4x | Video | Video calls |
| Multimedia Streaming | AF3x | Video | YouTube, Netflix, IPTV |
| Standard | CS0 | Best Effort | Browsing, unknown traffic |
| High-Throughput Data | AF1x | Best Effort | Downloads, game updates |
| Low-Priority Data | CS1 | Bulk | Backups, P2P, background sync |

The chain is: model predicts a class, the decision rule picks a tin, nftables sets the matching DSCP or mark, and CAKE queues it. "Standard" is the default for unknown and low-confidence flows, which is exactly plain CAKE behavior.

## System architecture

The system is a pipeline of five stages inside the router, with three helpers feeding it and an independent watchdog. Nothing reaches CAKE without passing the safety shield.

&#91;embedded content: control loop · 5 pipeline stages, 3 helpers, 1 watchdog\]

The classifier and decision engine only propose; the shield clamps or rejects, the enforcer applies and reads back, and the verifier can trigger a rollback.

**Module specs**

| # | Module | Owner | Inputs | Outputs | On failure | Done when |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | Testbed | A | Scenario YAML | Namespaces, veth, IPv4 and IPv6, netem and tbf impairments | Idempotent teardown | ping4 and ping6 pass; bottleneck rate within 3% of config |
| 2 | Traffic generators | A, B | Profile plus seed | Call, voice, gaming, ABR video, bulk and update traffic; ground-truth QoE logs | Crashed run marked invalid, never dropped silently | Same seed gives the same schedule |
| 3 | Feature extractor | A | Packets on the LAN interface | Feature record after 8 packets; per-second stats from conntrack counters | Buffer overflow means window stats only | No payload reads (CI check); stops after a flow is labelled |
| 4 | Classifier | B | Feature record | Class probabilities and confidence | Service down: flow stays Standard | Calibrated; beats rules on held-out real captures |
| 5 | Decision engine | B | Probabilities, damage table, active intents | Tin per flow | Error means Standard | Lower expected damage than top label |
| 6 | Reclassifier | B, A | Per-second counters, class envelopes | Reclassify requests | Flip cap pins the flow to Standard | Low reclassifications per flow per minute |
| 7 | Capacity estimator | A | Delay probes, byte counters | Shaper rate up and down | Reflectors lost: hold the minimum rate | Tracks emulated capacity; detects a drop within about 2 s (target) |
| 8 | Intent service | A, C | API or UI request | Time-limited leases | Invalid request rejected with a reason | Lease applied within 1 s and always expires |
| 9 | Safety shield | A | Proposed policy, current state | Accept, clamp or reject | Internal error means reject | Every fuzzed invalid proposal rejected |
| 10 | Enforcer | A | Accepted policy | nftables marks, ctinfo, tc CAKE changes | Readback mismatch restores the snapshot | Readback equals desired state |
| 11 | QoE verifier | B | Metrics before and after a change | Improved, neutral or regressed | Too few samples gives neutral | Low false-rollback rate in A/A runs |
| 12 | Watchdog | A | Controller heartbeat | Safe CAKE config | Kept tiny, no dependencies | kill -9 of the controller reaches safe state within 3 s |
| 13 | Dashboard and explainer | C | Metrics, action log | Grafana panels, plain-language "why" lines | Exporter down shows an alert panel | Latency, jitter, loss, throughput, queue depth and fairness all live |
| 14 | Experiment runner and report | B, C | Experiment matrix | Results, charts, report | Invalid runs retried and logged | One command resets, runs, collects and reports |

## Classifier

We train a random forest and a LightGBM model on the same features and data, calibrate both, and keep whichever has lower expected damage on held-out real captures. If they tie, we keep the random forest for simplicity.

**Features** (headers and timing only)

- Signed sizes of the first 8 packets (sign = direction)
- Gaps between those packets
- Protocol, ports, IP version
- Per-second window: packets and bytes up and down, mean and spread of packet size, regularity of timing, burstiness. Counts come from conntrack accounting (`net.netfilter.nf_conntrack_acct=1`), not our own per-packet counters.

**Training data**

| Source | Labels | Role |
| --- | --- | --- |
| Testbed generators with randomised settings and netem impairments | Known by construction | Main training set |
| Real apps on our own devices, one activity at a time, headers only (`tcpdump -s 128`) | The activity being run | Realism; part held out as the honest test set |
| Public datasets (for example CESNET) | App labels mapped to RFC 4594 classes | Optional cross-check; licence confirmed first |

All captures are our own traffic on our own devices, with no payloads. Collection steps, seeds and impairment settings are published for reproducibility.

**Pipeline**

1. Group packets into flows by 5-tuple.
2. Compute features with the same code the live extractor uses, so training and deployment cannot drift apart.
3. Split by capture session, never by individual flow. The final test set is real captures the model has never seen.
4. Train RF (little tuning) and LightGBM (tuned on a similar budget), with class weights for rare classes.
5. Calibrate both on a separate slice (scikit-learn `CalibratedClassifierCV`). Random forests tend to be under-confident and boosted trees over-confident.
6. Derive class envelopes (5th to 95th percentile per feature per class) for the reclassifier.
7. Deploy behind a small local service, or run inside Go if a library such as `leaves` still supports the model version.
8. User corrections act immediately as overrides and feed a retraining buffer. A retrained model is promoted only if it does not score worse on the held-out set.

**Evaluation, in priority order**

1. Expected damage on held-out real captures
2. Calibration (reliability chart)
3. Per-class recall, especially calls and games mislabelled as bulk
4. Accuracy and macro-F1
5. Inference time and model size

The qosify-style rules are scored in the same table as a third column.

**Rejected alternatives.** A cloud decision model (Jev) and a slim local LLM with choice-scoring were considered. Both lose to trees for this job: numeric input, sub-millisecond latency needs and a per-flow call rate. The trees already output an enum with probabilities. A local slim model remains an option only for parsing natural-language intent.

## Decision rule: expected damage

For each flow we pick the tin with the lowest expected damage, not the tin of the most likely class. When the model is confident both rules agree; when it is unsure, expected damage picks the safe middle instead of gambling.

```latex
\text{tin}^* = \arg\min_{t} \sum_{c} P(c \mid \text{flow}) \, D(c, t)
```

P comes from the calibrated classifier. D is a damage table: how much harm a flow of true class c suffers, or causes, when placed in tin t.

**Example damage table** (illustrative; the real one is measured)

| True class, placed in | Voice | Best Effort | Bulk |
| --- | --- | --- | --- |
| Call | 0 | 2 | 10 |
| Download | 4 | 1 | 0 |

**Worked example.** At 45% call and 55% download, top label sends the flow to Bulk with expected damage 4.5. Best Effort scores 1.45 and Voice 2.2, so expected damage picks Best Effort, about three times safer. At 5% call and 95% download, Bulk scores 0.5 and wins, the same answer as top label.

**Why it is better**

- Never worse in expectation, and better exactly where the classifier struggles.
- More stable: a flow drifting from 49% to 51% passes through Best Effort instead of jumping from Bulk to Voice.
- Degrades gracefully when real traffic is harder than test traffic.

**Requirements**

- Probabilities must be calibrated, or the rule misleads.
- D is measured in the testbed: place each class in each tin under load and convert latency, jitter, stalls and throughput loss into scores.
- Active intent leases adjust D (for example, a protected call makes Bulk placement for that flow prohibitively costly).
- Thresholds and the table are config, published with the results.

## Reclassification

A flow is labelled once, then watched cheaply against its class envelope and reclassified only when it leaves that envelope. Cost scales with how often flows change behavior, not with packets.

**Why flows change:** a call turns its camera on, streaming video alternates bursts and idle, a game moves from lobby to match, a download pauses, or one HTTP/3 connection carries mixed content.

**Rules**

1. **Envelopes from data.** Each class's envelope is the 5th to 95th percentile of each feature in its training flows. No hand-picked thresholds.
2. **Check per window.** Counters update per packet (from conntrack); the envelope check runs once per second per flow.
3. **Hysteresis.** Reclassify only after 2 to 3 consecutive windows outside the envelope.
4. **Asymmetric attention.** Flows in Voice and Video are checked tightly and demoted fast. Flows in Bulk that go quiet are harmless, since CAKE already favours sparse flows.
5. **Flip cap.** A flow that changes class more than twice a minute is pinned to Standard (plain CAKE behavior).
6. **Safety recheck.** Long flows are reclassified every 30 to 60 s regardless, to catch mislabels inside a loose envelope.
7. **Re-mark on change.** CAKE does not move flows between tins by itself, so a new label means a new mark.

**Known cost.** Moving a flow between tins can briefly reorder its packets, which TCP may misread as loss. Rare moves keep this minor, which is why rules 3 and 5 matter.

**Explainability.** Every reclassification logs its reason, for example: "moved from Voice to Standard because its packet rate stayed outside the call range for 3 seconds."

## Capacity, enforcement, intent and safety

These four parts keep the shaper rate honest, apply decisions through standard Linux tools, let users express temporary priorities and guarantee a fail-safe state.

**Capacity estimation**

- Delay-based, in the style of [cake-autorate](https://forum.openwrt.org/t/cake-w-adaptive-bandwidth/191049): raise the shaper rate under load while delay stays low, cut it when delay rises.
- Cut fast, recover slowly. Hold a configured minimum rate if probes fail.
- Ground truth in the testbed is the emulated bottleneck rate, so estimator error is measured exactly.
- cake-autorate itself runs as a baseline. ML capacity prediction is out of scope: published accuracy relies on radio-layer metrics a home router cannot see ([example](https://hal-laas.archives-ouvertes.fr/hal-02315068v2)).

**Enforcement**

- CAKE diffserv4 on WAN egress (upload) and LAN egress or IFB (download), with per-host isolation and NAT awareness.
- Labels are applied as nftables marks and DSCP, stored in the conntrack mark.
- On ingress, tc ctinfo restores DSCP from the conntrack mark, solving ISPs that wipe DSCP ([ctinfo](https://git.zx2c4.com/linux-dev/log/include/net/tc_act/tc_ctinfo.h?showmsg=1)).
- CAKE also accepts tc filter overrides of its tin, following the [cake-custom-isolation](https://github.com/heistp/cake-custom-isolation) pattern.
- Every apply is transactional: snapshot, apply, read back, then commit or restore.

**Intent**

- A lease: target (device or class), goal, duration up to 2 hours, priority cap.
- Hard limits: at most 2 concurrent leases, a boosted target capped at a share of the shaped rate, every other host keeps a floor of its fair share.
- Leases always expire, even if the UI dies.
- Input via buttons and a form. Optional: a slim local model with choice-scoring parses sentences like "prioritize my work call for 45 minutes" into the lease schema, with low confidence falling back to the form.

**Safety**

- Shield bounds: shaper rate within a min and max, asymmetric rate steps, no class guaranteed zero bandwidth.
- Rate limits on structural changes (for example at most one per 10 s).
- Verify-then-commit: a 10 s canary after each structural change; two bad windows in a row trigger rollback.
- Starvation detector: a host with backlog but almost no throughput for 2 s raises an alarm and caps boosts.
- Watchdog: a separate tiny process applies a conservative CAKE config if the controller misses heartbeats for 3 s.
- Every decision, accepted or rejected, is logged with its reason.

## Scalability

Our added cost scales with new flows per second, not with bandwidth. At gigabit speeds the bottleneck is CAKE's own per-packet work, not our classifier.

| Part | Scales with | Cost at gigabit |
| --- | --- | --- |
| ML inference | New flows per second (once per flow) | Microseconds per flow |
| Storing and applying the label | Nothing extra; conntrack mark already looked up for NAT | One mark read per packet |
| Feature extraction | First 8 packets per flow, then stops | Small and fixed per flow |
| Window stats | Read from conntrack counters once per second per flow | Small |
| CAKE shaping | Every packet, mostly one core | The real limit |

A 1 GB download is about 700,000 packets but one flow, so one label. Faster links mostly mean more packets per flow, not more flows.

**The real limit is flow storms.** Many tiny flows (DNS, P2P, page loads, port scans) could flood the classifier.

1. Skip tiny flows: classify only after a few packets or bytes; short flows stay Standard.
2. Bounded queue: if classification requests back up, new flows go unlabelled, which is plain CAKE.
3. Measure the limit with a synthetic flow storm and report it.

**Gigabit caveats** (for the limitations slide)

- Shaping at gigabit needs a capable CPU; QoSmate recommends lighter qdiscs on weaker routers ([README](https://github.com/brada4/qosmate)).
- Hardware flow offload bypasses tc entirely.
- At high WAN speeds the bottleneck often moves to Wi-Fi, which WAN shaping does not control.

**Evidence we will produce:** CPU per component (CAKE, extractor, classifier) at 100 Mbps, 500 Mbps and 1 Gbps over veth, plus CPU and unlabelled-flow share against new flows per second.

## Baselines, experiments and metrics

Every scenario runs against seven configurations with identical seeds, traffic and impairments. The key comparisons are ours vs qosify-style rules (does ML help) and expected damage vs top label (does the decision rule help).

**Configurations**

| ID | Configuration |
| --- | --- |
| B0 | No QoS: bloated FIFO at the bottleneck |
| B1 | Static priority: HTB classes by port and DSCP, fq\_codel leaves |
| B2 | Plain CAKE diffserv4, fixed rate |
| B3 | CAKE plus cake-autorate |
| B4 | CAKE plus qosify-style rules (packet rate means bulk, small packets mean priority), tuned fairly |
| O1 | Ours, top-label decisions |
| O2 | Ours, expected-damage decisions (full system) |

**Scenarios**

| # | Scenario | Primary metrics |
| --- | --- | --- |
| S1 | Large download starts during a video call | Call latency p95/p99, jitter, frame misses; download throughput |
| S2 | WAN drops from 100 to 20 Mbps | Time to recover call latency, peak queue delay, estimator error |
| S3 | Three TVs streaming plus a gaming device | Gaming p99, video stalls and switches, fairness |
| S4 | Cloud backup saturates upload during gaming | Gaming p99, upload throughput |
| S5 | "Prioritize work call 45 min" during S3 load | Call QoE, other hosts' floor, lease expiry accuracy |
| S6 | HD video call with high packet rate | Whether rules push the call to bulk; call QoE |
| S7 | Cloud gaming stream | Same as S6 (the documented qosify false positive) |
| S8 | Camera on and off mid-call; game lobby to match; download pause and resume | Time to detect change, reclassifications per minute |
| S9 | Flow storm of short flows | Classifier CPU, share of unlabelled flows |
| S10 | Controller killed; reflector blackholed | Time to safe state, latency during the fault |

S6 to S8 are designed where thresholds should fail. We also report scenarios where all methods tie because fair queueing already handles them.

**Statistics.** 10 seeded runs per scenario and configuration, 180 s each with a 20 s warm-up discarded. Medians with 95% bootstrap intervals; paired Wilcoxon tests by seed with effect sizes. A/A runs estimate noise.

**Metric definitions**

- Latency, jitter and loss: from generator timestamps (shared clock across namespaces); p50, p95 and p99.
- Fairness: Jain's index on throughput relative to each host's max-min fair share.
- Adaptation time: from injection until the metric holds within target for 5 s.
- Stability: shaper changes per minute, reclassifications per flow per minute.
- Classifier: per-class precision and recall, confusion matrix, calibration, macro-F1.
- CPU: per component, at each bandwidth and flow rate.

**Ablations**

1. RF vs LightGBM vs rules (same features, same data).
2. Expected damage vs top label as the classifier is deliberately degraded (input noise, less training data). Plot call latency against classifier accuracy for both rules.
3. Hysteresis on vs off: flapping rate.
4. Classify-once vs per-window reclassification: CPU and accuracy.

## Risks, limitations, decisions and open questions

The biggest risk is DIFFUSE's fate: fair queueing makes the ML look unnecessary. We answer it by targeting the cases where thresholds fail and by reporting ties honestly.

**Risks**

| Risk | Mitigation |
| --- | --- |
| ML shows no gain over CAKE or rules | Scenarios S6 to S8 target known rule failures; report ties; the rules-only engine still ships |
| Model overfits generator artefacts | Randomised generators, real-capture test set, session-based split |
| Calls mislabelled as bulk | High confidence bar for demotion, expected-damage rule, verify-then-rollback, user correction |
| Training and live features differ | One shared feature code path for capture and deployment |
| Reordering when flows change tins | Hysteresis and flip cap keep moves rare |
| Laptop CPU noise skews latency | CPU pinning, A/A runs, one machine for the testbed |
| Live demo failure | One-command reset, recorded video, cached report |
| Dataset licence conflicts | Our own captures first; public data only after licence check |

**Known limitations**

- All results are from Linux namespaces on laptops; hardware is emulated and labelled as such.
- Gigabit shaping depends on router CPU; hardware offload bypasses tc.
- Wi-Fi airtime contention is not controlled by WAN shaping.
- Tested with a handful of hosts; behavior with hundreds of devices is unmeasured.
- Classifier trained mainly on generated and our own traffic; real apps drift. VPN tunnels collapse to per-host fairness.
- Flow metadata still reveals behavior even without payloads; identifiers are hashed and data stays local.
- No operator certification or vendor firmware integration.

**Decisions log**

| Decision | Reason |
| --- | --- |
| Build on CAKE, do not replace it | Proven fairness and anti-starvation; it becomes our floor |
| Classify per flow, reclassify on envelope breach | Cost scales with flows and behavior changes, not bandwidth |
| RFC 4594 classes | Standard taxonomy with ready DSCP and CAKE tin mapping |
| Train on classes, decide by expected damage | Classes are labelable and scoreable; expected damage is safer than top label |
| RF and LightGBM, pick on expected damage | Tree models fit numeric features, train fast and run in microseconds |
| No cloud model, no LLM for flow classification | Latency, offline operation, privacy and numeric input |
| Slim local model only for intent text | Text input, rare calls, enum output with confidence |
| qosify-style rules as a baseline | The strongest deterministic competitor in practice |

**Open questions**

- [ ] Confirm Round 1 and Round 2 dates and submission format on the official Teliport page
- [ ] Measure the damage table in the testbed
- [ ] Choose the inference runtime (Python service or a Go library such as `leaves`) after a version check
- [ ] Confirm licences for any public dataset before use
- [ ] Pin exact versions of kernel, iproute2, Go, Python libraries and Grafana on day 1
- [ ] Check Google Scholar for any published qosify or dscpclassify comparison before Round 2
