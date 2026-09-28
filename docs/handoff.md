---
name: nettwin-handoff
description: START HERE for NetTwin (2026-09-27 late night) — planned build complete: M0-M11 merged, local row 22/22 (PR #30), README rewritten with a live terminal recording (PR #31); only optional user decisions remain; procedure for any further batch
metadata:
  type: project
  modified: 2026-09-28T02:45:56.196Z
---

# NetTwin handoff — start here (2026-09-27 23:00)

Repo `C:\dev\NetTwin`, remote https://github.com/sramireddy2/NetTwin (public). PRs #1 to #31
merged; #30 is the local row's results PR, #31 the README rewrite with the demo recording. The
full per-session log, every matrix analysis and every lesson is in [[nettwin-history]];
confirmed decisions and the zero-spend plan are in [[nettwin-project-context]].

## Where the build stands

- The planned scope is complete. Milestones M0 to M11 are delivered: containerlab twin, twinlab
  and netverify MCP servers, export gate, NetBench harness, Claude Code agent team with
  `/diagnose` and `/diagnose-solo`, headless Claude runner, local Ollama runner, README results,
  `netbench timeline` and `docs/demo.md`.
- Matrix v1 on Sonnet 5 (`results/v1/runs.jsonl`) is complete: team and solo, verifier on and
  off, 22 scenarios each, plus the evidence rows `-p0` and `-diagonly`.
- Local axis (`results/local/runs.jsonl`): row `local-diagnose-solo-qwen2.5-coder-7b` is complete,
  22 of 22. Root cause, fix correct, minimal and golden 5 %, all from the no-fault control (022),
  which a model that never reports and never changes passes by construction; verified 0 %, no
  collateral 32 %, 2 errors (010 and 015, `ModelTimeout`), median 915 s, 6.6 h in total. 404
  tool calls (374 show commands, 18 snapshots, 12 topology reads), every one as JSON text, no
  apply, no report, never an nftables command. qwen3:14b `--think` has one record on 001 and was
  dropped. README, lab-notes (M10 section) and roadmap carry these numbers.
- README rewritten for a general reader (PR #31): badges, plain-language pitch, "See it work"
  with `docs/media/demo-019.svg`, four-step "How it works", the original mermaid diagram with
  colour classes (checked in light and dark themes), tech stack table, simplified results table
  (medians in minutes, local row as "0 of 21 faults"), takeaways, limits, quickstart.
- The demo is a real headless team run on 019, recorded by `scripts/record_demo.py` (harness
  run_one with a live-printing spawn, h20 ping before and after, harness score card, scratch
  results dir, twin reset after) and rendered by `scripts/render_demo.py` (animated SVG, CSS
  keyframes, 72 s loop, waits over 1 s shortened). Two recording runs were spent on 2026-09-27:
  run 1 was clean but truncated the rule text; run 2 (kept) shows the verifier failing a
  route-leak fix (`ip prefix-list CORP seq 20 permit 10.0.20.0/24`), a rollback, the NAT retry
  (one refused for quotes), 20/20 pass, export, all five scores ✓, 6 min 43 s. One line of the
  JSON (`reachability_matrix`) was re-rendered from the run's transcript with the formatter's
  new summary.
- The twin is golden (the harness resets after every run). At the end of this session the WSL
  keep-alive and both servers were still running; they die with WSL.

## Items that need the user

- Optional: a screen capture of an interactive `/diagnose` run in the desktop app (the README
  already has the terminal recording of a live headless run).
- Any extra subscription-metered Claude runs beyond the approved matrix (more trials per cell, a
  Haiku 4.5 row via `--model haiku`).

## If another batch is ever needed

1. Environment. `wsl -d Containerlab -- sleep infinity` in the background (check first with
   `Get-CimInstance Win32_Process -Filter "Name='wsl.exe'"`; it may still be running). Servers:
   `curl -s -o /dev/null -w '%{http_code}' localhost:8001/admin/status` prints 401, and a POST to
   `localhost:8002/mcp` prints 400. If down: `uv run nettwin lab up`, then in WSL
   `make -C lab serve-bench` in the background (heredoc form
   `wsl.exe -d Containerlab -- bash -s <<'EOF' ... EOF`). The harness refuses to start unless
   the twin matches the matrix's golden id (`e4a6c253d114`); fix with `uv run nettwin lab golden`.
2. Keep the PC awake. This PC has Modern Standby (S0 low power idle), and an overnight idle
   sleep once stalled a batch for 90 minutes. `mcp__ccd_host__request_keep_awake` was not
   available in the 2026-09-27 evening window; what worked instead is a background PowerShell
   task that calls `SetThreadExecutionState(0x80000003)` (continuous, system and display
   required) every 60 s through `Add-Type` and exits after a maximum number of hours. It
   changes no power settings and the hold ends with the process; stop it with TaskStop when
   the batch ends.
3. Run the batch in the background with its log redirected to a file, and a Monitor
   (30-minute maximum, re-armed on expiry) grepping the log for `: root_cause=`, `skipped`,
   `crashed`, `runner unavailable`, `new runs recorded`, `Traceback` and ` ERROR `/` WARNING `.
   Recorded run ids are skipped, and a failed reset or injection skips that run for a retry, so
   rerun the same command until the row is complete. Never run an Ollama and a Claude batch
   together.
4. Results: `uv run netbench report --matrix <m>`; per-run tool tallies come from
   `results/<m>/transcripts/*.json` (`messages` with `role: tool` carry `tool_name`).

## Working rules that bit this project

- Git: branch, `gh pr create`, wait for CI with a Monitor loop over
  `gh run list --repo sramireddy2/NetTwin --branch <b> --json status,conclusion` (a `gh run watch`
  loop was denied by the auto-mode classifier), then `gh pr merge N --squash --delete-branch`,
  `git checkout main && git pull`. `gh` lives in `/c/Program Files/GitHub CLI` (add to PATH).
- Bash tool: heredocs halve backslashes, so write files with the Write tool; `sleep N; cmd`
  chains are blocked (use Monitor or `run_in_background`); `cd` persists between calls; Git Bash
  rewrites leading-slash arguments to `wsl.exe` (use `bash -s` over stdin).
- The `claude` CLI is in `/c/Users/shank/AppData/Roaming/npm`; `claude auth status` must show
  loggedIn and max before any Claude row.
- Harness hardening on main: hard time bound on every MCP call, `HttpToolClient` holding each
  session in its own task with reconnect, post-run retries, model timeouts and dropped requests
  recorded as per-run failures, skip-and-retry when preparing a run fails, and a hard process
  exit after the summary.
