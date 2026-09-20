"""The twinlab application: everything the MCP tools do, independent of the MCP layer.

Keeping this separate from `server.py` lets tests drive it with a `FakeExecutor` and lets
the same object back both the MCP tools and the admin HTTP routes.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from nettwin_core.executor import DockerExecutor, ExecResult, Executor
from nettwin_core.models import Snapshot
from nettwin_core.settings import Settings
from nettwin_core.topology import Topology, load_topology
from twinlab import allowlist
from twinlab.snapshot import SnapshotStore, capture, restore


class UnknownNode(ValueError):
    pass


@dataclass
class TwinLab:
    settings: Settings
    topology: Topology
    executor: Executor
    store: SnapshotStore
    write_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    @classmethod
    def from_settings(cls, settings: Settings, executor: Executor | None = None) -> TwinLab:
        settings.ensure_dirs()
        topology = load_topology(settings.topology_path)
        return cls(
            settings=settings,
            topology=topology,
            executor=executor or DockerExecutor(settings.lab_name),
            store=SnapshotStore(settings.snapshots_dir),
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
