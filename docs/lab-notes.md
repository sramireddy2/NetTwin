# Lab notes

Hands-on record of the reference topology. Evidence blocks are filled in from the real lab
the first time each step runs; until then they say "pending".

## Addressing

| Node | Interface | Address | Peer | OSPF |
|---|---|---|---|---|
| r1 | lo | 10.255.0.1/32 | | area 0, passive |
| r1 | eth1 | 10.0.12.1/30 | r2 eth1 | area 0, p2p |
| r1 | eth2 | 10.0.13.1/30 | r3 eth1 | area 1, p2p |
| r1 | eth3 | 10.0.14.1/30 | r4 eth1 | area 0, p2p |
| r2 | lo | 10.255.0.2/32 | | area 0, passive |
| r2 | eth1 | 10.0.12.2/30 | r1 eth1 | area 0, p2p |
| r2 | eth2 | 10.0.23.2/30 | r3 eth2 | area 1, p2p |
| r2 | eth3 | 10.0.24.2/30 | r4 eth2 | area 0, p2p |
| r2 | eth4 | 10.0.40.1/24 | srv | area 0, passive |
| r3 | lo | 10.255.0.3/32 | | area 1, passive |
| r3 | eth1 | 10.0.13.2/30 | r1 eth2 | area 1, p2p |
| r3 | eth2 | 10.0.23.1/30 | r2 eth2 | area 1, p2p |
| r3 | eth3.10 | 10.0.10.1/24 | sw1 trunk, VLAN 10 | area 1, passive |
| r3 | eth3.20 | 10.0.20.1/24 | sw1 trunk, VLAN 20 | area 1, passive |
| r4 | lo | 10.255.0.4/32 | | area 0, passive |
| r4 | eth1 | 10.0.14.2/30 | r1 eth3 | area 0, p2p |
| r4 | eth2 | 10.0.24.1/30 | r2 eth3 | area 0, p2p |
| r4 | eth3 | 203.0.113.1/30 | isp eth1 | eBGP AS 65001 |
| isp | eth1 | 203.0.113.2/30 | r4 eth3 | eBGP AS 65000 |
| isp | eth2 | 198.51.100.1/24 | inet | |
| h10 | eth1 | 10.0.10.10/24 | sw1 eth2, VLAN 10 | gw 10.0.10.1 |
| h20 | eth1 | 10.0.20.10/24 | sw1 eth3, VLAN 20 | gw 10.0.20.1 |
| srv | eth1 | 10.0.40.10/24 | r2 eth4 | gw 10.0.40.1 |
| inet | eth1 | 198.51.100.10/24 | isp eth2 | gw 198.51.100.1 |

Routing policy: r4 originates a default into OSPF from the ISP's BGP default. r4 advertises
the server subnet with a `network` statement and corporate VLAN 10 through
`redistribute ospf` filtered by prefix-list `CORP`. Guest VLAN 20 is never advertised; it
is masqueraded on r4 toward the ISP. r3 drops guest traffic to anything in 10.0.0.0/16.

Deviation from the design doc: no iBGP between r1 and r2. Nothing in the scenario list
needs it and the eBGP edge is enough for the BGP faults.

## Bring-up

```
nettwin lab up        # images, sync, deploy, converge, check
nettwin lab check
```

Evidence (cold start time, converge time, check output): pending.

## Fault 1: OSPF area mismatch (scenario 001)

Inject on r3:

```
docker exec clab-nettwin-r3 vtysh -c "conf t" -c "interface eth1" -c "ip ospf area 0.0.0.0"
```

Expect: r1 loses its neighbour on eth2 within 3 s; h10 still reaches srv through r2, so the
symptom is loss of redundancy plus `ospf-r1-eth2` red. Clear with `nettwin lab golden`.

Evidence: pending.

## Fault 2: OSPF MTU mismatch, adjacency stuck in ExStart (scenario 004)

Inject on r2:

```
docker exec clab-nettwin-r2 ip link set dev eth1 mtu 1400
```

Expect: r1-r2 adjacency drops to ExStart/Exchange and never reaches Full because the DBD
MTU field differs. `show ip ospf neighbor` on r1 shows the state. Clear with golden.

Evidence: pending.

## Fault 3: missing BGP network statement (scenario 006)

Inject on r4:

```
docker exec clab-nettwin-r4 vtysh -c "conf t" -c "router bgp 65001" \
  -c "address-family ipv4 unicast" -c "no network 10.0.40.0/24"
```

Expect: `inet` can no longer reach `srv` (isp loses 10.0.40.0/24) while everything inside
still works. `show ip route 10.0.40.0/24` on isp is empty. Clear with golden.

Evidence: pending.

## MTU in veth

`tests/lab/test_mtu_veth.py` lowers r2 eth4 to 1400 and confirms a 1472-byte DF ping from
srv fails while a 1300-byte one passes. Result: pending.
