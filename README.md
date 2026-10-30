<p align="center">
  <img src="docs/images/aqose-flow-hero.png" alt="Mixed traffic goes into AQoSE, each connection gets its own CAKE lane, calls stay smooth" width="100%">
</p>

<h1 align="center">AQoSE · Adaptive QoS Engine</h1>

<p align="center">
  <b>Your router can't tell a video call from a download. AQoSE can.</b><br>
  A traffic classifier that tells Linux CAKE which connection is the call or the game,<br>
  from the sizes and timing of its first eight packets — headers only, never content.
</p>

<p align="center">
  <a href="reports/paper/aqose-report.pdf"><img alt="Full report" src="https://img.shields.io/badge/report-PDF-1C3F86?style=for-the-badge"></a>
  <img alt="Rust 1.99" src="https://img.shields.io/badge/Rust-1.99-CE422B?style=for-the-badge&logo=rust&logoColor=white">
  <img alt="Python 3.14" src="https://img.shields.io/badge/Python-3.14-3776AB?style=for-the-badge&logo=python&logoColor=white">
  <img alt="LightGBM" src="https://img.shields.io/badge/LightGBM-4.7-6A4FD0?style=for-the-badge">
  <img alt="Linux CAKE" src="https://img.shields.io/badge/Linux-CAKE-1D8A5A?style=for-the-badge&logo=linux&logoColor=white">
</p>

---

<table>
<tr>
<td width="25%" align="center"><h2>94% → 0.6%</h2>late frames on an HD call while its laptop shares files</td>
<td width="25%" align="center"><h2>0 / 140</h2>paired runs worse than plain CAKE<br>(70 better, 70 tied)</td>
<td width="25%" align="center"><h2>~6 µs</h2>per classification, 0.13 s to label a new connection</td>
<td width="25%" align="center"><h2>~2 s</h2>back to plain CAKE if AQoSE stops, in 20 of 20 kills</td>
</tr>
</table>

<p align="center"><i>All results were measured in emulation, in a rootless network-namespace testbed on one laptop. AQoSE has not yet run on a real router or in a real home.</i></p>

---

## The problem

CAKE is the best open-source queue manager available on home routers. It shapes traffic just under the line rate, shares the link fairly between devices, and then fairly between each device's connections. For most homes, most of the time, that is enough.

It breaks in one everyday situation: **a call or a game on a device that is also downloading, uploading or sharing files.** The call becomes one connection among many on that device, gets a sliver of its share, and nothing tells CAKE which one is the call.

- CAKE has four priority lanes (tins: Voice, Video, Best Effort, Bulk), but only acts on them when packets carry a priority mark. Apps rarely set one, and ISPs often strip it.
- Existing tools use fixed thresholds — "many packets per second means bulk". A video call sends 400–600 packets/s and a cloud game over 1,000, so both get labelled bulk. qosify's defaults sent **78%** of cloud-gaming flows to the slowest lane.

| Scenario with plain CAKE | Late frames |
| --- | --- |
| HD video call on a laptop that is also sharing files | **94.3%** |
| Cloud game on a PC fetching a game update | **83.2%** |

---

## What AQoSE does

<p align="center"><img src="docs/images/flow.png" alt="Video call, cloud game and backup enter AQoSE; CAKE puts the game in Voice, the call in Video and the backup in Bulk" width="85%"></p>

1. **Listens** to the first 8 packet headers of every new connection via one nftables rule — no payload, no decryption.
2. **Predicts** the traffic class with probabilities across 7 categories: voice call, game/cloud game, video call, streaming, web, large download, backup/P2P.
3. **Decides** the CAKE tin with the lowest *expected damage*, inside hard safety guards.
4. **Writes one label, once** into the kernel's connection tracker. CAKE applies it to every packet in both directions; AQoSE never touches the packet path.
5. **Watches** every second with a cheap model-free check, re-asks the classifier only when a flow changes, and rolls back any change that adds delay.

Remove two nftables rules and the system is off. A mark of zero means plain CAKE, by construction.

---

## Results

<p align="center"><img src="docs/images/results-late-frames.png" alt="Late frames for S6, S5 and S7 under plain CAKE, threshold rules and AQoSE" width="70%"></p>

Median late video or game frames over 10 seeds, frozen system:

| Scenario | Plain CAKE | cake-autorate | Threshold rules | **AQoSE** |
| --- | ---: | ---: | ---: | ---: |
| HD call on a laptop sharing files (S6) | 94.3% | | 94.4% | **0.6%** |
| Work call on a busy evening, backup running (S5) | 93.3% | 93.3% | 78.7% | **0.1%** |
| Cloud game on a PC fetching an update (S7) | 83.2% | | 19.4% | **1.0%** |
| Line drops to a fifth of its speed (S2) | 55.5% | 4.6% | 55.6% | **0.9%** |
| Real video call, replayed packet for packet (S12) | 57.0% | | 42.7% | **2.4%** |

- **Against plain CAKE:** never worse in 140 seed-paired runs (70 better, 70 tied).
- **Against threshold rules:** never worse in any run. **Against cake-autorate:** better in all 20 tested runs.
- **Nothing starves.** Bulk transfers kept a median 98% of their plain-CAKE throughput; fairness between devices unchanged (±0.01).
- **Sealed test set**, opened once after the freeze: accuracy 96.9% on 1,213 flows from 183 unseen sessions. No call or cloud game was sent to Bulk.

---

## How it decides

<p align="center"><img src="docs/images/how-the-model-works-minimal.png" alt="28 flow features go into 259 decision trees, which output calibrated probabilities for 7 classes" width="85%"></p>

**The classifier** is LightGBM: 37 boosting rounds for each of 7 classes, 259 trees, calibrated with temperature scaling. It reads 28 numbers per flow: 8 packet sizes signed by direction, 7 inter-packet gaps, IP version, protocol, port class of each end (never raw port numbers), and 9 window statistics. The trees are exported as data and run in Rust; inference median is ~6.4 µs. Trained on 246,274 records from seven sources, split by capture session.

<table>
<tr>
<td width="50%" valign="top">

**The decision engine** doesn't just take the top label. A wrong lane doesn't always hurt the same: a call in Bulk is a disaster, a download in Voice mostly harmless. Each class was forced into each lane across 240 testbed runs and the harm was measured — that is the *damage table*. For each tin, AQoSE weighs the probabilities by the damage and picks the lowest:

$$E[t] = \sum_{c} P(c)\, D[c][t]$$

Hard guards sit on top: Voice only if call + game probability > 50%; Bulk only if > 80% sure, never on first packets alone and never for UDP; ties never go to Bulk.

</td>
<td width="50%"><img src="docs/images/expected-damage-full.png" alt="Probability input, damage matrix, expected-cost calculator and tin selector"></td>
</tr>
</table>

<p align="center"><img src="docs/images/cake-queue.png" alt="CAKE's four tins: Voice, Video, Best Effort and Bulk, with their shares" width="70%"></p>

CAKE does the queuing. AQoSE only picks the tin: Voice has priority up to 25% of the line, Video up to 50%, Best Effort up to 100%, and Bulk always keeps at least a sixteenth so nothing is starved.

---

## Built to fail safe

<table>
<tr>
<td width="38%"><img src="docs/images/safety-flow.png" alt="Five safety checks: class odds, expected damage and guards, safety shield, transaction, verify after promotion, plus the watchdog"></td>
<td width="62%" valign="top">

Every change passes the same checks:

- **Safety shield**: a pure function. Rejects invalid or stale proposals, a third lane change for one flow within 60 s, and more than 50 re-marks per second; clamps rate changes to safe steps. Passed 10.9 million fuzzed proposals; caught six planted bugs. The Rust shield has its own property, contract and generative stress tests.
- **Transactions**: snapshot, apply, read back, compare. A mismatch restores the snapshot.
- **QoE verifier**: after a promotion, if Voice/Video delay rose by more than 5 ms *and* more than half, the change is undone.
- **Watchdog**: a separate process. If the controller's heartbeat goes quiet for 2 s, it removes the marking rules and restores plain CAKE. In 20 of 20 kills that took 2.03–2.06 s.
- **Every decision is logged** with a plain-English reason, e.g. *"Looks like a video call (62% confident); the Video tin has the lowest expected damage."*

</td>
</tr>
</table>

---

## How it was tested

<p align="center"><img src="docs/images/pipeline.png" alt="Experimental pipeline: emulated home, traffic generators, emulated network, runs, freeze and re-test, measure, compare" width="100%"></p>

- A **rootless testbed** on one laptop: a network namespace per device, a router, and an ISP bottleneck with a 500 ms buffer. No `sudo`.
- **Eleven traffic generators** with per-packet timestamps. A frame is late if it arrives more than 100 ms late for a call, or 50 ms for a game.
- **Nine home scenarios × five router setups × ten seeds**, paired by seed so every setup sees byte-identical traffic. Ninety A/A runs measured the noise floor.
- **System frozen** after development: code, model, damage table and settings locked. Re-ran on fresh seeds, a new scenario, replayed real video calls and full IPv6. All 492 post-freeze runs were valid.

---

## Known limits

- **Emulation only.** No real router, no Wi-Fi, no real homes yet.
- **Generated calls don't adapt** their bitrate under pressure the way real apps do.
- **The model is weaker on data sources it has never seen.** Some classes are thin in the test set.
- **Two known problems:** a cloud game above a quarter of the line outgrows the Voice tin, and the line estimator overreacts when its probes return after an outage.

**Next steps:** run on a real OpenWrt router, test live with real apps, then a field trial in volunteer homes. See [reports/paper/aqose-report.pdf](reports/paper/aqose-report.pdf) for the full list of limitations.

---

## Quick start

Requirements: Linux with CAKE, nftables and user namespaces; Rust 1.99, Go 1.26 for the experiment testbed, Python 3.14 for research tooling. Versions are pinned in [VERSIONS.md](VERSIONS.md).

```bash
make venv    # create .venv from requirements.lock
make env     # compare this machine against VERSIONS.md
make check   # Rust format/clippy/tests, Go reference checks, Python and parity tests
make build   # Rust runtime commands and the Go testbed into bin/

scripts/demo.sh   # the whole loop in ~8 minutes, no sudo: S6 with plain CAKE, then with AQoSE, side by side
```

Rust is the default for the complete router runtime and classifier. `make build` produces:
`bin/aqosd`, `bin/aqos-classify`, `bin/aqos-extract`, `bin/aqos-ctl`, `bin/aqos-cake-reader`, `bin/aqos-reflector`, `bin/aqos-watchdog`, `bin/aqos-exporter`.

```bash
make build-rust
make test-rust      # Rust checks and differential Go/Rust socket tests
make bench-rust     # paired core + service benchmarks, raw JSON in reports/

PYTHONPATH=ml .venv/bin/python -m aqos_ml.run one \
  --scenario S6 --config O2 --seed 7 --results results-rust
```

```bash
make build
make test-runtime           # format, clippy, Rust tests, Go/Rust feature parity
make testbed-check          # full integration suite using the default Rust commands
make fuzz FUZZTIME=150s     # seeded Rust generative stress

PYTHONPATH=ml .venv/bin/python -m aqos_ml.run one \
  --scenario S6 --config O2 --seed 7 --results results-rust-runtime

bin/aqos-exporter -file results-rust-runtime/current/metrics.jsonl -listen :9108
```

Go runtime sources are retained as comparison references:

```bash
make build-go-reference
PYTHONPATH=ml AQOS_RUNTIME=go AQOS_SERVICE=go .venv/bin/python -m aqos_ml.run one \
  --scenario S6 --config O2 --seed 7 --results results-go-reference
```

To stage the eight production commands for deployment:

```bash
make install-runtime DESTDIR=/tmp/aqos-package PREFIX=/usr
```

The continuous stability harness (6-hour soak):

```bash
make soak-runtime SOAK_RESULTS=results-rust-soak-1
make soak-runtime SOAK_SECONDS=90 SOAK_RESULTS=results-rust-soak-smoke-1  # short smoke check
```

---

<details>
<summary><b>Reference: testbed, experiments, dataset, training and repository layout</b></summary>

### Documents

- [Full report](reports/paper/aqose-report.pdf) ([LaTeX source](reports/paper/aqose-report.tex))
- [Project log](<Project Log.md>)
- [Product requirements](<Adaptive QoS Engine Product Requirements (Teliport S4).md>)
- [Build plan](<Build Plan and Timeline.md>)
- [Technical specification](<Technical Specification.md>)
- [Interface contracts](contracts/README.md)
- [Pinned versions](VERSIONS.md)

### Testbed

No `sudo` is needed.

```
bin/aqos-testbed up contracts/examples/scenario.S1.yaml
bin/aqos-testbed exec host-laptop -- ping -c 3 10.78.0.10
bin/aqos-testbed check-rate
bin/aqos-testbed event link_rate direction=down rate_kbit=20000
bin/aqos-testbed down

make testbed-check
```

### Running an experiment

```
PYTHONPATH=ml .venv/bin/python -m aqos_ml.run one --scenario S1 --config B2 --seed 7
PYTHONPATH=ml .venv/bin/python -m aqos_ml.run one --scenario S6 --config O2 --seed 7
PYTHONPATH=ml .venv/bin/python -m aqos_ml.run matrix configs/matrix-3a.yaml
PYTHONPATH=ml .venv/bin/python -m aqos_ml.run show results/S1-B2-s0007
```

Configurations: `B0` no QoS, `B2` plain CAKE, `B4` threshold rules, `O1` model by top label, `O2` model by expected damage.

### Driving the router by hand

```
bin/aqos-testbed exec router -- bin/aqos-ctl marking install
bin/aqos-testbed exec router -- bin/aqos-ctl flows
bin/aqos-testbed exec router -- bin/aqos-ctl label udp 10.77.0.10:41000 10.78.0.10:7000 voice
bin/aqos-testbed exec router -- bin/aqos-ctl rate down 50000
bin/aqos-testbed exec router -- bin/aqos-ctl tins
```

### Dataset

```
PYTHONPATH=ml .venv/bin/python -m aqos_ml.dataset scenarios
PYTHONPATH=ml .venv/bin/python -m aqos_ml.run matrix scenarios/train/matrix.yaml --results results-train
PYTHONPATH=ml .venv/bin/python -m aqos_ml.dataset build
```

### Model, damage table, comparison

```
PYTHONPATH=ml .venv/bin/python -m aqos_ml.train --dataset data/dataset/v1.jsonl.gz
PYTHONPATH=ml .venv/bin/python -m aqos_ml.compare --dataset data/dataset/v1.jsonl.gz
PYTHONPATH=ml .venv/bin/python -m aqos_ml.damage run --seeds 1-3
PYTHONPATH=ml .venv/bin/python -m aqos_ml.damage score
make fuzz
```

### Feature records from a capture

```
bin/aqos-extract -pcap capture.pcap -out features.jsonl -keys keys.jsonl
```

### Layout

| Path | Contents |
| --- | --- |
| `contracts/` | Schemas, examples and Go types shared by Systems and ML |
| `cmd/aqos-testbed`, `internal/testbed` | The emulated home network |
| `internal/extract`, `internal/netfilter`, `internal/capture`, `cmd/aqos-extract` | Feature extractor, offline and live |
| `internal/cake`, `internal/exporter`, `configs/baselines/`, `dashboards/` | Baselines and metrics |
| `ml/aqos_ml/run` | Experiment runner |
| `ml/aqos_ml/dataset` | Dataset builder |
| `ml/aqos_ml/rules.py`, `ml/aqos_ml/evaluate.py` | Threshold rules and scoring code |
| `internal/enforce`, `cmd/aqos-ctl` | Enforcer |
| `cmd/aqos-watchdog`, `internal/heartbeat` | Watchdog |
| `ml/aqos_ml/features.py`, `train.py`, `model.py`, `serve.py` | Classifier and ML service |
| `ml/aqos_ml/damage.py`, `configs/damage_*.yaml` | Damage table |
| `ml/aqos_ml/decide.py` | Decision engine |
| `internal/shield`, `configs/shield.yaml` | Safety shield |
| `cmd/aqosd`, `internal/controller` | The controller |
| `ml/aqos_ml/compare.py` | Models vs rules, go/no-go report |
| `ml/aqos_ml/gen` | Traffic generators |
| `ml/aqos_ml/capture.py` | Recording and reviewing real-app captures |
| `scenarios/` | Scenario files |
| `kaggle/` | Reducing public datasets to flow tables |
| `ml/tests/` | Python tests |
| `scripts/` | Environment check, data fetching, demo, frozen-system runs |
| `reports/` | Report, result summaries, charts, slides |
| `docs/images/` | Figures used in this README |

</details>
