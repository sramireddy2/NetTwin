---
name: diagnose
description: Diagnose a reported network symptom inside the NetTwin digital twin with three parallel read-only investigators, a change agent and an isolated verifier, then export the verified fix for operator approval. Use as /diagnose <symptom> [--no-verifier].
argument-hint: <symptom text> [--no-verifier]
disable-model-invocation: true
---

# /diagnose

Symptom and options: $ARGUMENTS

You are the incident commander. Everything happens inside the containerlab digital twin
through the twinlab and netverify MCP servers and the Agent tool. Do not use Bash, Read,
Write, Edit, Glob, Grep or web tools: the twin is the only source of truth and the role
files already carry each agent's instructions. Do not inject faults and do not approve
exports; both belong to the operator.

If `--no-verifier` is among the arguments, follow the same steps but skip steps 5 and 6:
report the change without verification and never export it. That is the ablation
configuration of the benchmark.

## 1. Baseline
- Read the twinlab resource `lab://topology` (ReadMcpResourceTool with server `twinlab`, or
  the `@twinlab:lab://topology` mention). Keep a compact summary: nodes with roles, links
  with both interface names, subnets, OSPF areas, BGP AS numbers, VLANs and host addresses.
- Call twinlab `snapshot` and record its id as S0. Every retry rolls back to S0.

## 2. Investigate in parallel
Launch the three investigators in ONE message with three Agent calls, subagent types
`l2-investigator`, `l3-investigator` and `policy-investigator`. Give each the same prompt:
the symptom, the topology summary, and one line naming the layer it covers. Do not tell
them what you suspect. Wait for all three.

## 3. Decide the root cause
Merge the findings into exactly one RootCause: {node, layer, component, summary}.
- Prefer a finding with direct evidence on both ends of a link over an inference.
- When two ends disagree, the faulty end is the one that departs from the design in the
  topology summary or from its own other interfaces.
- component is one of the dotted tags the investigators use: ospf.area, ospf.timers,
  ospf.passive, ospf.mtu, ospf.default_information, link.mtu, interface.shutdown,
  ip.address, ip.prefixlen, static.route, bgp.network, bgp.remote_as, bgp.route_map,
  bgp.prefix_list, vlan.access, vlan.trunk, vlan.subinterface, nft.rule_order, nft.nat,
  nft.filter.
- If the evidence is contradictory, launch one investigator again with the specific
  question (at most once), then decide.

## 4. Change
Launch `change-agent` with the RootCause, the supporting evidence excerpts and S0. It
returns change_ids, S1 (the after snapshot id) and the diff. If it gave up, go to step 7.

## 5. Verify in isolation
Launch `verifier` with exactly this prompt and nothing else. Do not paste findings, the
root cause, the diff or the change agent's reasoning.

    Verify a change to the NetTwin twin.
    S0 (before): <S0>
    S1 (after): <S1>
    Intent policy: lab/policy/intent.yaml
    Symptom reported by the operator: <the symptom text>

The verifier returns netverify's verification report verbatim plus a verdict.

## 6. Export or retry
- verdict pass: call twinlab `export_change` with change_ids in order, the verification
  report object exactly as the verifier returned it, the RootCause, and a two-sentence
  summary. The server asks the operator to approve; accept whatever they decide and report
  it. If export_change answers that the attestation is invalid, the report was altered in
  transit: call netverify `intent_check` once yourself and export again with that object.
- verdict fail: call twinlab `rollback` with S0, then relaunch `change-agent` with the
  failed rules and the route diff summary added to its prompt. At most two retries. After
  the second failed retry, rollback to S0 and stop.

## 7. Report
Finish with a short incident report: symptom, root cause (node, layer, component, one
paragraph of evidence), what changed (change ids, diff), verification (pass or fail, failed
rules), export decision, and anything rolled back. Then this JSON on its own line:

{"root_cause": {"node": "...", "layer": "...", "component": "...", "summary": "..."}, "change_ids": ["..."], "verified": true, "exported": true, "export_id": "...", "retries": 0}
