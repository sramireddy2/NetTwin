#!/bin/bash
# Golden reachability check. Mirrors lab/policy/intent.yaml with plain pings so it works
# before netverify exists. Exit 1 if any expectation fails.
set -u
LAB=${LAB:-nettwin}
fail=0

run() {
  local node=$1; shift
  docker exec "clab-$LAB-$node" "$@" >/dev/null 2>&1
}

expect() {
  # expect <name> <ok|blocked> <node> <command...>
  local name=$1 want=$2 node=$3 got
  shift 3
  if run "$node" "$@"; then got=ok; else got=blocked; fi
  if [ "$got" = "$want" ]; then
    printf 'PASS %-18s %s\n' "$name" "$got"
  else
    printf 'FAIL %-18s wanted %s, got %s\n' "$name" "$want" "$got"
    fail=1
  fi
}

PING=(ping -c 2 -W 1)
BIGPING=(ping -c 2 -W 1 -M do -s 1472)

expect corp-to-srv      ok      h10  "${PING[@]}" 10.0.40.10
expect corp-to-srv-mtu  ok      h10  "${BIGPING[@]}" 10.0.40.10
expect corp-to-inet     ok      h10  "${PING[@]}" 198.51.100.10
expect guest-to-inet    ok      h20  "${PING[@]}" 198.51.100.10
expect guest-no-srv     blocked h20  "${PING[@]}" 10.0.40.10
expect srv-to-corp      ok      srv  "${PING[@]}" 10.0.10.10
expect inet-to-srv      ok      inet "${PING[@]}" 10.0.40.10
expect inet-no-guest    blocked inet "${PING[@]}" 10.0.20.10
expect lo-r1-to-r4      ok      r1   "${PING[@]}" 10.255.0.4
expect lo-r3-to-r4      ok      r3   "${PING[@]}" 10.255.0.4

if [ "$fail" = 0 ]; then
  echo "check: all passed"
else
  echo "check: failures" >&2
  exit 1
fi
