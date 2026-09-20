#!/bin/sh
# Golden kernel state for r4: addresses and guest NAT. Idempotent.
set -e
# Drop the containerlab management default route (distance 0 in FRR) so the routing
# protocols' default is what gets installed.
ip route del default dev eth0 2>/dev/null || true
setaddr() { ip -4 addr flush dev "$1"; ip addr add "$2" dev "$1"; ip link set dev "$1" mtu "${3:-1500}" up; }
ip -4 addr flush dev lo scope global
ip addr add 10.255.0.4/32 dev lo
setaddr eth1 10.0.14.2/30
setaddr eth2 10.0.24.1/30
setaddr eth3 203.0.113.1/30
nft flush ruleset
nft -f /nft.conf
# Drop cached path MTUs so probes see the real path after a change.
ip route flush cache
