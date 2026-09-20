# Lab notes

Hands-on record of the reference topology, with evidence captured from the real lab on
2026-09-20 (containerlab 0.79.0, FRR 10.2.1, Docker 27.5.1 inside the Containerlab WSL2 distro).

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

Routing policy: the ISP sends r4 a default with `default-originate`; r4 originates it into
OSPF. r4 advertises the server subnet with a `network` statement and corporate VLAN 10
through `redistribute ospf` filtered by prefix-list `CORP`. Guest VLAN 20 is never
advertised; it is masqueraded on r4 toward the ISP. r3 drops guest traffic to 10.0.0.0/16.

Deviation from the design doc: no iBGP between r1 and r2. Nothing in the scenario list
needs it and the eBGP edge is enough for the BGP faults.

## Bring-up

```
nettwin lab up        # images, sync, deploy, converge, check
nettwin lab check
```

Evidence: with images cached, `make up` completes in 34 s, OSPF and BGP converge in 8 to
13 s, and all ten checks pass:

```
converged in 8s
PASS corp-to-srv        ok
PASS corp-to-srv-mtu    ok
PASS corp-to-inet       ok
PASS guest-to-inet      ok
PASS guest-no-srv       blocked
PASS srv-to-corp        ok
PASS inet-to-srv        ok
PASS inet-no-guest      blocked
PASS lo-r1-to-r4        ok
PASS lo-r3-to-r4        ok
check: all passed
```

`make golden` (parallel frr-reload + setup.sh on every node) applies in about 1 s and
converges in 8 to 9 s, so a benchmark run can reset the twin in roughly 10 s.

## Things the lab taught us

- **Containerlab's management default route shadows the routing protocols.** Every
  container gets `default via 172.20.20.1 dev eth0`. FRR treats kernel routes as
  distance 0, so the BGP and OSPF defaults were never installed and replies from srv and
  traffic from r3 toward the ISP silently left through the Docker bridge. Router
  `setup.sh` scripts now delete that route first.
- **WSL2 idle shutdown kills the lab.** The VM stops about a minute after the last
  Windows-side session closes; on restart Docker brings the containers back without their
  veth links. Keep a session open (`wsl -d Containerlab -- sleep infinity`) or raise
  `vmIdleTimeout`. `make up` always uses `--reconfigure` so a stale lab is recoverable.
- **The stock FRR image has no `/etc/frr/vtysh.conf`**, which makes every `vtysh -c`
  print two warning lines before the real output. The image adds the file.
- **FRR refuses to change an interface's OSPF area in one step**
  (`Must remove previous area config before changing ospf area`). Injections and fixes
  that move an interface between areas must `no ip ospf area X` first. Scenario 001 uses
  the two-line form.
- **`frr-reload.py` works in the container** and is what `golden` uses to diff the
  running config against `/golden/frr.conf`; the running-config only differs from the
  golden file in FRR's canonical ordering (`exit` lines, timer order, `domainname`).

## Fault 1: OSPF area mismatch (scenario 001)

Inject on r3 (two lines, see above):

```
docker exec clab-nettwin-r3 vtysh -c "conf t" -c "interface eth1" \
  -c "no ip ospf area 0.0.0.1" -c "ip ospf area 0.0.0.0"
```

Evidence: within 5 s r1 lists only r2 and r4; r3 eth1 reports `Area 0.0.0.0`. h10 still
reaches srv through r2, so the visible symptom is loss of redundancy and `ospf-r1-eth2`
going red, not an outage.

```
Neighbor ID     Pri State           Up Time   Address         Interface
10.255.0.2        1 Full/-          27.958s   10.0.12.2       eth1:10.0.12.1
10.255.0.4        1 Full/-          27.955s   10.0.14.2       eth3:10.0.14.1
  Internet Address 10.0.13.2/30, Broadcast 10.0.13.3, Area 0.0.0.0
```

`make golden` clears it: converged in 8 s, check all passed.

## Fault 2: OSPF MTU mismatch, adjacency stuck (scenario 004)

Inject on r2:

```
docker exec clab-nettwin-r2 ip link set dev eth1 mtu 1400
```

Evidence: no `clear` needed. Within 6 s the r1–r2 adjacency drops out of Full and sticks
in Exchange with the retransmit counter climbing:

```
Neighbor ID     Pri State           Up Time   Address      Interface        RXmtL
10.255.0.2        1 Exchange/-      5.229s    10.0.12.2    eth1:10.0.12.1       1
10.255.0.2        1 Exchange/-      7.727s    10.0.12.2    eth1:10.0.12.1       7
```

`make golden` (setup.sh restores the MTU) clears it.

## Fault 3: missing BGP network statement (scenario 006)

Inject on r4:

```
docker exec clab-nettwin-r4 vtysh -c "conf t" -c "router bgp 65001" \
  -c "address-family ipv4 unicast" -c "no network 10.0.40.0/24"
```

Evidence: within 4 s `show ip route 10.0.40.0/24` on isp answers `% Network not in
table`, `inet` can no longer ping srv, while h10 to srv still works. `make golden`
(frr-reload re-adds the statement) clears it.

## MTU in veth

`tests/lab/test_mtu_veth.py` and the same steps by hand: with r2 eth4 at MTU 1400, a
1300-byte DF ping from srv to 10.0.40.1 passes (2/2), a 1472-byte one is dropped (0/2),
and after restoring 1500 the 1472-byte ping passes again. Scenarios 004 and 005 are viable.

## NetBench tier A scenarios

Each scenario in `lab/scenarios/` carries the injected ops, the ground truth, the expected
minimal fix, the intent rules that should go red, and a one-command probe that shows the
symptom. `tests/lab/test_scenarios.py` injects every scenario through the admin path, waits
for the probe to match, rolls back to the pre-injection snapshot and requires a byte-identical
match before moving on.

| Id | Layer | Root cause (node, component) | Expected red rules |
|---|---|---|---|
| 001-ospf-area-mismatch | L3 | r3: `ospf.area` | ospf-r1-eth2, ospf-r3-eth1 |
| 002-ospf-passive-transit | L3 | r2: `ospf.passive` | ospf-r2-eth2, ospf-r3-eth2 |
| 003-ospf-timer-mismatch | L3 | r1: `ospf.timers` | ospf-r1-eth1, ospf-r2-eth1 |
| 004-ospf-mtu-mismatch | L2 | r2: `link.mtu` | ospf-r1-eth1, ospf-r2-eth1 |
| 005-path-mtu-silent | L2 | r2: `link.mtu` | corp-to-srv-mtu |
| 006-bgp-missing-network | L3 | r4: `bgp.network` | inet-to-srv |
| 007-bgp-route-leak | policy | r4: `bgp.route_map` | no-guest-leak, inet-no-guest |
| 008-bgp-prefix-list-typo | policy | r4: `bgp.prefix_list` | corp-to-inet |
| 009-static-blackhole | L3 | r4: `static.route` | inet-to-srv |
| 010-duplicate-ip | L2 | r4: `ip.address` | ospf-r2-eth3, ospf-r4-eth2 |
| 011-wrong-subnet-mask | L3 | r2: `ip.prefixlen` | corp-to-srv, corp-to-srv-mtu, inet-to-srv |
| 012-interface-shutdown | L2 | r2: `interface.shutdown` | ospf-r2-eth2, ospf-r3-eth2 |
| 013-no-default-originate | L3 | r4: `ospf.default_information` | corp-to-inet, guest-to-inet, inet-to-srv |
| 014-bgp-wrong-remote-as | L3 | r4: `bgp.remote_as` | ebgp-r4-isp, corp-to-inet, guest-to-inet, inet-to-srv |

Lab verification (2026-09-20): all 14 pass in 4 min 13 s total, about 18 s per scenario
including reconvergence. Every rollback matched the pre-injection snapshot byte for byte.
