"""Containerlab topology parsing and graph helpers.

The parsed topology backs the `lab://topology` resource and the static `no_spof` check.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field


class Node(BaseModel):
    name: str
    kind: str = "linux"
    image: str | None = None
    role: str = "linux"


class Link(BaseModel):
    a_node: str
    a_iface: str
    b_node: str
    b_iface: str

    def touches(self, node: str) -> bool:
        return node in (self.a_node, self.b_node)

    def other(self, node: str) -> str | None:
        if node == self.a_node:
            return self.b_node
        if node == self.b_node:
            return self.a_node
        return None

    def label(self) -> str:
        return f"{self.a_node}:{self.a_iface}<->{self.b_node}:{self.b_iface}"


class Topology(BaseModel):
    name: str
    nodes: dict[str, Node]
    links: list[Link] = Field(default_factory=list)

    def neighbors(self, node: str, *, without: Link | None = None) -> set[str]:
        result: set[str] = set()
        for link in self.links:
            if link is without:
                continue
            other = link.other(node)
            if other is not None:
                result.add(other)
        return result

    def is_connected(self, nodes: Iterable[str], *, without: Link | None = None) -> bool:
        """True if every node in `nodes` is in one connected component of the whole graph."""
        wanted = set(nodes)
        if not wanted:
            return True
        start = next(iter(wanted))
        seen = {start}
        queue = deque([start])
        while queue:
            current = queue.popleft()
            for nxt in self.neighbors(current, without=without):
                if nxt not in seen:
                    seen.add(nxt)
                    queue.append(nxt)
        return wanted <= seen

    def single_link_partitions(self, nodes: Iterable[str]) -> list[Link]:
        """Links whose individual failure disconnects the given node set."""
        wanted = list(nodes)
        return [link for link in self.links if not self.is_connected(wanted, without=link)]

    def to_resource(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "nodes": [n.model_dump() for n in self.nodes.values()],
            "links": [
                {"a": f"{link.a_node}:{link.a_iface}", "b": f"{link.b_node}:{link.b_iface}"}
                for link in self.links
            ],
        }


def _endpoint(raw: Any) -> tuple[str, str]:
    if isinstance(raw, str):
        node, _, iface = raw.partition(":")
        if not node or not iface:
            raise ValueError(f"bad endpoint {raw!r}, expected node:iface")
        return node, iface
    if isinstance(raw, dict) and "node" in raw and "interface" in raw:
        return str(raw["node"]), str(raw["interface"])
    raise ValueError(f"bad endpoint {raw!r}")


def parse_topology(data: dict[str, Any]) -> Topology:
    topo = data.get("topology") or {}
    kinds = topo.get("kinds") or {}
    nodes: dict[str, Node] = {}
    for name, spec in (topo.get("nodes") or {}).items():
        spec = spec or {}
        kind = spec.get("kind", "linux")
        kind_defaults = kinds.get(kind) or {}
        labels = spec.get("labels") or {}
        nodes[name] = Node(
            name=name,
            kind=kind,
            image=spec.get("image", kind_defaults.get("image")),
            role=str(labels.get("role", kind)),
        )
    links: list[Link] = []
    for raw in topo.get("links") or []:
        endpoints = raw.get("endpoints") if isinstance(raw, dict) else None
        if not endpoints or len(endpoints) != 2:
            raise ValueError(f"link needs exactly two endpoints: {raw!r}")
        (a_node, a_iface), (b_node, b_iface) = _endpoint(endpoints[0]), _endpoint(endpoints[1])
        for node in (a_node, b_node):
            if node not in nodes:
                raise ValueError(f"link references unknown node {node!r}")
        links.append(Link(a_node=a_node, a_iface=a_iface, b_node=b_node, b_iface=b_iface))
    return Topology(name=str(data.get("name", "lab")), nodes=nodes, links=links)


def load_topology(path: Path) -> Topology:
    with path.open("rb") as fh:
        return parse_topology(yaml.safe_load(fh) or {})
