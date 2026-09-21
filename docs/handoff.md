---
name: nettwin-handoff
description: Handoff for the NetTwin build as of 2026-09-20 — what is merged (M0-M6), what is in PR (M7), environment facts, decisions, how to run, and the next milestones
metadata:
  type: project
---

# NetTwin handoff (2026-09-20)

Repo: `C:\dev\NetTwin` (Windows), remote https://github.com/sramireddy2/NetTwin (public). Plan
file: `~/.claude/plans/recommended-combination-claude-code-lexical-galaxy.md`; condensed roadmap in
`docs/roadmap.md`; design in `docs/design.md`; lab evidence and lessons in `docs/lab-notes.md`.

## Where the build stands

Merged to `main` (PRs #1 #2 #3 #4 #5 #7 #8 #9; #6 was auto-closed by GitHub when its stacked
base branch was deleted, so it was re-opened as #7):

- M0 scaffold: uv workspace (`nettwin` CLI + `packages/{nettwin_core,twinlab,netverify,netbench}`),
  CI (ruff + `pytest -m "not lab"`), `nettwin doctor`, `scripts/wsl.ps1`.
- M1 lab: 10-node containerlab topology (r1–r4, isp on FRR 10.2.1 image; sw1 VLAN bridge;
  h10/h20/srv/inet Alpine), OSPF area 0/1 with 1s/3s timers, eBGP r4–isp with default-originate,
  guest NAT + guest filter in nftables. `make up` 34 s warm, `make golden` ~10 s, all checks pass.
- M2 twinlab read path: `run_show_command` (shlex + allowlist, argv only), `snapshot`/`rollback`
  (content-addressed, routes and bridge timers excluded from the id), `lab://topology`.
- M3 twinlab write path: typed `apply_config` (auto snapshot, FRR `%` error detection, auto
  rollback), admin routes `/admin/{status,scenarios,inject/{id},golden}` behind a bearer token,
  14 tier-A scenarios in `lab/scenarios/` each with inject, ground_truth, expected_fix,
  expected_failed_rules and a probe.
- M4 netverify: `ReadOnlyExecutor` (refuses non-allowlisted argv and stdin), `wait_converged`,
  `reachability_matrix` (ICMP, DF-bit, nc), `route_diff`, `intent_check` with rules reach /
  path_mtu / ospf_full / bgp_established / no_route_leak / no_spof, HMAC attestation. Shared
  allowlist, snapshot capture and attest helpers live in `nettwin_core`.
- M5 export gate: `export_change(change_ids, verification, root_cause, summary)` verifies the
  HMAC and that it covers the state after the last change and the policy in force, then asks the
  operator via MCP elicitation; `NETTWIN_BENCH=1` auto-approves; no-elicitation clients leave it
  pending for `nettwin approve <id>` (`/admin/approve/{id}`). Prompts `diagnose`, `propose-change`.
- M6 netbench: `ToolClient` over in-memory or streamable-HTTP MCP sessions, `HttpAdmin` /
  `CallableAdmin` for the side door, `Harness` (baseline, reset, inject, run, score, one JSONL
  record per run, resumable by run id), `FakeAgentRunner` replaying `expected_fix`, structured
  scoring (root cause node+component, verified, collateral vs golden matrix, minimal), markdown
  report, `netbench run|report` CLI, twinlab `get_change` tool, `make serve-bench`.
- M7 claude agents (PR #9, merged): `.claude/agents/` role files
  (l2/l3/policy investigators with `run_show_command` only, change-agent without export,
  verifier scoped to netverify), `.claude/skills/diagnose` and `diagnose-solo`,
  `netbench.roles` (frontmatter parser, reused by M10), `ManualRunner` + `netbench run
  --runner manual` to score an interactive run through the new `/admin/exports/{id}` route,
  `docs/diagnose.md` runbook. Manual runner proven live with a scripted stand-in (001, RVC),
  then the real team closed scenario 001 interactively: RVC, minimal, 4 min 4 s from S0 to
  export, verifier isolated; Claude Code desktop did not surface the elicitation, so the
  export went through the pending + `nettwin approve` path (`results/interactive/runs.jsonl`).
- M8 scenarios B/C (branch `feat/bench-scenarios-bc`): 015-017 VLAN faults (sw1 access port,
  sw1 trunk, r3 sub-interface tag), 018-020 nftables faults (rule order, NAT, OSPF input
  filter), 021 two-fault stretch (MTU + BGP network, `extra_causes`), 022 no-fault control
  (`ground_truth: null`). `NftRule` delete accepts rule text and twinlab resolves the handle on
  the node; r3 golden nft gained an empty `input` chain; scoring handles multi-cause and
  control; `tests/unit/test_scenarios_bc.py`. Live: all 8 inject/probe/rollback byte-identical
  and fail exactly their rules; fake agent extended `results/v0` to 22 scenarios.
- M9 headless runner (branch `feat/bench-claude-runner`): `netbench.claude_cli.ClaudeCliRunner`
  runs `claude -p` (prompt on stdin, stream-json, `--mcp-config .mcp.json --strict-mcp-config
  --allowedTools mcp__twinlab,mcp__netverify,Agent,ReadMcpResourceTool,ListMcpResourcesTool
  --max-turns N`, cwd = repo), saves the stream under `results/<matrix>/transcripts/`, parses
  tool calls per server, subagents, turns, tokens, cost, and reads the export bundle for the
  scorer; `netbench run --runner claude --model <m> --skill diagnose|diagnose-solo
  [--no-verifier]`. Scoring gained harness-side `fix_correct` (the harness runs its own
  intent_check after every run) so the verifier-off ablation is comparable; report has a
  "Fix correct" column and RFVC marks. Fixtures in `tests/fixtures/stream/`. Merged as PR #11.
  First batch (`claude-diagnose-sonnet-p0`, 001-008) recorded in `results/v1` and analysed in
  lab-notes: verifier prompt revised to p1 (verdict follows intent_check, route_diff against
  the converged snapshot), `restored_golden` score added, watchdog tree-kill on timeout,
  baseline guard against a planted twin. p1 batch (row `claude-diagnose-sonnet`) reached 009
  before WSL wedged; 001-005, 008, 009 clean with golden restored, 006 work-around again,
  007 lost to a CLI hang mid-fix (RunTimeout), 010 lost to the WSL wedge (no row). Resume with
  the same command (resumable by run id) after `nettwin lab up` + `make -C lab serve-bench`;
  then `--no-verifier`, then `--skill diagnose-solo` with and without verifier. Timeouts are
  now recorded as errors; `no ipv6 forwarding` is pinned in the golden FRR configs.

Live results recorded in `docs/lab-notes.md`: 14 scenarios inject/probe/rollback byte-identical
(4 m 13 s); verifier fails exactly each scenario's expected rules, golden passes with attestation
(15 tests, 10 m 14 s); inject → fix → verify → export bundle end to end (48 s); fake agent
100 % on all four scores over the 14 tier-A scenarios, mean 22 s per run, 13 min 18 s wall
clock (M6).

## How to run the benchmark today

Live test inside WSL (about 13 min, all 14 scenarios must score root_cause/verified/
collateral_free/minimal): `NETTWIN_LAB=1 uv run pytest tests/lab/test_fake_agent.py -q`.
From Windows against the live servers: `make -C lab serve-bench` inside WSL, then
`uv run netbench run --runner fake --matrix v0` and `uv run netbench report --matrix v0`;
results land in `results/<matrix>/runs.jsonl` and a re-run skips run ids already recorded.
`results/**/transcripts/` is git-ignored, the JSONL is not.

## Environment facts (not derivable from the repo)

- WSL distro `Containerlab` (user `clab`, passwordless sudo, Debian 12, native Docker 27.5,
  containerlab 0.79, rsync, python3, pipx, uv at `~/.local/bin`, venv `~/.venvs/nettwin`, state
  `~/.nettwin/` with `admin.token`, `attest.key`, `snapshots/`, `changes/`, `exports/`, `logs/`).
- WSL2 idles out about a minute after the last Windows session closes and Docker restarts the
  containers WITHOUT their containerlab links. Keep `wsl -d Containerlab -- sleep infinity` (or
  `nettwin serve`) running while working; `nettwin lab up` redeploys in ~35 s if the lab is stale.
- Servers are started with `make -C lab serve` (both) or `serve-bench`; stop with `make stop-serve`
  (pattern is `[t]winlab-server` so it cannot match its own shell; never `pkill -f twinlab-server`
  from a shell whose command line contains that string).
- Pass scripts into WSL as `wsl.exe -d Containerlab -- bash -s <<'EOF' ... EOF`; inline
  `bash -lc '...'` with `$1`-style variables gets mangled.
- `gh` 2.101 is at `C:\Program Files\GitHub CLI\gh.exe`, authenticated as sramireddy2, not on the
  Bash tool PATH: `export PATH="$PATH:/c/Program Files/GitHub CLI"`. The desktop app's PR pane
  refuses to bind this repo; use `gh pr checks N --watch`.
- `claude` CLI 2.1.278 is installed at `C:\Users\shank\AppData\Roaming\npm\claude.cmd`; that
  folder is not on the Bash tool PATH (add `/c/Users/shank/AppData/Roaming/npm`) for M9.
- Ollama 0.34.1 on Windows with `qwen2.5-coder:7b` and `qwen3:14b` pulled; CPU only (Intel
  Arc iGPU, 32 GB RAM).
- Python 3.13 + uv 0.12 on Windows; `mcp` SDK is 2.2.0: `mcp.server.mcpserver.MCPServer`,
  `ToolError` in `mcp.server.mcpserver.exceptions`, `run("streamable-http", host=, port=,
  transport_security=)`, low-level server `._lowlevel_server`, client
  `mcp.client.streamable_http.streamable_http_client(url)`, in-memory testing via
  `mcp.shared.memory.create_client_server_memory_streams` + `ClientSession` (see
  `packages/netbench/src/netbench/clients.py`). Never write 1.x FastMCP code.
- pytest needs unique test basenames across `tests/unit` and `tests/lab` (no `__init__.py`);
  `pythonpath = ["."]` lets tests import `tests.unit.test_netverify.make_fake`.

- Open new chats in `C:\dev\NetTwin`: the memory directory is keyed by the working folder, and
  the old OneDrive folder has its own stale memory. `mcp__ccd_directory__change_directory`
  moves a session that opened elsewhere.
- Git Bash rewrites arguments that start with `/` (MSYS path conversion), so
  `wsl.exe -- bash /mnt/c/x.sh` becomes `C:/Program Files/Git/mnt/c/...`. Feed scripts over
  stdin (`bash -s < file`, or the heredoc form) or set `MSYS_NO_PATHCONV=1`.
- Long lab tests: `pytest -q` prints nothing until the end. Launch them detached
  (`nohup uv run pytest ... > ~/.nettwin/logs/x.log &`) and watch the log; a Bash tool call
  cannot block that long.
- A Claude Code session must be opened in the repo for `.mcp.json` to load; if the servers are
  down at session start the user reconnects them with `/mcp` (the reconnect tool only handles
  claude.ai connectors). While a session holds MCP connections, `make stop-serve` hangs on
  uvicorn shutdown: wait 10 s then `pkill -9 -f '[t]winlab-server'` (same for netverify).
- Lab config files are bind-mounted into the containers; after editing anything under
  `lab/configs/` run `nettwin lab up` (redeploy), not just `make sync` + `make golden`, or the
  container keeps the old inode.
- OSPF probes: when only one side stops receiving hellos, the other side keeps the neighbour
  in Init, so probe `show ip ospf neighbor ethN` with `absent: "Full"` rather than the
  neighbour id.
- Headless runs: `claude -p` hangs when a subagent's call stalls or when WSL (and with it the
  MCP servers) goes away; the runner kills the process tree at `--run-timeout` (default 25 min),
  waits 60 s for the pipes, records a RunTimeout error and moves on.
- WSL2 wedged twice on 2026-09-20 (HCS_E_CONNECTION_TIMEOUT at start, Wsl/Service/0x8007274c
  after ~2 h of headless runs): `wsl --shutdown`, then `nettwin lab up`, then serve-bench. Stopping a batch mid-run leaves a fault
  planted: run `nettwin lab golden` before the next batch (the harness refuses a baseline that
  differs from the matrix's golden id; `no ipv6 forwarding` is pinned in the golden FRR configs
  so `make golden` and `lab up` agree on that id, e4a6c253d114 as of 2026-09-20). Transcripts are named
  `<scenario>.<config>.<trial>.jsonl` under `results/<matrix>/transcripts/`.

## Decisions the user confirmed

Zero API spend; Claude Code subscription is the agent runtime (subagents, `/diagnose` skill,
headless `claude -p` for the benchmark); Ollama is the unlimited local axis; no Agent SDK. Repo
moved from OneDrive to `C:\dev\NetTwin`. Server named `twinlab` not `netlab`. I open PRs and
squash-merge them myself once CI is green. Fault injection is an admin route, never an MCP tool.
Matrix cut to about 60 runs (20 scenarios × verifier on/off, single vs multi-agent, plus local).

## Lab lessons (details in docs/lab-notes.md)

Containerlab management default route shadows protocol defaults in FRR (setup.sh deletes it);
stock FRR image lacks `/etc/frr/vtysh.conf`; FRR needs `no ip ospf area X` before a new area;
hosts cache path MTU for ten minutes after ICMP fragmentation-needed, so `apply`, `rollback` and
golden scripts flush route caches; snapshot ids exclude routes (derived) and bridge STP timers.

## Next milestones

- M7 and M8 are merged. The desktop app did not present MCP elicitation, so approvals go
  through `nettwin approve <id>`; the harness's manual runner waits 3 min for that decision
  and scores a pending bundle as exported.
- M8 tier B/C scenarios 015–020 (VLAN access/trunk/subinterface tag; nft ACL order, NAT,
  proto-89 filter) plus two-fault and no-fault control.
- M9 `runners/claude_cli.py`: `claude -p "/diagnose <symptom>" --output-format stream-json
  --verbose --model <m> --mcp-config .mcp.json --strict-mcp-config --allowedTools
  "mcp__twinlab__*,mcp__netverify__*,Agent" --max-turns N`; matrix v1 on Sonnet 5 in batches.
- M10 local loop on Ollama reading the same `.claude/agents/*.md` role files.
- M11 README results, recording, limitations.
