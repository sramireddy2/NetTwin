#!/bin/sh
# srv: file server on the server subnet behind r2. Idempotent.
set -e
ip -4 addr flush dev eth1
ip addr add 10.0.40.10/24 dev eth1
ip link set dev eth1 mtu 1500 up
ip route replace default via 10.0.40.1
