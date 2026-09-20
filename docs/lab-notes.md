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
- **Hosts cache path MTU for ten minutes.** When r2 forwards a 1500-byte DF probe out a
  1400-byte link it answers ICMP fragmentation-needed and h10 caches a path MTU of 1400.
  Restoring the interface does not clear that cache, so after scenario 005 every later
  DF probe failed locally and a correct MTU fix would have looked broken to the verifier.
  `apply`, `rollback` and the golden scripts now end with `ip route flush cache` on every
  node. The first full verifier run over all scenarios found this; the second passed.

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

Verifier check (2026-09-20): with netverify, golden passes every rule and receives a signed
attestation, and each scenario fails exactly its `expected_failed_rules` (15 tests, 10 min
14 s including a convergence wait per scenario).

## NetBench harness with the fake agent (M6)

`tests/lab/test_fake_agent.py` drives the whole benchmark loop in-process against the live
lab: baseline (wait for convergence, golden snapshot, golden reachability matrix), then per
scenario rollback to golden, inject through the admin path, wait for convergence, run the
agent, take the after matrix, score, append one JSONL record, rollback again. The fake agent
replays each scenario's `expected_fix` through the real tool surface (topology resource, the
probe show command, `snapshot`, `apply_config`, `wait_converged`, `intent_check`,
`export_change`), so it exercises the servers, the harness and the scoring at zero token
cost and sets the ceiling every real agent is measured against.

Lab result (2026-09-20), `NETTWIN_LAB=1 uv run pytest tests/lab/test_fake_agent.py`:

| Config | Runs | Root cause | Verified fix | No collateral | Minimal | Errors | Mean s |
|---|---|---|---|---|---|---|---|
| fake | 14 | 100% | 100% | 100% | 100% | 0 | 22 |

All 14 scenarios scored `RVC`. Wall clock 13 min 18 s, which includes the two rollbacks
and the convergence waits around every run; the agent's own part averages 22 s.

CLI over HTTP from Windows (2026-09-20), `uv run netbench run --runner fake --matrix v0`
against `make serve-bench`: the same 14/14 `RVC`, 0 errors, 14/14 exports auto-approved,
recorded in `results/v0/runs.jsonl`. Mean 33 s per run, 16 min 42 s wall clock. The mean is
skewed by the first run: its `wait_converged` call, given a 60 s budget, returned after 153 s,
so one poll of the concurrent show commands stalled for about 90 s. The other 13 runs took 18
to 43 s and the stall did not recur in the other 29 fake-agent runs of the day.

## Claude Code roles and the manual runner (M7)

`.claude/agents/` holds five subagent role files (three read-only investigators, the change
agent, the verifier) and `.claude/skills/` the `/diagnose` and `/diagnose-solo` workflows;
`docs/diagnose.md` is the runbook. `tests/unit/test_roles.py` parses the frontmatter and
checks every tool name against the live tool lists of both servers, that the investigators
hold only `run_show_command`, that the change agent cannot export, and that the verifier is
scoped to netverify.

To score an interactive run, `netbench run --runner manual` resets and injects as usual,
prints the symptom, then polls twinlab's admin route for a new export bundle and scores it
once the operator has decided. Live check (2026-09-20), servers in bench mode, with a
scripted stand-in replaying the expected fix over HTTP in place of the Claude Code session:
scenario 001 was injected 50 s after start (baseline, reset, inject, convergence), the export
`4ac8ba67607e` was picked up within one poll and scored `RVC` (36 s from injection to score),
and the twin was rolled back to golden afterwards.

Interactive run with the real team (2026-09-20, Claude Code desktop session in the repo,
model inherited by every role, servers without bench mode), scored by `netbench run --runner
manual --name claude-team --scenarios 001 --matrix interactive` and recorded in
`results/interactive/runs.jsonl`:

| Config | Runs | Root cause | Verified fix | No collateral | Minimal | Errors |
|---|---|---|---|---|---|---|
| claude-team | 1 | 100% | 100% | 100% | 100% | 0 |

What happened, from the transcript: the commander read `lab://topology` and took S0; the
three investigators ran in parallel (L2 in 64 s with 15 show commands, L3 in 42 s with 12,
policy in 52 s with 9) and all three named r3 `ospf.area` with evidence from both ends of the
link, the policy investigator ruling out every filter and BGP policy explicitly; the change
agent confirmed the state, applied one `frr_lines` op on r3 and saw the adjacency Full on
both ends within 6 s, and its after snapshot S1 was byte-identical to the golden baseline;
the verifier, given only S0, S1, the policy path and the symptom, passed all 20 rules with
the attestation bound to S1 and confirmed nothing else was in its prompt. From S0 to the
export bundle took 4 min 4 s (the harness's 474 s includes the wait before the operator
started). The Claude Code desktop app did not surface the MCP elicitation, so `export_change`
returned `pending` and the operator decides with `nettwin approve d51b24a0865e`; the score
counts a pending bundle as exported. The verifier subagent's transcript contains only its
prompt and three netverify calls (`wait_converged`, `intent_check`, `route_diff`).

## NetBench tier B, C, stretch and control scenarios (M8)

Tier B breaks VLANs on the switch and the gateway; tier C breaks nftables policy. Two
mechanics were needed: `NftRule` `delete` now accepts the rule text and twinlab resolves the
kernel-assigned handle on the node at apply time (`nft -a list chain`, quotes stripped,
protocol names compared numerically), and r3's golden ruleset gained an empty `input` chain
so a host-firewall fault has somewhere to live. Scenarios may carry `extra_causes` (a reported
cause matching any planted fault counts; a verified fix has to remove all of them) or no
`ground_truth` at all (control: the right answer is no cause and no change).

| Id | Tier | Layer | Root cause (node, component) | Expected red rules |
|---|---|---|---|---|
| 015-vlan-access-port | B | L2 | sw1: `vlan.access` | corp-to-srv, corp-to-srv-mtu, corp-to-inet |
| 016-vlan-trunk-missing | B | L2 | sw1: `vlan.trunk` | guest-to-inet |
| 017-vlan-subinterface-tag | B | L2 | r3: `vlan.subinterface` | guest-to-inet |
| 018-nft-rule-order | C | policy | r3: `nft.rule_order` | corp-to-srv, corp-to-srv-mtu |
| 019-nft-nat-missing | C | policy | r4: `nft.nat` | guest-to-inet |
| 020-nft-ospf-filter | C | policy | r3: `nft.filter` | ospf-r2-eth2, ospf-r3-eth2 |
| 021-two-faults-mtu-and-bgp | stretch | L2 + L3 | r2: `link.mtu` and r4: `bgp.network` | ospf-r1-eth1, ospf-r2-eth1, inet-to-srv |
| 022-no-fault-control | control | none | none | none |

Lab verification (2026-09-20): all eight inject, show their symptom on the probe, roll back
byte-identical and fail exactly their expected rules under netverify (the control keeps its
attestation), 17 tests in 6 min 33 s plus a 1 min 27 s re-run after two fixes. The fixes
were a probe, not a fault: when only one router stops receiving hellos its peer keeps the
neighbour listed in Init, so 020 probes `show ip ospf neighbor eth2` for a missing `Full`
instead of a missing neighbour id; and the intent test now expects an attestation on the
control. Two environment lessons: a bind-mounted config file such as r3's `nft.conf` only
changes inside the container after `nettwin lab up` redeploys it, and `make stop-serve` hangs
while a Claude Code session still holds MCP connections to the servers.

Fake agent, `uv run netbench run --runner fake --matrix v0` resumed over the eight new
scenarios (8 min 22 s, servers in bench mode, the earlier 14 runs kept):

| Config | Runs | Root cause | Verified fix | No collateral | Minimal | Errors | Mean s |
|---|---|---|---|---|---|---|---|
| fake | 22 | 100% | 100% | 100% | 100% | 0 | 30 |

The new runs took 17 to 28 s each; the control run applied nothing, exported nothing, and
scored root cause and minimal for exactly that. The first 14 records were taken before r3's
golden ruleset gained its empty input chain, which changes no behaviour.

## Headless Claude Code runner (M9)

`netbench run --runner claude` launches `claude -p` from the repository root with the
symptom on stdin and parses the stream-json events; `docs/diagnose.md` has the flags. The
harness also gained its own judgement: after every run it calls `intent_check` itself and
records `fix_correct`, so the verifier-off ablation still measures whether the network ended
up right. Records from before that column show `_` (per scenario) and `-` (summary) rather
than a fabricated value.

First headless run (2026-09-20, Max subscription, `--model sonnet`, `--skill diagnose`,
servers in bench mode), recorded as the first row of `results/v1/runs.jsonl`:

| Config | Runs | Root cause | Fix correct | Verified | No collateral | Minimal | Errors | Mean s |
|---|---|---|---|---|---|---|---|---|
| claude-diagnose-sonnet | 1 | 100% | 100% | 100% | 100% | 100% | 0 | 299 |

From the transcript (275 events): the CLI loaded both project skills, all five roles and both
MCP servers without `--bare`; the commander read the topology, took S0, launched the three
investigators (L2 12, L3 7, policy 13 `run_show_command` calls, nothing else), the change
agent (3 show commands, one snapshot, one `apply_config` on r3), and the verifier (two
`wait_converged`, one `intent_check`, one `route_diff`, no twinlab tool at all); the verifier's
prompt contained exactly S0, S1, the policy path and the symptom. 53 tool calls, 5 subagents,
about 375 k cache-read, 59 k cache-write and 4 k output tokens, which the CLI prices at
$0.97 had it been paid per token; the export was auto-approved in bench mode. The main thread
also made four `ToolSearch` calls, the headless CLI's way of loading deferred tool schemas.

### Prompt revision 0: what the first batch taught

The first headless batch (row `claude-diagnose-sonnet-p0`, scenarios 001 to 008, Sonnet 5,
team skill, verifier on) is kept as recorded, with two harness repairs applied afterwards and
noted here: the parser had kept the CLI's first-turn text instead of the final report (with
background subagents the result event's text is not the last message), and a report whose
layer said `bgp` failed validation; both hid a correct diagnosis on 007. The rows were
relabelled `-p0` and `restored_golden` was filled in from each run's verification snapshot.

| Config | Runs | Root cause | Fix correct | Verified | No collateral | Minimal | Errors | Mean s | Golden |
|---|---|---|---|---|---|---|---|---|---|
| claude-diagnose-sonnet-p0 | 8 | 75% | 88% | 75% | 88% | 75% | 0 | 861 | 83% |

- 001 to 005: every axis, 165 to 335 s each, golden state restored.
- 006: service restored by a different design. The agent added a prefix-list entry so the
  server subnet is redistributed, instead of putting back the removed `network` statement:
  fix correct, verified, one op, but not the planted cause and not the golden state. That
  is why `restored_golden` now exists; op counting cannot tell a work-around from a
  restoration.
- 007: right diagnosis and right fix, then the verifier compared routes between S0 and S1,
  two instants while BGP was still converging, saw the leaked prefixes appear on the ISP,
  and failed a run whose intent check had passed. The commander obeyed, retried twice and
  gave up with the fault still planted, 1400 s. The verifier prompt is revised (p1): the
  verdict follows `intent_check`, `route_diff` is taken against the converged snapshot from
  that check, and a diff never vetoes a passing check.
- 008: correct fix (the harness snapshot equalled golden), then the verifier's model call
  stalled after its intent check and the CLI hung. The runner's timeout could not fire
  because cancelling the pipe reads on Windows waits for every child process to close the
  pipe. Recorded as a timeout after 4005 s. Fixed: a watchdog kills the whole process tree
  at the deadline.
- One more guard from the same batch: stopping the batch mid-run left a fault planted, and
  the next baseline would have adopted it as golden. The harness now refuses a baseline
  whose snapshot differs from the golden id the matrix's earlier rows were scored against.
