# Roadmap

One pull request per milestone. Each is merged when its "green" condition holds.
Design and rationale live in [design.md](design.md).

| # | Branch | Scope | Green when |
|---|---|---|---|
| M0 | `chore/scaffold` | uv workspace, core contracts and tests, CI, `nettwin doctor`, WSL wrapper, setup docs | CI passes, doctor reports honestly |
| M1 | `feat/lab-topology` | reference topology, custom FRR image, golden configs, Makefile, MTU veth test, lab notes | `up` + `check` from cold in under 2 min, three faults reproduce by hand |
| M2 | `feat/twinlab-read` | twinlab server: topology resource, show-command allowlist, snapshot and rollback, `.mcp.json` | round trip restores running-config byte for byte, Inspector lists tools |
| M3 | `feat/twinlab-write` | typed `apply_config`, admin route for fault injection, tier A scenarios 001 to 014 | each scenario injects, shows its symptom, rolls back clean |
| M4 | `feat/netverify` | convergence wait, reachability matrix, route diff, intent rules, HMAC attestation, policy | golden all green, each fault fails exactly its rule |
| M5 | `feat/export-gate` | `export_change` with elicitation, bench auto-approve, `nettwin approve`, server prompts | bundle only after accept |
| M6 | `feat/bench-harness` | harness, fake agent replay, scoring, report, resumable runs | fake agent 100 % on tier A |
| M7 | `feat/claude-agents` | Claude Code subagents, `/diagnose`, `/diagnose-solo` | interactive run ends in an approved export and is scored |
| M8 | `feat/bench-scenarios-bc` | VLAN and nftables scenarios, stretch and control | fake agent 100 % on all |
| M9 | `feat/bench-claude-runner` | headless Claude runner, matrix v1 on Sonnet 5 | complete matrix and README table |
| M10 | `feat/local-agent` | provider-agnostic loop on Ollama, local baseline matrix | local matrix complete |
| M11 | `chore/readme-results` | README results, recording, limitations | published |

## Runtime split

- WSL2 (Containerlab distro): docker, containerlab, the lab, both MCP servers, all runtime state in `~/.nettwin`.
- Windows: editing, git, Claude Code (interactive and headless), the harness, Ollama.

## Zero spend

Claude runs use the Claude Code subscription (subagents and headless `-p`). The local axis
uses Ollama. Servers and harness are exercised by a scripted fake agent so tests cost
nothing. There is no paid API usage anywhere in the plan.

## Steps only the owner can do

1. Install GitHub CLI and `gh auth login`.
2. Install the Containerlab WSL distro and disable Docker Desktop integration for it
   ([setup-wsl.md](setup-wsl.md)).
3. Before M9: install the Claude Code CLI and sign in.
4. Before M10: `ollama pull qwen3:14b`.
