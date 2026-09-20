# Running /diagnose interactively

Milestone M7 wires the agent team into Claude Code. The roles live in `.claude/agents/`, the
two workflows in `.claude/skills/`, and both MCP servers in `.mcp.json`. Nothing here needs
an API key: Claude Code runs the agents on the subscription, the twin runs in WSL.

## The team

| Role | File | Tools | Sees |
|---|---|---|---|
| L2, L3, policy investigators (parallel) | `l2-investigator.md`, `l3-investigator.md`, `policy-investigator.md` | twinlab `run_show_command` only | symptom, topology summary |
| change agent | `change-agent.md` | twinlab read, `snapshot`, `apply_config`, `rollback`, `get_change`; `export_change` denied | root cause and evidence, S0, on retry the failed rules |
| verifier | `verifier.md` | netverify only (`mcpServers: [netverify]`) | S0, S1, the policy path, the symptom, nothing else |
| commander | the `/diagnose` skill in the main thread | twinlab, netverify, Agent | everything |

All roles are `model: inherit`, so the model chosen for the session is the single Claude
axis of the benchmark. None of them has Bash, Read, Write, Edit, Glob or Grep.
`tests/unit/test_roles.py` checks the allowlists against the live tool lists of both servers,
so a renamed tool fails CI rather than silently widening a role.

`/diagnose-solo` does the same job in one context with no subagents; it is the single-agent
baseline. Both skills accept `--no-verifier`, which skips verification and never exports.

## Run one incident

1. Lab and servers, inside WSL (or from Windows with `uv run nettwin lab up` and
   `uv run nettwin serve`). Start the servers **without** `NETTWIN_BENCH` so the export
   approval is a real elicitation dialog in Claude Code:

   ```bash
   make -C lab up
   make -C lab serve
   ```

2. Open Claude Code in `C:\dev\NetTwin`. `/mcp` should list `twinlab` and `netverify` as
   connected. The project settings already allow both servers' tools.

3. Plant a fault. Either from the harness, which also scores the run afterwards (window A):

   ```bash
   uv run netbench run --runner manual --scenarios 001 --matrix interactive --timeout 1800
   ```

   It rolls the twin back to golden, injects the scenario, prints the symptom and waits for
   an export bundle. Or, without scoring:

   ```bash
   uv run nettwin lab inject 001-ospf-area-mismatch
   ```

4. In Claude Code (window B), paste the symptom:

   ```
   /diagnose NOC ticket: monitoring shows r1 lost its OSPF neighbour on eth2 ...
   ```

   The commander snapshots, launches the three investigators in parallel, picks a root
   cause, hands it to the change agent, then launches the verifier with only the two
   snapshot ids. When the verifier passes, `export_change` asks for approval. A client that
   supports MCP elicitation shows a dialog; the Claude Code desktop app did not surface one
   in our runs, in which case twinlab leaves the bundle pending and tells the agent so.
   Either way the decision is yours: answer the dialog, or run

   ```bash
   uv run nettwin approve <export_id>
   ```

   Declining, or never approving, leaves nothing exported.

5. The harness in window A notices the bundle, waits up to three minutes for your decision
   (a pending bundle still counts as exported for the score), scores the run and rolls the
   twin back to golden. Then:

   ```bash
   uv run netbench report --matrix interactive
   ```

Approved bundles are written under `~/.nettwin/exports/<export_id>/` in WSL
(`diff.patch`, `root_cause.md`, `verification.json`). `uv run nettwin exports` lists them;
`uv run nettwin approve <id>` decides a bundle left pending by a client without elicitation.

## Checking isolation after a run

Open the verifier subagent's transcript in Claude Code. It must contain only the verifier
prompt (S0, S1, policy path, symptom) and netverify tool calls. If the commander leaked the
root cause or the diff into it, the skill text in `.claude/skills/diagnose/SKILL.md` is the
thing to fix.
