# Interface contracts

These are the formats that cross the boundary between Systems and ML. Both sides build against them, so a change here needs both people to agree.

The JSON Schemas in `schema/` are the source of truth. This page explains what the schemas cannot say. `make check` validates every file in `examples/` against its schema in Python, and checks Rust production types and the Go reference types in `contracts.go`. The Rust runtime rejects missing required nullable fields as well as unknown fields. Scenario files are read in Go with `contracts.LoadScenario`, which rejects unknown fields.

| Contract | Schema | From | To |
| --- | --- | --- | --- |
| Scenario | `scenario.schema.json` | Written by hand (YAML) | Testbed, generators, runner |
| Feature record | `feature_record.schema.json` | Feature extractor | Classifier |
| Classification | `classification.schema.json` | Classifier | Decision engine |
| Service request and response | `service_request.schema.json`, `service_response.schema.json` | Controller, ML service | ML service, controller |
| Policy proposal | `policy_proposal.schema.json` | Decision engine, reclassifier, capacity estimator, intent service, verifier, user override | Safety shield |
| Policy verdict | `policy_verdict.schema.json` | Safety shield | Enforcer, action log |
| Metric sample | `metric_sample.schema.json` | Generators, CAKE reader, controller | Runner, dashboard, report |
| Run manifest | `run_manifest.schema.json` | Runner | Report |

The build plan named four contracts. Classification and policy verdict were added because the pipeline cannot be wired without them, and the run manifest is part of the metrics format.

## Taxonomy

`taxonomy.json` is the one place that maps classes to DSCP and tins, and generator profiles to their true class. Code reads it; nobody hard-codes the mapping.

| Class | DSCP | Tin |
| --- | --- | --- |
| `telephony` | EF | `voice` |
| `realtime_interactive` | CS4 | `voice` |
| `multimedia_conferencing` | AF41 | `video` |
| `multimedia_streaming` | AF31 | `video` |
| `standard` | CS0 | `best_effort` |
| `high_throughput_data` | AF11 | `best_effort` |
| `low_priority_data` | CS1 | `bulk` |

The decision engine picks a tin, not a class. When the chosen tin is the tin of the most likely class, the proposal uses that class's DSCP. Otherwise it uses the tin's default DSCP from `taxonomy.json` (EF, AF31, CS0, CS1).

## Decisions these contracts lock in

**Time.** Scenario times and `t_s` in metric samples are seconds since the start of the run. Warm-up starts at 0, so the measured window is `warmup_s` to `warmup_s + duration_s`. `duration_s` is the measured time only: the PRD's "180 s with a 20 s warm-up" is a 200 s run. `ts_ns` in live records is `CLOCK_MONOTONIC`, which all namespaces on one machine share.

**Direction.** Positive packet sizes, and "up", mean LAN to WAN. Negative sizes, and "down", mean WAN to LAN. `src_port` is the port on the side that opened the flow.

**Packet size.** L3 length in bytes including the IP header, so IPv4 and IPv6 are measured the same way.

**Flow identity.** `flow_id` and `host_id` are salted hashes, 16 hex characters. No IP address appears in any contract. The controller keeps the table from `flow_id` back to the conntrack entry, so the classifier never sees addresses. `host_id` is carried for leases and fairness and must not be used as a model feature.

**When a feature record is emitted.** Once per flow with `kind: initial`, after the 8th packet. Flows that never reach 8 packets are never classified and stay Standard. A `kind: window` record is emitted only for a reclassification check, and repeats the stored first-8 fields so the classifier stays stateless.

**Window statistics.** Conntrack gives only packet and byte counters per direction, read once per second. The spread, regularity and burstiness features in the PRD are therefore defined across the last 1 to 5 one-second samples, not inside one second:

- `pkt_size_spread`: standard deviation of the per-second mean packet size
- `rate_cv`: coefficient of variation of the per-second packet count
- `burstiness`: peak per-second bytes divided by mean per-second bytes

The initial record has `window: null` when no full sample exists yet, so the model must handle missing window features.

**Probabilities.** All seven classes are always present and sum to 1 within 1e-6. `confidence` is the largest of them. The qosify-style rules use the same format, with `model_version` naming the rule set.

**Every proposal has a reason.** `reason` is a plain sentence, written to be shown to a user. The shield's verdict has its own.

**Verdicts.** On `accept`, `applied` repeats the proposed values. On `clamp` they differ. On `reject`, `applied` is null and the enforcer does nothing.

**Invalid runs.** A crashed run still writes a manifest with `valid: false` and a reason. A retry gets a new `attempt` number and its own manifest. Nothing is deleted.

## The ML service

The controller and native classifier/decision engine are separate Rust processes joined by a Unix socket in the router namespace. `aqos_ml.serve` remains a compatible Python reference for B4 rules and explicitly selected research experiments. Each side writes one JSON object per line.

| Request | Response when `ok` is true |
| --- | --- |
| `classify`: a feature record, the flow's current tin, the classes a lease protects on its host | The classification and a flow-label proposal ready for the shield |
| `health` | Model version, decision rule, and whether the service is ready |

- Every request carries an `id` and gets exactly one response with the same `id`. The service answers several requests at once when they arrive together, so responses can come back out of order.
- The service keeps no state about flows. `previous_tin` and `leased_classes` come with each request because the controller is the one that knows them.
- `ok: false` carries an `error` and nothing else. The controller then leaves the flow unlabelled, the same as when the service is slow or down.
- `verify` (chunk 7B) will be added as a third `op`.

## Metrics

A run writes `manifest.json` and `metrics.jsonl` (one metric sample per line). Labels that do not apply to a sample are left out.

| Metric | Unit | Source | Grain | Labels |
| --- | --- | --- | --- | --- |
| `latency_ms` | ms, one way | generator | Per packet | host, traffic_id, profile, true_class, direction |
| `jitter_ms` | ms, RFC 3550 | generator | Per second | host, traffic_id, profile, true_class, direction |
| `loss_ratio` | 0 to 1 | generator | Per second | host, traffic_id, profile, true_class, direction |
| `throughput_kbit` | kbit/s | generator | Per second | host, traffic_id, profile, true_class, direction |
| `frame_miss` | Frames past their deadline | generator | Per second | host, traffic_id, profile, true_class, direction |
| `video_stall_ms` | ms stalled | generator | Per second | host, traffic_id, profile, true_class |
| `video_bitrate_kbit` | kbit/s, selected rung | generator | Per second | host, traffic_id, profile, true_class |
| `page_load_ms` | ms from the first request of a page to its last byte | generator | Per page | host, traffic_id, profile, true_class |
| `queue_delay_ms` | ms, CAKE average delay | cake | Per second | tin, direction |
| `queue_backlog_bytes` | bytes | cake | Per second | tin, direction |
| `tin_drops` | Packets | cake | Per second | tin, direction |
| `shaper_rate_kbit` | kbit/s, read back from CAKE | cake | Per second | direction |
| `capacity_true_kbit` | kbit/s, emulated bottleneck | system | On change | direction |
| `cpu_percent` | Percent of one core | system | Per second | component |
| `reclassifications` | Count | controller | Per second | none |
| `flows_labelled` | Count | controller | Per second | none |
| `flows_unlabelled` | Count | controller | Per second | none |
| `inference_us` | µs | controller | Per classification | none |

`page_load_ms` was added in chunk 5B: without it a web page's experience could only be guessed from throughput.

**Configurations.** `B0` to `O2` are the configurations the report compares. `D1` to `D4` exist only to measure the damage table (chunk 5B): plain CAKE as in `B2`, marking installed, every host's flows marked into the tin of its entry's class, and the host of the traffic entry called `subject` marked into tin 1 to 4 (Bulk, Best Effort, Video, Voice) instead.

Fairness (Jain's index), percentiles and adaptation time are computed from these in the report. They are not logged.

The live exporter exposes each metric to Prometheus as `aqos_<metric>` with the same labels. `latency_ms` is exposed as a histogram; the rest are gauges.

## Traffic parameters

A parameter left out is drawn from its range using the run seed. Every profile also takes `ip_version` (4 or 6, default 4). Sizes are UDP payload bytes.

| Profile | Parameters (range when drawn) |
| --- | --- |
| `voice_call` | `payload_up`, `payload_down` (100 to 200) |
| `video_call` | `kbit_up`, `kbit_down` (1000 to 2500), `audio_payload` (100 to 200), `keyframe_s` (5 to 15), `deadline_ms` (100) |
| `gaming` | `tick_hz` (30 to 128), `payload_up`, `payload_down` (50 to 200), `deadline_ms` (50) |
| `cloud_gaming` | `kbit_down` (10000 to 25000), `payload_up` (40 to 100), `keyframe_s` (2 to 10), `deadline_ms` (50) |
| `abr_video` | `segment_s` (2 to 4), `max_kbit` (3000 to 8000), `buffer_max_s` (30), `reservoir_s` (5), `cushion_s` (20) |
| `web_browsing` | `think_s` (5 to 15), `parallel` (6) |
| `bulk_download` | `connections` (1 to 4) |
| `game_update` | `connections` (4 to 8) |
| `backup_upload` | `connections` (1 to 2) |
| `p2p` | `tcp_down`, `tcp_up` (4 to 10), `udp_peers` (4 to 12) |
| `flow_storm` | `flows_per_s` (20 to 100), `udp_share` (0.5) |

## Files a run writes besides the contracts

These are local to a run directory and are not contracts, but the runner and the dataset builder read them.

| File | Written by | Contents |
| --- | --- | --- |
| `flows.jsonl` | Generator clients | One line per flow: traffic id, profile, true class, and the 5-tuple as the LAN side sees it. Holds addresses, so it never leaves the run directory |
| `gen/<traffic_id>.<role>.json` | Each generator, last | `status` (`ok` or `error`), parameters, plan digest, counts, send-timing error. A missing file means the generator died |

## Extensions and resolved choices

- **Event arguments in scenarios** are free-form objects for now. They are tightened when the scenario definitions (6C) exist. The testbed already takes `link_rate` (`direction`, `rate_kbit`) and `link_impair` (`delay_ms`, `jitter_ms`, `loss_pct`).
- **Traffic parameters** are still a free-form object in the schema, but the generators (2B) now define the names and reject any other. They move into the schema with 6C.
- **Lease format** for the intent service belongs to chunk 7D.
- **Which proposals count as structural** for the shield's rate limit and canary belongs to chunk 5D.
- **Transport (resolved).** The Rust controller and classifier exchange JSON lines over a local Unix socket. The migrated implementations preserve the payloads used by the Go/Python references.

## Changing a contract

1. Change the schema, the examples, the Rust types in `rust/aqos-runtime` and `rust/aqos-classify`, and the Go reference types in `contracts.go` together.
2. Run `make check`.
3. A change that breaks existing records bumps `schema_version`.
