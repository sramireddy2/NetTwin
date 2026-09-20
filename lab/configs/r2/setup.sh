#!/bin/sh
# Golden kernel state for r2. Idempotent.
set -e
setaddr() { ip -4 addr flush dev "$1"; ip addr add "$2" dev "$1"; ip link set dev "$1" mtu "${3:-1500}" up; }
ip -4 addr flush dev lo scope global
ip addr add 10.255.0.2/32 dev lo
setaddr eth1 10.0.12.2/30
setaddr eth2 10.0.23.2/30
setaddr eth3 10.0.24.2/30
setaddr eth4 10.0.40.1/24
nft flush ruleset
