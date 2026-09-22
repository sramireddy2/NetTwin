---
name: nettwin-handoff
description: Handoff for the NetTwin build as of 2026-09-21 — M0-M9 merged, v1 team rows done (verifier on/off), solo rows running/owed, M10 local runner in PR #15 awaiting Ollama smoke test, resume procedure, lessons
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
  baseline guard against a planted twin. The p1 team row (`claude-diagnose-sonnet`) is complete:
  22 runs, root cause 91 %, fix correct 95 %, verified 91 %, golden 82 %, 1 error (007 CLI
  hang), median 205 s; 006 and 021 use a prefix-list work-around for the missing BGP network,
  019 restores NAT with the clauses in another order (not golden by text). Still to run for v1:
  the same command with `--no-verifier` (row claude-diagnose-sonnet-noverify), then
  `--skill diagnose-solo` with and without `--no-verifier`. Each row is about 22 x 4 min plus
  hangs; runs are resumable by run id. Timeouts are recorded as errors; `no ipv6 forwarding`
  is pinned in the golden FRR configs; the scorer takes the first node-like token of a
  prose node name.

Live results recorded in `docs/lab-notes.md`: 14 scenarios inject/probe/rollback byte-identical
(4 m 13 s); verifier fails exactly each scenario's expected rules, golden passes with attestation
(15 tests, 10 m 14 s); inject → fix → verify → export bundle end to end (48 s); fake agent
100 % on all four scores over the 14 tier-A scenarios, mean 22 s per run, 13 min 18 s wall
clock (M6).

## Session 2026-09-21 handoff (v1 ablation rows in progress, M10 code in PR #15)

Main is the PR #25 squash (PRs #15-#25 all merged 2026-09-21/22; main tree on main, clean apart from untracked results/local of the running row). Remaining: local matrix runs, README local row, worktree removal, demo recording. Details:

- MATRIX v1 COMPLETE and MERGED: PR #16 squash-merged 2026-09-21 ~20:45 local, main = f0489d2, working tree on main and clean. (History: the branch `bench/v1-ablation-rows` is where commit 28fdbd0 held the
  verifier-off team row, plus the lab-notes and handoff docs; pushed to origin, no PR yet).
  The solo row started at 19:30 UTC on 2026-09-21 from the previous chat's background task (it
  dies with that chat; completed runs stay recorded, a planted fault needs `uv run nettwin lab
  golden` before the next batch, and the same command resumes by run id) and appends to results/v1/runs.jsonl in this working tree; commit it on this
  branch when it finishes, then run the last row, then push and open one results PR with
  the docs/lab-notes.md analysis (section "Ablation: the team without its verifier" is
  already written and uncommitted; docs/handoff.md mirrors this memory file).
- M11 part 1 DONE: README results section (v1 table, five findings, limitations) merged as PR #17, main = 28557a6. Remaining M11: local-model row in the README once the Ollama matrix exists, demo recording.
- M10 smoke tests done (2026-09-21 evening, qwen2.5-coder:7b, scenario 001): test 1 one turn (tool call as JSON text; `text_tool_calls` fallback added). test 2 ~8 turns/50 min, context hit the 16 k window, a >900 s call was mis-mapped to RunnerUnavailable (fixed: `ModelTimeout` = run failure, partial transcript saved, `--result-cap`). test 3 (`--max-turns 20 --result-cap 3000`): 20 turns, 932 s, 20 tool results, no protocol errors, EVERY tool call came as text (fallback essential), the 7b model only ran show commands and never applied a change (root cause False, fix False). Timing: first turn ~5 min (4.2 k prompt tokens on CPU), then ~25 s/turn, ~220 tokens/turn. The transcript success-path bug and a `text_calls` count are fixed and merged (PR #19, 2026-09-21 ~22:35 local); the running local row still uses the pre-fix code (imported at start), so its transcripts' hit_max_turns field is unreliable for row 1; the notes field is right. The unregistered worktree directory `.claude/worktrees/agent-a3c23ca2a6eac06f3` is gitignored but could not be deleted (busy); `rm -rf` it in a later session.
- PR #15 (M10 local runner) MERGED as 287e517; worktree removed (directory left behind, gitignored). LOCAL ROW 1 attempt 1 (qwen3:14b, 22:26 local): scenario 001 ran 767 s and 2 turns: a structured read_topology call, then an EMPTY assistant message that the loop took as final. Stopped the row, `nettwin lab golden`, deleted results/local. Fix: ToolLoop nudges an empty answer (any loop) or a top-level final answer without the root_cause JSON line, at most twice (`MAX_NUDGES`, `NUDGE`), counted as `nudges=` in notes/transcript; merged as PR #20 (main ff96591, 2026-09-21 ~23:00 local; main tree on main, clean apart from the untracked results/local of the running row). LOCAL ROW 1 attempt 2 relaunched ~22:55 local from the main tree with the nudge code: `uv run netbench run --runner local --model qwen3:14b --skill diagnose-solo --scenarios 001,...,014 --matrix local --max-turns 20 --result-cap 3000` (results/local/runs.jsonl, row `local-diagnose-solo-qwen3-14b`, ~15-25 min per scenario, resumable by run id, never while a Claude batch runs). Then the same with `--model qwen2.5-coder:7b`, then the README local row (M11).
- HARNESS HANG ROOT CAUSE (2026-09-21 ~23:30): the harness's MCP sessions idle during a run; the streamable-HTTP GET event stream drops after ~10-11 min idle (`GET stream disconnected, reconnecting in 1000ms`), and the next tools/call can block far past read_timeout_seconds (26 min observed before the retry guard fired, then the retry succeeded). Fixed and merged as PR #21 (main be43791, 2026-09-21 ~23:45 local): `ToolClient.call` wrapped in `anyio.fail_after(timeout + hard_margin)`, new `HttpToolClient(url, name)` with connect/aclose/reconnect used by the CLI, `post_run_call` reconnects netverify between attempts. LOCAL ROW 1 attempt 2 was stopped after its 001 record (1192 s, 4 turns, 2 nudges, no fix); twin reset to golden ~23:38; results/local/runs.jsonl keeps that record (delete it if the qwen3 settings change, or the row resumes at 002 with mixed settings).
- QWEN3 EMPTY TURNS diagnosed (2026-09-21 ~23:55): NOT truncation (topology result was 1937 chars). Ollama's log shows each empty turn generated only a handful of tokens (n_tokens +41 per turn including the nudge), i.e. qwen3:14b with `think: false` emits (almost) nothing inside the real 4.5 k-char skill prompt with the full tool list, while a direct probe with a short system prompt and 2 tools returned structured tool calls both with think false (199 s) and think true (332 s, 658 eval tokens). Fixes in PR from branch fix/local-topology-cap (checked out in the main tree): read_topology bypasses the cap, empty turns logged with done_reason/eval_count (kept in the transcript), new `--think` CLI flag. Plan: run the qwen2.5-coder:7b tier-A row first (fast, text-form calls, ~15 min/scenario), then try qwen3:14b with `--think` on 001 alone (~3-4 min per turn, so ~1 h per scenario; only worth a row if 001 completes). results/local was cleared (the 001 qwen3 record is gone).
- LOCAL ROW (qwen2.5-coder:7b, tier A) attempt 1 (23:48-00:05 local 2026-09-21/22) CRASHED after scenario 001's loop: the new hard bound raised TimeoutError from a TWINLAB call the harness does not guard (the twinlab session had idled ~13 min like netverify's; only netverify was reconnected). Nothing recorded. Twin reset to golden ~23:58. Fix: `HttpToolClient.call` itself retries once over a fresh session on TimeoutError/MCPError (covers runner and harness alike); merged as PR #23 (2026-09-22 ~00:10 local; main tree on main). ROW attempt 2 relaunched 2026-09-22 00:02 local (baseline e4a6c253d114) from the main tree with that code: `uv run netbench run --runner local --model qwen2.5-coder:7b --skill diagnose-solo --scenarios 001,...,014 --matrix local --max-turns 20 --result-cap 3000` (~15 min/scenario). PR #22 (topology cap, diagnostics, --think) is merged: main d783f5e.
- LOCAL ROW (qwen2.5-coder:7b) attempt 2 (2026-09-22 00:02-~01:30): 6 records (001-006), every one 20 turns / text_calls=20 / max_turns_hit / no change (670-1033 s each), then the batch crashed with `CancelledError: Cancelled via cancel scope ... by Task-1 running at cli.py:166` (the HttpToolClient.aclose `move_on_after(15)` cancelling an anyio task-group teardown during a reconnect or the final close) and the process stayed wedged until 10:02. Diagnosed 2026-09-22 ~10:10: the Ollama POST for 007 failed with a non-timeout httpx read error -> mapped to RunnerUnavailable -> matrix stopped unrecorded -> final aclose hung (CancelledError is not an Exception). Fixed on branch fix/local-transport-errors (checked out in the main tree) = PR #24, MERGED 2026-09-22 ~10:15 local (main tree on main): ConnectError only -> RunnerUnavailable, other HTTP errors -> `ModelCallFailed` (per-run failure); `HttpToolClient.reconnect` abandons wedged sessions (list `_abandoned`) instead of closing; `netbench run` prints the summary and `os._exit`s (2 = runner unavailable, 1 = other). Twin reset to golden ~10:05; results/local/runs.jsonl keeps the 6 records; ROW attempt 3 (10:07-12:33 local 2026-09-22) recorded 007-012 (12 of 14 now; 010 = `ModelTimeout` recorded as a run error, the rest 20 turns / text_calls=20 / no change; median 850 s) and then died during 013 with another escaped `CancelledError: Cancelled via cancel scope` (exited promptly this time, nothing recorded for 013). Twin reset to golden ~12:35. Cause found (12:12 log): after a twinlab run_show_command timed out and the client reconnected, the ABANDONED http_session async generator was finalised from another task -> anyio `Attempted to exit cancel scope in a different task` -> escaped CancelledError. Fix: `HttpToolClient` runs each session in its own holder task (enter, wait for a stop event, exit in the same task); reconnect signals the old holder and opens a new one; aclose waits 15 s; a hung holder is left to process exit. In-memory test added. PR #25, MERGED 2026-09-22 ~12:45 local (main tree on main). ROW COMPLETE 2026-09-22 13:19 local (attempt 4 finished 013-014 cleanly with the holder code): `local-diagnose-solo-qwen2.5-coder-7b`, 14 runs, root cause 0 %, fix correct 0 %, verified 0 %, golden 0 %, 1 error (010 ModelTimeout), mean 951 s / median 866 s, 3.7 h total, ~132 k prompt tokens and ~700 output tokens per run (20 turns), every tool call as text, no change ever applied. NEXT: results PR (results/local/runs.jsonl + lab-notes local section + README local row + handoff mirror), then decide on qwen3:14b from the --think experiment on 001 started ~13:20 local (`--name local-diagnose-solo-qwen3-14b-think`, ~1 h).

Matrix v1 (Sonnet 5, results/v1/runs.jsonl, golden id e4a6c253d114):

| row | runs | root cause | fix correct | verified | golden | mean s | errors |
| claude-diagnose-sonnet-p0 | 8 | 75 % | 88 % | 75 % | 83 % | 861 | 1 |
| claude-diagnose-sonnet (team, verifier) | 22 | 91 % | 95 % | 91 % | 82 % | 393 | 1 |
| claude-diagnose-sonnet-noverify | 22 | 82 % | 95 % | 0 % | 82 % | 152 | 0 |
| claude-diagnose-solo-sonnet | 22 | 82 % | 95 % | 91 % | 77 % | 225 | 1 (018 RunTimeout) |
| claude-diagnose-solo-sonnet-noverify-diagonly | 22 | 91 % | 5 % | 0 % | 5 % | 75 | 0 (skill-text defect: 0 ops applied, kept as evidence) |
| claude-diagnose-solo-sonnet-noverify | 22 | 82 % | 91 % | 0 % | 77 % | 77 | 0 |

Findings of the verifier-off row (details in lab-notes): median 150 s, no hangs (both CLI
hangs of the verifier-on rows were inside the verifier subagent). 019 is the headline: the
team blamed r4's prefix-list for the missing guest NAT on r3, added `permit 10.0.20.0/24`
so the guest subnet is advertised to the ISP (the route leak the intent policy forbids)
and reported done; only the harness-side fix_correct/collateral_free caught it. 022
(control) reported transient flaps on r2 read from the counters left by earlier injections
and changed nothing. 021 led with the BGP half (r4 bgp.prefix_list) so root cause missed.
006 took the prefix-list work-around a third time. 007 got its first clean run but bundled
two extra lines into the op (not golden).

Solo no-verifier defect (found 2026-09-21, fixed in 550b14c on bench/v1-ablation-rows): the
solo skill said "skip steps 4 and 5" for --no-verifier, but in the solo skill step 4 is the
change (the team skill's flag correctly skips 5 and 6). The agent obeyed: right cause on
20 of 22, zero changes applied, about a minute per run. Those 22 records were relabelled
`claude-diagnose-solo-sonnet-noverify-diagonly` (a diagnose-only row, useful as a pure
diagnosis-accuracy number) and the row is being rerun under the proper name with the fixed
clause ("still apply the change in step 4, then skip step 5 entirely"). Written up in lab-notes (2becf3f).

Solo row (verifier on) is complete and committed (8aded21, pushed): 22 runs, median 131 s, misses are 001 (blamed r1 for r3's area mismatch and changed r1 to match: fix correct, not the planted cause, not golden), 006 (prefix-list work-around, now 4 of 4 across rows), 019 (right cause, NAT clause order differs from golden), 021 (led with the BGP half), and 018 (RunTimeout: the solo agent hung after 9 tool calls, killed at 1500 s). Solo, defect and harness-retry sections are written in lab-notes (2becf3f). Earlier in the row the harness crashed once: 10 runs recorded (001-010) before at 16:15 local on
2026-09-21: after the agent had fixed and exported 011, the harness's own post-run
`reachability_matrix` / `intent_check` on netverify exceeded its MCP timeout (`MCPError:
Request 'tools/call' timed out`, harness.py run_one lines 174-177) and the batch exited 1
without recording 011. Servers and containers were healthy afterwards (slow probes, not a
wedge). Fix to make (small, in the results branch or its own PR): wrap the post-run scoring
calls in run_one so a timeout records an error record for that run, resets, and continues,
instead of killing the batch. Meanwhile the row was reset with `nettwin lab golden` and
relaunched with the same command (resumes at 011). Solo results so far: 001 and 006 not root
cause / not golden (001 solo: RC False, FIX True, VER True), the other eight clean.

Resume procedure (every new chat): `wsl -d Containerlab -- sleep infinity` in the background,
`uv run nettwin lab up` (34 s, all checks pass, lands on e4a6c253d114), then in WSL
`make -C lab serve-bench` in the background (serve.sh keeps both servers in the foreground),
`claude auth status` must say loggedIn true / max, then the batch in the background with a
Monitor on results/v1/runs.jsonl (one line per new record). Batches resume by run id.
`uv run netbench report --matrix v1` renders the table.

Lessons this session: the Bash tool blocks `sleep N; cmd` chains (use Monitor or
run_in_background); the auto-mode classifier denied a `gh run watch` polling loop, plain
`gh pr checks N` and `gh run list --branch X` are fine; `cd` into a worktree moves the Bash
tool's persistent cwd (use absolute paths); reading the admin token needs the heredoc form
(`wsl.exe -d Containerlab -- bash -s <<'EOF' cat ~/.nettwin/admin.token EOF`), a bare
`wsl -- cat /home/...` path gets MSYS-rewritten; Ollama and Claude runs must never overlap
(benchmark timing), so M10 live work waits for the Claude rows.

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
