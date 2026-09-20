"""Read-only command allowlist shared by twinlab (run_show_command) and netverify (its executor).

A command string from an agent is split with shlex, checked for shell metacharacters,
and matched against a small set of read-only families. FRR commands must start with
`show` and are passed as a single `-c` argument to vtysh; everything else is run as argv.
No command ever reaches a shell.
"""

from __future__ import annotations

import ipaddress
import re
import shlex
from dataclasses import dataclass
from typing import Literal

FRR_TIMEOUT = 10.0
KERNEL_TIMEOUT = 10.0
PROBE_TIMEOUT = 20.0

_TOKEN_RE = re.compile(r"^[A-Za-z0-9./:_,+-]+$")
_FORBIDDEN_RE = re.compile(r"[;|&<>`$'\"\\\r\n]")

FRR_FAMILIES: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p)
    for p in (
        r"show version",
        r"show running-config(?: \S+)?",
        r"show interface(?: \S+){0,2}",
        r"show ip route(?: \S+){0,4}",
        r"show ip ospf(?: \S+){0,5}",
        r"show (?:ip )?bgp(?: \S+){0,7}",
        r"show ip prefix-list(?: \S+){0,3}",
        r"show route-map(?: \S+)?",
        r"show ip protocols?",
        r"show ip forwarding",
        r"show zebra(?: \S+){0,2}",
        r"show ip nht",
    )
)

_IP_FLAGS = {"-j", "-json", "-br", "-brief", "-d", "-details", "-4", "-s", "-stats", "-o"}
_IP_OBJECTS = {
    "addr",
    "address",
    "a",
    "link",
    "l",
    "route",
    "r",
    "neigh",
    "neighbour",
    "neighbor",
    "n",
}
_IP_VERBS_DENIED = {
    "add",
    "del",
    "delete",
    "set",
    "change",
    "replace",
    "flush",
    "append",
    "prepend",
    "up",
    "down",
    "monitor",
}
_BRIDGE_OBJECTS = {"vlan", "fdb", "link"}
_NFT_FLAGS = {"-a", "-s", "-j", "-n", "-nn"}
_PING_INT_FLAGS = {"-c": (1, 5), "-W": (1, 3), "-w": (1, 10), "-s": (0, 9000), "-t": (1, 255)}
_SYSCTL_RE = re.compile(r"^net\.ipv4\.[a-z0-9_.]+$")

EXAMPLES = (
    "show ip route",
    "show ip route 10.0.40.0/24",
    "show ip ospf neighbor",
    "show ip ospf interface eth1",
    "show bgp summary",
    "show bgp ipv4 unicast neighbors 203.0.113.2 advertised-routes",
    "show running-config",
    "ip -j -4 addr",
    "ip -j -d link show eth3.10",
    "ip route get 10.0.40.10",
    "bridge -j vlan show",
    "nft -a list ruleset",
    "ping -c 2 -W 1 -M do -s 1472 10.0.40.10",
    "traceroute -n -w 1 -m 8 10.0.40.10",
    "nc -z -w 2 10.0.40.10 80",
    "sysctl net.ipv4.ip_forward",
)


@dataclass(frozen=True)
class AllowedCommand:
    argv: list[str]
    family: Literal["frr", "kernel"]
    timeout: float


class CommandNotAllowed(ValueError):
    """Raised for anything outside the allowlist; the message explains what is allowed."""


def _deny(reason: str) -> CommandNotAllowed:
    return CommandNotAllowed(f"{reason}. Allowed forms include: " + "; ".join(EXAMPLES))


def _target(token: str) -> str:
    try:
        ipaddress.ip_address(token)
    except ValueError as exc:
        raise _deny(f"probe target must be an IP address, got {token!r}") from exc
    return token


def _check_ip(argv: list[str]) -> None:
    i = 1
    while i < len(argv) and argv[i] in _IP_FLAGS:
        i += 1
    if i >= len(argv) or argv[i] not in _IP_OBJECTS:
        raise _deny("ip: only addr, link, route and neigh objects may be read")
    rest = argv[i + 1 :]
    if rest and rest[0] in ("show", "list", "ls", "get"):
        rest = rest[1:]
    if len(rest) > 4 or any(t in _IP_VERBS_DENIED for t in rest):
        raise _deny("ip: only read forms are allowed")


def _check_bridge(argv: list[str]) -> None:
    i = 1
    while i < len(argv) and argv[i] in _IP_FLAGS:
        i += 1
    if i >= len(argv) or argv[i] not in _BRIDGE_OBJECTS:
        raise _deny("bridge: only vlan, fdb and link may be read")
    rest = argv[i + 1 :]
    if rest and rest[0] in ("show", "list"):
        rest = rest[1:]
    if len(rest) > 4 or any(t in _IP_VERBS_DENIED for t in rest):
        raise _deny("bridge: only read forms are allowed")


def _check_nft(argv: list[str]) -> None:
    i = 1
    while i < len(argv) and argv[i] in _NFT_FLAGS:
        i += 1
    rest = argv[i:]
    if not rest or rest[0] != "list":
        raise _deny("nft: only 'list' is allowed")
    if len(rest) < 2 or rest[1] not in ("ruleset", "tables", "table", "chain", "chains"):
        raise _deny(
            "nft: list ruleset | tables | table <family> <name> | chain <family> <table> <chain>"
        )
    if len(rest) > 5:
        raise _deny("nft: too many arguments")


def _check_ping(argv: list[str]) -> None:
    i = 1
    target: str | None = None
    while i < len(argv):
        tok = argv[i]
        if tok in _PING_INT_FLAGS:
            lo, hi = _PING_INT_FLAGS[tok]
            if i + 1 >= len(argv) or not argv[i + 1].isdigit() or not lo <= int(argv[i + 1]) <= hi:
                raise _deny(f"ping: {tok} must be an integer between {lo} and {hi}")
            i += 2
        elif tok == "-M":
            if i + 1 >= len(argv) or argv[i + 1] not in ("do", "dont", "want", "probe"):
                raise _deny("ping: -M takes do, dont, want or probe")
            i += 2
        elif tok == "-I":
            if i + 1 >= len(argv):
                raise _deny("ping: -I needs an interface")
            i += 2
        elif tok in ("-n", "-q", "-4"):
            i += 1
        elif tok.startswith("-"):
            raise _deny(f"ping: flag {tok} is not allowed")
        else:
            if target is not None:
                raise _deny("ping: exactly one target")
            target = _target(tok)
            i += 1
    if target is None:
        raise _deny("ping: missing target")
    if "-c" not in argv:
        raise _deny("ping: -c <count> is required (max 5)")


def _check_traceroute(argv: list[str]) -> None:
    i = 1
    target: str | None = None
    limits = {"-w": (1, 5), "-m": (1, 20), "-q": (1, 3)}
    while i < len(argv):
        tok = argv[i]
        if tok in limits:
            lo, hi = limits[tok]
            if i + 1 >= len(argv) or not argv[i + 1].isdigit() or not lo <= int(argv[i + 1]) <= hi:
                raise _deny(f"traceroute: {tok} must be an integer between {lo} and {hi}")
            i += 2
        elif tok in ("-n", "-I", "-4"):
            i += 1
        elif tok.startswith("-"):
            raise _deny(f"traceroute: flag {tok} is not allowed")
        else:
            if target is not None:
                raise _deny("traceroute: exactly one target")
            target = _target(tok)
            i += 1
    if target is None:
        raise _deny("traceroute: missing target")


def _check_sysctl(argv: list[str]) -> None:
    rest = [t for t in argv[1:] if t != "-n"]
    if len(rest) != 1 or not _SYSCTL_RE.match(rest[0]):
        raise _deny("sysctl: only reading net.ipv4.* keys is allowed")


def _check_nc(argv: list[str]) -> None:
    """nc -z -w N <ip> <port>: a TCP connect probe and nothing else."""
    i = 1
    positional: list[str] = []
    while i < len(argv):
        tok = argv[i]
        if tok == "-w":
            if i + 1 >= len(argv) or not argv[i + 1].isdigit() or not 1 <= int(argv[i + 1]) <= 5:
                raise _deny("nc: -w must be an integer between 1 and 5")
            i += 2
        elif tok in ("-z", "-v", "-4"):
            i += 1
        elif tok.startswith("-"):
            raise _deny(f"nc: flag {tok} is not allowed")
        else:
            positional.append(tok)
            i += 1
    if "-z" not in argv or len(positional) != 2:
        raise _deny("nc: only 'nc -z -w N <ip> <port>' is allowed")
    _target(positional[0])
    if not positional[1].isdigit() or not 1 <= int(positional[1]) <= 65535:
        raise _deny("nc: port must be between 1 and 65535")


_KERNEL_CHECKS = {
    "ip": (_check_ip, KERNEL_TIMEOUT),
    "bridge": (_check_bridge, KERNEL_TIMEOUT),
    "nft": (_check_nft, KERNEL_TIMEOUT),
    "ping": (_check_ping, PROBE_TIMEOUT),
    "traceroute": (_check_traceroute, PROBE_TIMEOUT),
    "nc": (_check_nc, PROBE_TIMEOUT),
    "sysctl": (_check_sysctl, KERNEL_TIMEOUT),
}


def check(cmd: str) -> AllowedCommand:
    """Validate a command string and return the argv to execute inside the container."""
    if not cmd or not cmd.strip():
        raise _deny("empty command")
    if _FORBIDDEN_RE.search(cmd):
        raise _deny("shell metacharacters, quotes and pipes are not allowed")
    if len(cmd) > 200:
        raise _deny("command too long")
    try:
        argv = shlex.split(cmd)
    except ValueError as exc:
        raise _deny(f"could not parse command: {exc}") from exc
    if not argv:
        raise _deny("empty command")
    for tok in argv:
        if not _TOKEN_RE.match(tok):
            raise _deny(f"token {tok!r} contains characters that are not allowed")
    text = " ".join(argv)
    if argv[0] == "show":
        if not any(p.fullmatch(text) for p in FRR_FAMILIES):
            raise _deny(f"FRR command {text!r} is not in the allowlist")
        return AllowedCommand(argv=["vtysh", "-c", text], family="frr", timeout=FRR_TIMEOUT)
    checker = _KERNEL_CHECKS.get(argv[0])
    if checker is None:
        raise _deny(f"{argv[0]!r} is not an allowed program")
    fn, timeout = checker
    fn(argv)
    return AllowedCommand(argv=argv, family="kernel", timeout=timeout)
