# Demo: one incident, with and without the verifier

Two real headless runs of the same planted fault, replayed from their stream-json transcripts
with `netbench timeline`. The fault is scenario 019: the masquerade rule for the guest VLAN
was removed from r4's nftables NAT table. The symptom the team was given:

> NOC ticket: guest users on VLAN 20 cannot reach the internet although they reach their gateway and the corporate side of the network. Corporate users reach the internet normally and the ISP reports no change on their side.

Both runs use the same model (Sonnet 5), the same `/diagnose` skill, the same three
investigators and change agent. The only difference is `--no-verifier`: without it, an
isolated verifier judges the change with netverify before `export_change` can be called.

### With the verifier (row claude-diagnose-sonnet)

| Time | Agent | Step |
|---|---|---|
| +0:03 | commander | reads `lab://topology` |
| +0:03 | commander | snapshot `ad3afc9b2ed3` |
| +0:16 | commander | launches **l2-investigator**: L2 investigation of guest VLAN outage |
| +0:19 | l2-investigator | 2 show commands on sw1 |
| +0:20 | commander | launches **l3-investigator**: L3 investigation of guest VLAN outage |
| +0:20 | l2-investigator | 4 show commands on r3, h10, h20 |
| +0:24 | commander | launches **policy-investigator**: Policy investigation of guest VLAN outage |
| +0:27 | l2-investigator | 6 show commands on sw1, r3, h20, h10 |
| +0:32 | l3-investigator | 6 show commands on r1, r2, r3, r4 |
| +0:35 | policy-investigator | 4 show commands on r4, r3, r2, r1 |
| +0:40 | l3-investigator | 9 show commands on r3, r1, r4, isp |
| +0:54 | policy-investigator | 5 show commands on r4, isp |
| +2:20 | commander | launches **change-agent**: Apply NAT fix for guest VLAN egress |
| +2:28 | change-agent | 2 show commands on r4 |
| +2:33 | change-agent | apply_config on r4: `nft_rule add ip nat postrouting ip saddr 10.0.20.0/24 oifname "eth3" masquerade` -> refused: Error executing tool apply_config: 1 validation error for... |
| +2:36 | change-agent | apply_config on r4: `nft_rule add ip nat postrouting ip saddr 10.0.20.0/24 oifname eth3 masquerade` -> change `cb2bcc6fad653561` |
| +2:42 | change-agent | show on r4: `nft -a list ruleset` |
| +2:54 | commander | launches **verifier**: Verify NAT fix in isolation |
| +2:57 | verifier | wait_converged: converged |
| +3:02 | verifier | intent_check: **passed** 20/20 rules |
| +3:15 | verifier | route_diff S0 to S1: no route changes |
| +4:03 | commander | export_change -> approved (bench-auto) |

Reported root cause: **r4 nft.nat**. r4's nat postrouting chain had no masquerade/SNAT rule for guest VLAN 20 (10.0.20.0/24), which is intentionally not BGP-advertised, so guest packets left r4 toward the ISP with an unroutable source address and return traffic was black-holed.

Wall clock 4 min 14 s.

The investigators read in parallel for about two minutes; the commander hands the change
agent one root cause; the change agent's first `apply_config` is refused by twinlab's
typed-op validation and its second applies; the verifier sees only S0, S1, the intent
policy and the symptom, finds every rule passing and no route changes, and only then does
the commander export.

### Without the verifier (row claude-diagnose-sonnet-noverify)

| Time | Agent | Step |
|---|---|---|
| +0:02 | commander | reads `lab://topology` |
| +0:02 | commander | snapshot `ad3afc9b2ed3` |
| +0:16 | commander | launches **l2-investigator**: L2 investigation of guest VLAN issue |
| +0:18 | l2-investigator | 4 show commands on sw1, r3 |
| +0:22 | commander | launches **l3-investigator**: L3 investigation of guest VLAN issue |
| +0:23 | l2-investigator | 4 show commands on r3, h10, h20 |
| +0:27 | commander | launches **policy-investigator**: Policy investigation of guest VLAN issue |
| +0:29 | l3-investigator | show on r3: `show ip route` |
| +0:30 | policy-investigator | show on r4: `nft -a list ruleset` |
| +0:30 | l3-investigator | show on r3: `show running-config` |
| +0:30 | policy-investigator | show on r3: `nft -a list ruleset` |
| +0:30 | l3-investigator | 2 show commands on r1, r4 |
| +0:35 | l2-investigator | 2 show commands on sw1, h20 |
| +0:36 | l3-investigator | show on r4: `show bgp ipv4 unicast neighbors 203.0.113.2 adv...` |
| +0:36 | l2-investigator | show on r3: `ip neigh` |
| +0:36 | l3-investigator | 2 show commands on isp, r4 |
| +0:40 | policy-investigator | 2 show commands on r4 |
| +0:41 | l2-investigator | 2 show commands on r1, r4 |
| +0:43 | l3-investigator | 2 show commands on r4, r3 |
| +0:50 | policy-investigator | 5 show commands on r4, isp |
| +2:00 | commander | launches **change-agent**: Apply fix for guest VLAN BGP prefix-list |
| +2:02 | change-agent | show on r4: `show running-config` |
| +2:05 | change-agent | apply_config on r4: `ip prefix-list CORP seq 20 permit 10.0.20.0/24` -> change `bccb13fb6238c9e1` |
| +2:10 | change-agent | 4 show commands on r4, isp |

Reported root cause: **r4 bgp.prefix_list**. prefix-list CORP (matched by route-map REDIST-OSPF applied to 'redistribute ospf' in r4's BGP address-family) only permitted 10.0.10.0/24, so the valid OSPF-learned guest subnet 10.0.20.0/24 was never redistributed into eBGP and never advertised to the ISP, leaving the ISP with no return route fo...

Wall clock 2 min 32 s.

The same investigators, the same evidence, a different conclusion: the commander decides
the guest subnet is missing from the prefix-list that feeds BGP, and the change agent adds
it. That advertises 10.0.20.0/24 to the ISP, which is exactly the route leak the intent
policy forbids (`no_route_leak`), and the planted NAT fault is still there. Nothing in this
configuration checks the change, so the run ends with a confident report. The harness's own
post-run intent check scored it: root cause wrong, fix wrong, collateral on the ISP.

Regenerate either table from a local transcript:

```bash
uv run netbench timeline results/v1/transcripts/019-nft-nat-missing.claude-diagnose-sonnet.1.jsonl
```

Transcripts are not committed (they are large and contain full tool output); the scored
records for both runs are in `results/v1/runs.jsonl`.
