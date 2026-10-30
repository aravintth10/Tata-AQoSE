# Pinned versions

Pinned on Oct 9, 2026 from the reference laptop. All results in the report come from this set. `make env` checks a machine against it.

| Component | Version | Where it is pinned |
| --- | --- | --- |
| OS | Fedora 43 | This file |
| Kernel | 7.2.8-100.fc43.x86_64 | This file; `make env` accepts any 7.2.x |
| iproute2 (`tc`, `ip`) | 6.14.0 | This file |
| nftables | 1.1.3 | This file |
| tcpdump | 4.99.6 | This file |
| Go (testbed and comparison references) | 1.26.8 | `go.mod` (`go 1.26`) |
| Rust (default classifier and runtime) | 1.99.1 | `rust/{aqos-classify,aqos-runtime,aqos-watchdog}/Cargo.lock` pin dependencies; benchmark JSON records the compiler |
| gopkg.in/yaml.v3 | 3.0.1 | `go.mod`, `go.sum` |
| Python | 3.14.7 | This file |
| scikit-learn | 1.9.1 | `requirements.txt` |
| LightGBM | 4.7.0 | `requirements.txt` |
| NumPy | 2.5.3 | `requirements.txt` |
| pandas | 3.0.6 | `requirements.txt` |
| PyYAML | 6.0.3 | `requirements.txt` |
| jsonschema | 4.26.0 | `requirements.txt` |
| pytest | 9.1.1 | `requirements.txt` |
| Grafana | 13.2.3 (`grafana/grafana:13.2.3`) | This file; the compose file arrives with chunk 3A |
| Prometheus | 3.15.0 (`prom/prometheus:v3.15.0`) | This file; the compose file arrives with chunk 3A |

`requirements.lock` holds the full resolved Python set, including transitive dependencies. Install from the lock file, not from `requirements.txt`.

## Kernel modules the testbed needs

`sch_cake`, `sch_netem`, `sch_tbf`, `sch_htb`, `sch_fq_codel`, `act_ctinfo`, `ifb`, `nft_ct`. All are present on the reference laptop.

## Not installed yet on the reference laptop

These are optional until the chunk that uses them, and need `sudo`:

```
sudo dnf install conntrack-tools iperf3 irtt
```

| Tool | Fedora 43 version | Needed for |
| --- | --- | --- |
| conntrack-tools | 1.4.9 | Inspecting conntrack marks and counters while debugging the enforcer |
| iperf3 | 3.19.1 | Checking the bottleneck rate in the testbed |
| irtt | 0.9.1 | Delay probes for the capacity estimator |

## Notes

- CAKE with `diffserv4` attaches inside a rootless user and network namespace (`unshare -Urn`) on this kernel. Chunk 2A decided: the testbed runs without `sudo`. It needs unprivileged user namespaces, plus `nsenter`, `unshare`, `ethtool`, `taskset` and `ping` on the path.
- Conntrack accounting is off by default on the host (`net.netfilter.nf_conntrack_acct = 0`). The testbed must turn it on inside the router namespace.
- The second laptop has not been checked. Run `make env` there and record any difference in this file.
