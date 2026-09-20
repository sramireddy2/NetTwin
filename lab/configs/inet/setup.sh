#!/bin/sh
# inet: a host on "the internet" behind the ISP. Idempotent.
set -e
ip -4 addr flush dev eth1
ip addr add 198.51.100.10/24 dev eth1
ip link set dev eth1 mtu 1500 up
ip route replace default via 198.51.100.1
# Drop cached path MTUs so probes see the real path after a change.
ip route flush cache
