#!/bin/sh
# Golden kernel state for isp. Idempotent.
set -e
# Drop the containerlab management default route so the blackhole static default wins and
# unknown destinations are dropped here rather than leaking into the management network.
ip route del default dev eth0 2>/dev/null || true
setaddr() { ip -4 addr flush dev "$1"; ip addr add "$2" dev "$1"; ip link set dev "$1" mtu "${3:-1500}" up; }
setaddr eth1 203.0.113.2/30
setaddr eth2 198.51.100.1/24
nft flush ruleset
