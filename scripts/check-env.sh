#!/usr/bin/env bash
# Compares this machine against the pinned versions in VERSIONS.md.
# Exits non-zero if anything required is missing or on a different version.
set -u

fail=0
ok()   { printf '  ok    %s\n' "$1"; }
warn() { printf '  warn  %s\n' "$1"; }
bad()  { printf '  FAIL  %s\n' "$1"; fail=1; }

# want_version <label> <expected substring> <command...>
want_version() {
    local label=$1 expected=$2
    shift 2
    local got
    if ! got=$("$@" 2>&1 | head -n 1); then
        bad "$label: not installed (want $expected)"
    elif [[ $got == *"$expected"* ]]; then
        ok "$label $expected"
    else
        bad "$label: want $expected, got: $got"
    fi
}

echo "Tools"
want_version "kernel"   "7.2."     uname -r
want_version "iproute2" "6.14.0"   tc -V
want_version "nftables" "v1.1.3"   nft --version
want_version "go"       "go1.26."  go version
want_version "rust"     "1.99.1"   "${RUSTC:-$HOME/.cargo/bin/rustc}" --version
want_version "cargo"    "1.99.1"   "${CARGO:-$HOME/.cargo/bin/cargo}" --version
want_version "python"   "3.14."    python3 --version
want_version "tcpdump"  "4.99."    tcpdump --version

if command -v docker >/dev/null 2>&1 || command -v podman >/dev/null 2>&1; then
    ok "container runtime (for Grafana and Prometheus)"
else
    bad "container runtime: need docker or podman"
fi

echo "Kernel modules"
for mod in sch_cake sch_netem sch_tbf sch_htb sch_fq_codel act_ctinfo ifb nft_ct; do
    if modinfo "$mod" >/dev/null 2>&1; then
        ok "$mod"
    else
        bad "$mod: not available"
    fi
done

echo "CAKE in a network namespace"
if unshare -Urn sh -c 'ip link set lo up && tc qdisc add dev lo root cake bandwidth 100mbit diffserv4' >/dev/null 2>&1; then
    ok "cake diffserv4 attaches without root"
else
    warn "cake did not attach in a rootless namespace; the testbed will need sudo"
fi

echo "Testbed tools"
for tool in nsenter unshare ethtool taskset ping sysctl; do
    if command -v "$tool" >/dev/null 2>&1; then
        ok "$tool"
    else
        bad "$tool: not installed"
    fi
done

echo "Optional tools"
for tool in conntrack iperf3 irtt; do
    if command -v "$tool" >/dev/null 2>&1; then
        ok "$tool"
    else
        warn "$tool missing: sudo dnf install conntrack-tools iperf3 irtt"
    fi
done

echo "Python environment"
if [[ -x .venv/bin/python ]]; then
    if .venv/bin/pip freeze 2>/dev/null | diff -q - requirements.lock >/dev/null; then
        ok ".venv matches requirements.lock"
    else
        bad ".venv differs from requirements.lock (run: make venv)"
    fi
else
    bad ".venv missing (run: make venv)"
fi

exit $fail
