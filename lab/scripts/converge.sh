#!/bin/bash
# Wait until every OSPF adjacency is Full and the r4-isp eBGP session is Established.
# Usage: LAB=nettwin TIMEOUT=90 converge.sh
set -u
LAB=${LAB:-nettwin}
TIMEOUT=${TIMEOUT:-90}

declare -A WANT=( [r1]=3 [r2]=3 [r3]=2 [r4]=2 )

full_count() {
  local out
  out=$(docker exec "clab-$LAB-$1" vtysh -c "show ip ospf neighbor" 2>/dev/null | grep -c "Full")
  out=${out//[^0-9]/}
  echo "${out:-0}"
}

bgp_established() {
  docker exec "clab-$LAB-r4" vtysh -c "show bgp neighbors 203.0.113.2 json" 2>/dev/null \
    | grep -q '"bgpState":"Established"'
}

start=$(date +%s)
while :; do
  ok=1
  for n in r1 r2 r3 r4; do
    have=$(full_count "$n")
    [ "$have" -ge "${WANT[$n]}" ] || ok=0
  done
  bgp_established || ok=0
  now=$(date +%s)
  if [ "$ok" = 1 ]; then
    echo "converged in $((now - start))s"
    exit 0
  fi
  if [ $((now - start)) -ge "$TIMEOUT" ]; then
    echo "not converged after ${TIMEOUT}s" >&2
    for n in r1 r2 r3 r4; do
      echo "  $n: $(full_count "$n") of ${WANT[$n]} OSPF neighbours Full" >&2
    done
    bgp_established || echo "  r4: eBGP to isp not Established" >&2
    exit 1
  fi
  sleep 2
done
