# Lab host setup on Windows 11

The lab and both MCP servers run inside a WSL2 distro that has its own Docker engine.
Docker Desktop cannot be used: containerlab wires veth pairs between container network
namespaces and needs the engine in the same distro it runs in.

Everything else (this repo, Claude Code, the benchmark harness, Ollama) stays on Windows.
`nettwin doctor` verifies every step below.

## 1. Install the Containerlab WSL distro

The containerlab project ships a ready-made WSL2 image with the Docker engine, containerlab,
and the usual network tools. Follow the current instructions at
<https://containerlab.dev/windows/>. In short: download the `.wsl` file from the
`srl-labs/wsl-containerlab` releases page, then in PowerShell:

```powershell
wsl --install --from-file .\Containerlab.wsl
```

Open it once (`wsl -d Containerlab`) and confirm:

```bash
docker version --format '{{.Server.Version}}'
containerlab version
nft --version
```

If you would rather use a stock Ubuntu distro, install Docker Engine (not Docker Desktop)
inside it per docs.docker.com and containerlab per containerlab.dev/install. Set
`NETTWIN_WSL_DISTRO` to the distro name.

## 2. Keep Docker Desktop out of the way

Docker Desktop, Settings, Resources, WSL integration: make sure the Containerlab distro is
**unchecked**. Otherwise `docker` inside the distro talks to Docker Desktop's engine and
containerlab cannot reach the containers. `nettwin doctor` reports "Docker Desktop" in the
engine line when this is wrong.

## 2b. Keep the distro alive

WSL2 shuts the VM down about a minute after the last Windows-side session closes, even
with Docker running inside. On the next command Docker restarts the containers, but the
veth links containerlab created are gone: every node keeps only its management interface
and the lab is silently dead. Two defences:

- While working, keep a session open from Windows, for example in a spare terminal:

  ```powershell
  wsl -d Containerlab -- sleep infinity
  ```

  (`nettwin serve`, from milestone M2, is a long-running process that does the same.)
- Raise the idle timeout in `%USERPROFILE%\.wslconfig` (Windows 11), then `wsl --shutdown`:

  ```ini
  [wsl2]
  vmIdleTimeout=3600000
  ```

`nettwin lab up` always redeploys with `--reconfigure`, so a stale lab is fixed by rerunning it.

## 3. Tools inside the distro

```bash
sudo apt-get update && sudo apt-get install -y make rsync
curl -LsSf https://astral.sh/uv/install.sh | sh
echo 'export UV_PROJECT_ENVIRONMENT=$HOME/.venvs/nettwin' >> ~/.bashrc
echo 'export NETTWIN_STATE_DIR=$HOME/.nettwin' >> ~/.bashrc
```

The repo is read from `/mnt/c/dev/NetTwin`. The venv and all runtime state live inside the
distro so nothing slow or lock-prone crosses the Windows filesystem boundary.

## 4. Tools on Windows

- GitHub CLI: <https://cli.github.com/>, then `gh auth login`.
- Claude Code CLI for headless benchmark runs (milestone M9):
  `npm install -g @anthropic-ai/claude-code`, then sign in with your subscription.
- Ollama is already installed; pull a stronger tool-calling model when convenient:
  `ollama pull qwen3:14b`.

## 5. Check

```powershell
uv run nettwin doctor
```

Every line should be PASS except the two server ports, which show WARN until
`nettwin serve` is running.
