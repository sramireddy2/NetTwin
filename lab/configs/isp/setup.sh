#!/bin/sh
# Golden kernel state for isp. Idempotent.
set -e
setaddr() { ip -4 addr flush dev "$1"; ip addr add "$2" dev "$1"; ip link set dev "$1" mtu "${3:-1500}" up; }
setaddr eth1 203.0.113.2/30
setaddr eth2 198.51.100.1/24
nft flush ruleset
