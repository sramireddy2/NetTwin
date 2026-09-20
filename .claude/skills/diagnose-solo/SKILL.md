---
name: diagnose-solo
description: Single-agent baseline of /diagnose. Diagnose a reported network symptom inside the NetTwin digital twin yourself with the twinlab and netverify tools, no subagents, then export the verified fix for operator approval. Use as /diagnose-solo <symptom> [--no-verifier].
argument-hint: <symptom text> [--no-verifier]
disable-model-invocation: true
---

# /diagnose-solo

Symptom and options: $ARGUMENTS

You diagnose and fix this alone: no Agent tool, no subagents. Use only the twinlab and
netverify MCP tools. Do not use Bash, Read, Write, Edit, Glob, Grep or web tools. Do not
inject faults and do not approve exports; both belong to the operator.

If `--no-verifier` is among the arguments, skip steps 4 and 5 and never export.

## 1. Baseline
Read the twinlab resource `lab://topology` (ReadMcpResourceTool with server `twinlab`) and
note nodes, links with both interface names, subnets, OSPF areas, BGP AS numbers, VLANs and
host addresses. Call `snapshot` and record its id as S0.

## 2. Investigate
Use `run_show_command` across the three layers; prefer filtered commands, output is
truncated when long, and compare both ends of a link before blaming it.
- L2: `ip -br link`, `ip -d link show eth1`, `ip -br addr`, `show interface eth1` (MTU,
  state, addresses on both ends), `bridge vlan show` and `ip -d link show eth3.10` for
  VLANs, `ip neigh` for a host that cannot reach its gateway.
- L3: `show ip ospf neighbor` everywhere; for a broken adjacency `show ip ospf interface
  eth1` on both ends (area, timers, passive, MTU) and `show running-config ospfd`;
  `show ip route`, `show ip route 10.0.40.0/24`, `show ip route static`; `show bgp summary`,
  `show bgp ipv4 unicast`, `show running-config bgpd` on r4 and isp; the default route on r1.
- Policy: `nft -a list ruleset` on the routers that carry policy (rule order, NAT for the
  guest subnet, the filter keeping guest away from srv); `show route-map`,
  `show ip prefix-list` and the routes isp learned (10.0.20.0/24 must never be there).

## 3. Decide the root cause
Exactly one RootCause: {node, layer, component, summary}. When two ends disagree, the faulty
end is the one that departs from the design or from its own other interfaces. component is a
dotted tag: ospf.area, ospf.timers, ospf.passive, ospf.mtu, ospf.default_information,
link.mtu, interface.shutdown, ip.address, ip.prefixlen, static.route, bgp.network,
bgp.remote_as, bgp.route_map, bgp.prefix_list, vlan.access, vlan.trunk, vlan.subinterface,
nft.rule_order, nft.nat, nft.filter; coin one in the same style only if nothing fits.

## 4. Change
Apply the smallest change that restores the design with `apply_config`: one node, typed
ops (frr_lines are FRR configure-mode lines in order; FRR needs `no ip ospf area X` before
a new area), nothing beyond what the root cause names. Read the returned diff; if it touched
more than intended, `rollback` to S0 and apply tighter ops. Record change_ids and S1, the
after snapshot id of the last change. Check the direct effect with one show command.

## 5. Verify and export
Call netverify `wait_converged` with timeout 60, then `intent_check`, then `route_diff` S0
S1. Routes that disappeared on nodes unrelated to the symptom are collateral even if every
rule passed.
- All rules pass and no collateral: call twinlab `export_change` with change_ids in order,
  the verification report object exactly as intent_check returned it, the RootCause and a
  two-sentence summary. The server asks the operator to approve; report their decision.
- Otherwise: `rollback` to S0, rethink with the failed rules, and try a different minimal
  change. At most two retries; then rollback to S0 and stop.

## 6. Report
Finish with a short incident report: symptom, root cause with evidence, what changed
(change ids, diff), verification result, export decision, anything rolled back. Then this
JSON on its own line:

{"root_cause": {"node": "...", "layer": "...", "component": "...", "summary": "..."}, "change_ids": ["..."], "verified": true, "exported": true, "export_id": "...", "retries": 0}
