# NetTwin

An agentic network troubleshooter that never touches production.

Config changes cause a large share of network outages: a bad ACL, a fat-fingered OSPF area, an MTU mismatch. Engineers cannot safely try fixes on production, and nobody lets an LLM agent loose on real routers. NetTwin puts the agent inside a **digital twin** instead: a containerlab replica of the network running real FRRouting routers. The agent diagnoses the fault there, tests the fix there, and an independent verifier checks the result. Only a verified, human-approved config diff comes out the other end.

```mermaid
flowchart TB
  SYM([Symptom]) --> IC
  subgraph HOST["Agent runtime (Claude Code subagents)"]
    IC[Incident commander]
    L2[L2 agent]
    L3[L3 agent]
    POL[Policy agent]
    CA[Change agent]
    VER[Verifier<br/>fresh context, state only]
    IC --> L2 & L3 & POL
    L2 & L3 & POL --> CA
  end
  subgraph MCP["MCP boundary (typed tools, no shell)"]
    NL[twinlab<br/>run_show_command, apply_config,<br/>snapshot, rollback,<br/>lab://topology, export_change]
    NV[netverify<br/>reachability_matrix, route_diff,<br/>intent_check, signed attestation]
    SS[(snapshot store<br/>content-addressed)]
  end
  subgraph TWIN["Digital twin (containerlab + FRR, WSL2)"]
    FRR[FRR routers<br/>OSPF, BGP, nftables]
    SW[VLAN switch<br/>Linux bridge]
    H[Alpine hosts]
  end
  L2 & L3 & POL -->|reads| NL
  CA -->|reads + writes| NL
  VER --> NV
  NL --> SS
  NV --> SS
  NL --> TWIN
  NV --> TWIN
  NL -.->|elicitation| OP([Operator])
  OP --> EXP[Export bundle<br/>diff, root cause, evidence]
  NB[NetBench harness] -->|admin route| NL
  NB --> HOST
```

## What it shows

- **Networking.** OSPF, BGP, VLANs, MTU, NAT and packet filters on real routing software (FRRouting on Linux), not a simulation.
- **MCP as a safety boundary.** Two MCP servers with separate responsibilities. `twinlab` owns the twin and every mutation through typed, validated tools. `netverify` owns judgement and has no write tools at all. The human approval gate is an MCP elicitation raised by the server, so the agent cannot talk its way past it. Fault injection lives on an admin route the agent cannot reach.
- **Agents, measured.** A commander, three parallel layer investigators, a change agent, and a verifier that runs in a fresh context and only ever sees before and after state plus the intent policy. NetBench plants about 20 faults and scores root cause, verified fix, and collateral damage across single vs multi-agent, verifier on vs off, and across models.

## Status

Design complete, build in progress. See [docs/design.md](docs/design.md) for the architecture, parts, contracts, scenarios, and tradeoffs, and [docs/roadmap.md](docs/roadmap.md) for the milestone plan.

## Layout

```
lab/          containerlab topology, golden FRR configs, intent policy, fault scenarios
packages/     nettwin_core (contracts), twinlab (twin control MCP server),
              netverify (verification MCP server), netbench (harness and scoring)
.claude/      agent roles and the /diagnose skill for Claude Code
docs/         design, roadmap, lab notes
```

## License

MIT
