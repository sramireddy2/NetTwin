#!/bin/sh
# Golden kernel state for r3: trunk sub-interfaces for VLAN 10/20 and the guest packet filter.
# Sub-interfaces are recreated so a fault that retagged one is undone. Idempotent.
set -e
setaddr() { ip -4 addr flush dev "$1"; ip addr add "$2" dev "$1"; ip link set dev "$1" mtu "${3:-1500}" up; }
ip -4 addr flush dev lo scope global
ip addr add 10.255.0.3/32 dev lo
setaddr eth1 10.0.13.2/30
setaddr eth2 10.0.23.1/30
ip -4 addr flush dev eth3
ip link set dev eth3 mtu 1500 up
for l in $(ip -o link show type vlan | awk -F': ' '{print $2}' | cut -d@ -f1); do
  ip link del dev "$l"
done
ip link add link eth3 name eth3.10 type vlan id 10
ip link add link eth3 name eth3.20 type vlan id 20
setaddr eth3.10 10.0.10.1/24
setaddr eth3.20 10.0.20.1/24
nft flush ruleset
nft -f /nft.conf
