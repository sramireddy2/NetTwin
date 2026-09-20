#!/bin/sh
# Golden state for sw1: a VLAN-aware Linux bridge.
#   eth1  trunk to r3, VLAN 10 and 20 tagged
#   eth2  access VLAN 10 (h10, corporate)
#   eth3  access VLAN 20 (h20, guest)
# The bridge is rebuilt from scratch so any port re-tagging fault is undone. Idempotent.
set -e
ip link del br0 2>/dev/null || true
ip link add br0 type bridge vlan_filtering 1
for p in eth1 eth2 eth3; do
  ip link set dev "$p" master br0
  ip link set dev "$p" mtu 1500 up
done
ip link set dev br0 up
bridge vlan del dev br0 vid 1 self 2>/dev/null || true
for p in eth1 eth2 eth3; do bridge vlan del dev "$p" vid 1 2>/dev/null || true; done
bridge vlan add dev eth1 vid 10
bridge vlan add dev eth1 vid 20
bridge vlan add dev eth2 vid 10 pvid untagged
bridge vlan add dev eth3 vid 20 pvid untagged
