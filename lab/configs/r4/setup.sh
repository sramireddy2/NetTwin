#!/bin/sh
# Golden kernel state for r4: addresses and guest NAT. Idempotent.
set -e
setaddr() { ip -4 addr flush dev "$1"; ip addr add "$2" dev "$1"; ip link set dev "$1" mtu "${3:-1500}" up; }
ip -4 addr flush dev lo scope global
ip addr add 10.255.0.4/32 dev lo
setaddr eth1 10.0.14.2/30
setaddr eth2 10.0.24.1/30
setaddr eth3 203.0.113.1/30
nft flush ruleset
nft -f /nft.conf
