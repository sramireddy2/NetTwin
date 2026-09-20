#!/bin/sh
# Golden kernel state for r1. Idempotent: re-run by `make golden` to undo faults.
set -e
# Drop the containerlab management default route (distance 0 in FRR) so the routing
# protocols' default is what gets installed.
ip route del default dev eth0 2>/dev/null || true
setaddr() { ip -4 addr flush dev "$1"; ip addr add "$2" dev "$1"; ip link set dev "$1" mtu "${3:-1500}" up; }
ip -4 addr flush dev lo scope global
ip addr add 10.255.0.1/32 dev lo
setaddr eth1 10.0.12.1/30
setaddr eth2 10.0.13.1/30
setaddr eth3 10.0.14.1/30
nft flush ruleset
# Drop cached path MTUs so probes see the real path after a change.
ip route flush cache
