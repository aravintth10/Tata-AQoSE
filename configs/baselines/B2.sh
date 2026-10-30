#!/bin/sh
# B2: plain CAKE diffserv4 at a fixed rate, in both directions, with
# per-host isolation. No marks: CAKE picks the tin from each packet's DSCP.
#
# Run inside the router namespace: B2.sh <down_kbit> <up_kbit>
#
# The shaper sits below the true bottleneck, or the queue would form in isp
# where CAKE cannot manage it. SHAPER_PCT is a starting value.
set -eu
down_kbit=$1
up_kbit=$2
pct=${SHAPER_PCT:-95}
down=$((down_kbit * pct / 100))
up=$((up_kbit * pct / 100))

# Upload: on the WAN interface itself.
tc qdisc replace dev wan0 root cake bandwidth "${up}kbit" diffserv4 dual-srchost nat

# Download: Linux shapes only what it sends, so packets arriving on the WAN
# interface are redirected to an IFB device and shaped as they leave it.
ip link add ifb0 type ifb 2>/dev/null || true
ip link set ifb0 up
tc qdisc del dev wan0 ingress 2>/dev/null || true
tc qdisc add dev wan0 handle ffff: ingress
tc filter replace dev wan0 parent ffff: protocol all prio 10 matchall \
    action mirred egress redirect dev ifb0
tc qdisc replace dev ifb0 root cake bandwidth "${down}kbit" diffserv4 dual-dsthost nat ingress

echo "B2: cake diffserv4, down ${down} kbit on ifb0, up ${up} kbit on wan0"
