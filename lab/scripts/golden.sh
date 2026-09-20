#!/bin/bash
# Re-apply golden state on every node, in parallel, then wait for convergence.
#   routers: frr-reload.py diffs the running config against /golden/frr.conf and applies it
#   all nodes: sh /setup.sh restores addresses, MTU, VLANs, bridge ports and nftables
# Usage: LAB=nettwin golden.sh
set -u
LAB=${LAB:-nettwin}
ROUTERS="r1 r2 r3 r4 isp"
NODES="r1 r2 r3 r4 isp sw1 h10 h20 srv inet"
LOGDIR=$(mktemp -d)
HERE=$(cd "$(dirname "$0")" && pwd)

run_parallel() {
  # run_parallel <label> <node list> <command...>   (command runs inside each node)
  local label=$1 nodes=$2
  shift 2
  local pids=() names=() fail=0
  for n in $nodes; do
    docker exec "clab-$LAB-$n" "$@" >"$LOGDIR/$label-$n.log" 2>&1 &
    pids+=($!)
    names+=("$n")
  done
  for i in "${!pids[@]}"; do
    if ! wait "${pids[$i]}"; then
      echo "golden: $label failed on ${names[$i]}:" >&2
      sed 's/^/  /' "$LOGDIR/$label-${names[$i]}.log" >&2
      fail=1
    fi
  done
  return $fail
}

start=$(date +%s)
run_parallel frr-reload "$ROUTERS" /usr/lib/frr/frr-reload.py --reload /golden/frr.conf || exit 1
run_parallel setup "$NODES" sh /setup.sh || exit 1
echo "golden: applied in $(( $(date +%s) - start ))s"
rm -rf "$LOGDIR"
LAB=$LAB "$HERE/converge.sh"
