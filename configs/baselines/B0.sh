#!/bin/sh
# B0: no QoS. The router keeps its default qdisc, so the queue forms in the
# bottleneck FIFO in isp, as it does behind a plain modem.
#
# Run inside the router namespace: B0.sh <down_kbit> <up_kbit>
set -eu
echo "B0: router left alone"
