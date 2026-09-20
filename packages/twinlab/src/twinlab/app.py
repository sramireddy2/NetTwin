"""The twinlab application: everything the MCP tools and admin routes do.

Keeping this separate from the transport lets tests drive it with a `FakeExecutor`.
"""

from __future__ import annotations

import asyncio
import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from nettwin_core.executor import DockerExecutor, ExecResult, Executor
from nettwin_core.models import ChangeResult, Snapshot
from nettwin_core.ops import Op
from nettwin_core.scenario import Scenario, load_scenarios
from nettwin_core.settings import Settings
from nettwin_core.topology import Topology, load_topology
from twinlab import allowlist
from twinlab.apply import ApplyError, apply_ops, describe_change
from twinlab.changes import ChangeStore
from twinlab.snapshot import SnapshotStore, capture, restore


class UnknownNode(ValueError):
    pass


@dataclass
class TwinLab:
    settings: Settings
    topology: Topology
    executor: Executor
    store: SnapshotStore
    changes: ChangeStore
    write_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    last_injection: dict[str, Any] | None = None

    @classmethod
    def from_settings(cls, settings: Settings, executor: Executor | None = None) -> TwinLab:
        settings.ensure_dirs()
        return cls(
            settings=settings,
            topology=load_topology(settings.topology_path),
            executor=executor or DockerExecutor(settings.lab_name),
            store=SnapshotStore(settings.snapshots_dir),
            changes=ChangeStore(settings.changes_dir),
        )

    # --- reads -----------------------------------------------------------------------------

    def require_node(self, node: str) -> str:
        if node not in self.topology.nodes:
            known = ", ".join(sorted(self.topology.nodes))
            raise UnknownNode(f"unknown node {node!r}; nodes: {known}")
        return node

    async def show(self, node: str, cmd: str) -> ExecResult:
        """Run an allowlisted read-only command on a node."""
        self.require_node(node)
        allowed = allowlist.check(cmd)
        return await self.executor.exec(node, allowed.argv, timeout=allowed.timeout)

    def topology_resource(self) -> dict[str, Any]:
        resource = self.topology.to_resource()
        resource["snapshots"] = self.store.ids()[-5:]
        resource["golden_snapshot"] = self.golden_id
        return resource

    # --- state -----------------------------------------------------------------------------

    async def snapshot(self) -> Snapshot:
        snap = await capture(self.executor, self.topology, self.settings.lab_name)
        self.store.save(snap)
        return snap

    async def rollback(self, snapshot_id: str) -> dict[str, Any]:
        """Restore a stored snapshot. Serialised with every other write to the twin."""
        target = self.store.load(snapshot_id)
        async with self.write_lock:
            current = await self.snapshot()
            changed = await restore(self.executor, self.topology, target, current)
            after = await self.snapshot()
        return {
            "restored": target.id,
            "before": current.id,
            "after": after.id,
            "matches_target": after.id == target.id,
            "changed_nodes": changed,
        }

    async def apply(self, node: str, ops: list[Op], rationale: str = "") -> ChangeResult:
        """Apply typed ops to one node between two snapshots; roll back if any op fails."""
        self.require_node(node)
        if not ops:
            raise ValueError("apply needs at least one op")
        async with self.write_lock:
            before = await self.snapshot()
            try:
                await apply_ops(self.executor, node, ops)
            except ApplyError as exc:
                current = await self.snapshot()
                await restore(self.executor, self.topology, before, current)
                raise ApplyError(
                    node, exc.argv, f"{exc.detail} (rolled back to snapshot {before.short_id})"
                ) from exc
            after = await self.snapshot()
        change = ChangeResult(
            change_id=secrets.token_hex(8),
            node=node,
            before_snapshot_id=before.id,
            after_snapshot_id=after.id,
            diff=describe_change(before.nodes[node], after.nodes[node], node),
            ops=list(ops),
            rationale=rationale,
            applied_at=datetime.now(UTC),
        )
        self.changes.save(change)
        return change

    # --- admin: never exposed as MCP tools ---------------------------------------------------

    @property
    def golden_id(self) -> str | None:
        marker = self.settings.golden_marker
        return marker.read_text(encoding="utf-8").strip() if marker.exists() else None

    def scenarios(self) -> list[Scenario]:
        return load_scenarios(Path(self.settings.scenarios_dir))

    def scenario(self, scenario_id: str) -> Scenario:
        matches = [
            s for s in self.scenarios() if s.id == scenario_id or s.id.startswith(scenario_id)
        ]
        if len(matches) != 1:
            raise KeyError(f"scenario {scenario_id!r} matches {len(matches)} scenarios")
        return matches[0]

    async def inject(self, scenario_id: str) -> dict[str, Any]:
        """Plant a NetBench fault. Only reachable through the token-protected admin route."""
        scenario = self.scenario(scenario_id)
        async with self.write_lock:
            before = await self.snapshot()
            for node, ops in scenario.inject.items():
                self.require_node(node)
                await apply_ops(self.executor, node, ops)
            after = await self.snapshot()
        self.last_injection = {
            "scenario": scenario.id,
            "symptom": scenario.symptom,
            "before": before.id,
            "after": after.id,
            "at": datetime.now(UTC).isoformat(),
        }
        return dict(self.last_injection)

    async def golden(self) -> dict[str, Any]:
        """Re-apply golden state with the lab's golden.sh, then record the golden snapshot."""
        script = self.settings.lab_run_dir / "scripts" / "golden.sh"
        if not script.exists():
            raise FileNotFoundError(f"golden script not found at {script}; run `make sync` first")
        async with self.write_lock:
            proc = await asyncio.create_subprocess_exec(
                "bash",
                str(script),
                env={"LAB": self.settings.lab_name, "PATH": "/usr/local/bin:/usr/bin:/bin"},
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            out, _ = await asyncio.wait_for(proc.communicate(), 180)
            if proc.returncode != 0:
                raise RuntimeError(f"golden.sh failed: {out.decode(errors='replace')[-2000:]}")
            snap = await self.snapshot()
        self.settings.golden_marker.write_text(snap.id + "\n", encoding="utf-8")
        self.last_injection = None
        return {"golden": snap.id, "output": out.decode(errors="replace").strip().splitlines()[-3:]}

    def status(self) -> dict[str, Any]:
        return {
            "lab": self.settings.lab_name,
            "nodes": len(self.topology.nodes),
            "golden_snapshot": self.golden_id,
            "snapshots": len(self.store.ids()),
            "changes": len(self.changes.ids()),
            "last_injection": self.last_injection,
            "scenarios": [s.id for s in self.scenarios()]
            if self.settings.scenarios_dir.exists()
            else [],
        }
