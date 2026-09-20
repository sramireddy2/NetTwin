"""Typed configuration operations for the twin.

Every mutation of the twin (an agent's fix, an injected fault, a scenario's expected fix)
is a list of these operations. pydantic validates them and :func:`render` turns them into
argv lists to run inside a node's container. Nothing here ever produces a shell string.
"""

from __future__ import annotations

import ipaddress
import re
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, TypeAdapter, field_validator, model_validator

NODE_RE = re.compile(r"^[a-z][a-z0-9-]{0,31}$")
IFACE_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,15}$")
IDENT_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,31}$")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_NFT_FORBIDDEN_RE = re.compile(r"[;\"'`$\\#]")


def _iface(value: str) -> str:
    if not IFACE_RE.fullmatch(value):
        raise ValueError(f"invalid interface name: {value!r}")
    return value


def _ident(value: str) -> str:
    if not IDENT_RE.fullmatch(value):
        raise ValueError(f"invalid identifier: {value!r}")
    return value


class FrrLines(BaseModel):
    """Lines entered in FRR configure mode, in order, on one node."""

    kind: Literal["frr_lines"] = "frr_lines"
    lines: list[str] = Field(min_length=1)

    @field_validator("lines")
    @classmethod
    def _validate_lines(cls, lines: list[str]) -> list[str]:
        cleaned: list[str] = []
        for line in lines:
            if _CONTROL_RE.search(line):
                raise ValueError("control characters are not allowed in config lines")
            stripped = line.rstrip()
            if not stripped.strip():
                raise ValueError("empty config line")
            if stripped.lstrip().startswith("!"):
                raise ValueError("comments are not configuration")
            cleaned.append(stripped)
        return cleaned


class SetMtu(BaseModel):
    """Set the kernel MTU of an interface."""

    kind: Literal["set_mtu"] = "set_mtu"
    iface: str
    mtu: int = Field(ge=68, le=9216)

    _iface = field_validator("iface")(_iface)


class SetAddr(BaseModel):
    """Assign an address with prefix length to an interface, replacing existing ones by default."""

    kind: Literal["set_addr"] = "set_addr"
    iface: str
    address: str
    replace: bool = True

    _iface = field_validator("iface")(_iface)

    @field_validator("address")
    @classmethod
    def _validate_address(cls, value: str) -> str:
        if "/" not in value:
            raise ValueError("address must include a prefix length, e.g. 10.0.12.1/24")
        return str(ipaddress.ip_interface(value))


class AddVlan(BaseModel):
    """Create an 802.1Q sub-interface on `parent` and bring it up."""

    kind: Literal["add_vlan"] = "add_vlan"
    parent: str
    vlan_id: int = Field(ge=1, le=4094)
    name: str | None = None

    _parent = field_validator("parent")(_iface)

    @field_validator("name")
    @classmethod
    def _validate_name(cls, value: str | None) -> str | None:
        return None if value is None else _iface(value)

    @property
    def link_name(self) -> str:
        return self.name or f"{self.parent}.{self.vlan_id}"


class DelVlan(BaseModel):
    """Delete a VLAN sub-interface."""

    kind: Literal["del_vlan"] = "del_vlan"
    name: str

    _name = field_validator("name")(_iface)


class BridgeVlan(BaseModel):
    """Change VLAN membership of a port on a Linux bridge with vlan_filtering."""

    kind: Literal["bridge_vlan"] = "bridge_vlan"
    action: Literal["add", "del"]
    dev: str
    vlan_id: int = Field(ge=1, le=4094)
    pvid: bool = False
    untagged: bool = False

    _dev = field_validator("dev")(_iface)


class NftRule(BaseModel):
    """Add, insert, delete or flush nftables rules. Data-plane ACLs and NAT live here.

    `delete` takes either the rule handle or the rule text; with only the text, the apply
    engine lists the chain with handles and resolves the matching rule at execution time,
    because handles are assigned by the kernel and cannot be known in advance.
    """

    kind: Literal["nft_rule"] = "nft_rule"
    action: Literal["add", "insert", "delete", "flush_chain"]
    family: Literal["inet", "ip", "ip6"] = "inet"
    table: str
    chain: str
    rule: str | None = None
    handle: int | None = Field(default=None, ge=0)
    index: int | None = Field(default=None, ge=0)

    _table = field_validator("table")(_ident)
    _chain = field_validator("chain")(_ident)

    @field_validator("rule")
    @classmethod
    def _validate_rule(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if _CONTROL_RE.search(value) or _NFT_FORBIDDEN_RE.search(value):
            raise ValueError("rule contains forbidden characters")
        tokens = value.split()
        if not tokens:
            raise ValueError("empty rule")
        if "include" in tokens:
            raise ValueError("include is not allowed in rules")
        return " ".join(tokens)

    @model_validator(mode="after")
    def _check_shape(self) -> NftRule:
        if self.action in ("add", "insert") and not self.rule:
            raise ValueError(f"{self.action} needs a rule")
        if self.action == "delete" and self.handle is None and not self.rule:
            raise ValueError("delete needs a handle or the rule text to look one up")
        if self.action == "flush_chain" and (self.rule or self.handle is not None):
            raise ValueError("flush_chain takes neither rule nor handle")
        return self


Op = Annotated[
    FrrLines | SetMtu | SetAddr | AddVlan | DelVlan | BridgeVlan | NftRule,
    Field(discriminator="kind"),
]

NodeOps = dict[str, list[Op]]

_op_list_adapter: TypeAdapter[list[Op]] = TypeAdapter(list[Op])
_node_ops_adapter: TypeAdapter[NodeOps] = TypeAdapter(NodeOps)


def parse_ops(data: Any) -> list[Op]:
    """Validate a list of op dicts into typed ops."""
    return _op_list_adapter.validate_python(data)


def parse_node_ops(data: Any) -> NodeOps:
    """Validate a mapping of node name to op list."""
    parsed = _node_ops_adapter.validate_python(data)
    for node in parsed:
        if not NODE_RE.fullmatch(node):
            raise ValueError(f"invalid node name: {node!r}")
    return parsed


def render(op: Op) -> list[list[str]]:
    """Render one op into one or more argv commands to run inside the node's container."""
    if isinstance(op, FrrLines):
        argv = ["vtysh", "-c", "configure terminal"]
        for line in op.lines:
            argv += ["-c", line]
        return [argv]
    if isinstance(op, SetMtu):
        return [["ip", "link", "set", "dev", op.iface, "mtu", str(op.mtu)]]
    if isinstance(op, SetAddr):
        commands: list[list[str]] = []
        if op.replace:
            commands.append(["ip", "addr", "flush", "dev", op.iface])
        commands.append(["ip", "addr", "add", op.address, "dev", op.iface])
        return commands
    if isinstance(op, AddVlan):
        name = op.link_name
        return [
            [
                "ip",
                "link",
                "add",
                "link",
                op.parent,
                "name",
                name,
                "type",
                "vlan",
                "id",
                str(op.vlan_id),
            ],
            ["ip", "link", "set", "dev", name, "up"],
        ]
    if isinstance(op, DelVlan):
        return [["ip", "link", "del", "dev", op.name]]
    if isinstance(op, BridgeVlan):
        argv = ["bridge", "vlan", op.action, "dev", op.dev, "vid", str(op.vlan_id)]
        if op.pvid:
            argv.append("pvid")
        if op.untagged:
            argv.append("untagged")
        return [argv]
    if isinstance(op, NftRule):
        location = [op.family, op.table, op.chain]
        if op.action == "flush_chain":
            return [["nft", "flush", "chain", *location]]
        if op.action == "delete":
            if op.handle is None:
                raise ValueError("nft delete: resolve the handle from the rule text first")
            return [["nft", "delete", "rule", *location, "handle", str(op.handle)]]
        argv = ["nft", op.action, "rule", *location]
        if op.index is not None:
            argv += ["index", str(op.index)]
        argv += (op.rule or "").split()
        return [argv]
    raise TypeError(f"unknown op type: {type(op).__name__}")


def render_node_ops(ops: NodeOps) -> dict[str, list[list[str]]]:
    """Render every node's ops, preserving order."""
    return {
        node: [argv for op in node_ops for argv in render(op)] for node, node_ops in ops.items()
    }
