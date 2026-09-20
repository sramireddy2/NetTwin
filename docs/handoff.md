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

Merged to `main` (PRs #1 #2 #3 #4 #5 #7 #8; #6 was auto-closed by GitHub when its stacked base
branch was deleted, so it was re-opened as #7):

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
- M7 claude agents (branch `feat/claude-agents`, PR #9 open): `.claude/agents/` role files
  (l2/l3/policy investigators with `run_show_command` only, change-agent without export,
  verifier scoped to netverify), `.claude/skills/diagnose` and `diagnose-solo`,
  `netbench.roles` (frontmatter parser, reused by M10), `ManualRunner` + `netbench run
  --runner manual` to score an interactive run through the new `/admin/exports/{id}` route,
  `docs/diagnose.md` runbook. Manual runner proven live with a scripted stand-in (001, RVC).

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
- `claude` CLI is not installed (needed for M9: `npm install -g @anthropic-ai/claude-code`).
- Ollama 0.34.1 on Windows with `qwen2.5-coder:7b` (tools capable); CPU only (Intel Arc iGPU,
  32 GB RAM). Pull `qwen3:14b` before M10.
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
- This desktop session (opened in the OneDrive folder, moved with change_directory) never
  loaded the project `.mcp.json` servers, so `/diagnose` could not be exercised from it.
  Sessions opened in the repo do load them.

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

- M7 remaining: one real interactive run. Open Claude Code in the repo (a session opened
  elsewhere does not load `.mcp.json`; `/mcp` must list twinlab and netverify), start the
  servers WITHOUT NETTWIN_BENCH (`make -C lab serve`) so the export approval is an
  elicitation dialog, run `uv run netbench run --runner manual --scenarios 001 --matrix
  interactive` in another window, then `/diagnose <symptom>` in Claude Code, approve, check
  that the verifier subagent transcript holds only S0/S1/policy/symptom, record the score in
  lab-notes, tune the role prompts if needed, merge the PR.
- M8 tier B/C scenarios 015–020 (VLAN access/trunk/subinterface tag; nft ACL order, NAT,
  proto-89 filter) plus two-fault and no-fault control.
- M9 `runners/claude_cli.py`: `claude -p "/diagnose <symptom>" --output-format stream-json
  --verbose --model <m> --mcp-config .mcp.json --strict-mcp-config --allowedTools
  "mcp__twinlab__*,mcp__netverify__*,Agent" --max-turns N`; matrix v1 on Sonnet 5 in batches.
- M10 local loop on Ollama reading the same `.claude/agents/*.md` role files.
- M11 README results, recording, limitations.
