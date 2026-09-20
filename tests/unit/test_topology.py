from __future__ import annotations

from pathlib import Path

import pytest

from nettwin_core.topology import load_topology, parse_topology


def test_parse_fixture_topology(fixtures: Path) -> None:
    topo = load_topology(fixtures / "topology.clab.yml")
    assert topo.name == "fixture"
    assert set(topo.nodes) == {"r1", "r2", "r3", "h1"}
    assert topo.nodes["r1"].role == "router"
    assert topo.nodes["h1"].role == "host"
    assert topo.nodes["h1"].image == "alpine:3.20"
    assert len(topo.links) == 4
    assert topo.neighbors("r3") == {"r2", "r1", "h1"}


def test_connectivity_and_spof(fixtures: Path) -> None:
    topo = load_topology(fixtures / "topology.clab.yml")
    assert topo.is_connected(["r1", "r2", "r3", "h1"])
    assert topo.single_link_partitions(["r1", "r2", "r3"]) == []
    weak = topo.single_link_partitions(["r1", "h1"])
    assert [link.label() for link in weak] == ["r3:eth3<->h1:eth1"]
    assert topo.is_connected([]) is True


def test_resource_shape(fixtures: Path) -> None:
    resource = load_topology(fixtures / "topology.clab.yml").to_resource()
    assert resource["name"] == "fixture"
    assert {"a": "r1:eth1", "b": "r2:eth1"} in resource["links"]


def test_parse_rejects_bad_links() -> None:
    with pytest.raises(ValueError):
        parse_topology(
            {"name": "x", "topology": {"nodes": {"a": {}}, "links": [{"endpoints": ["a:eth1"]}]}}
        )
    with pytest.raises(ValueError):
        parse_topology(
            {
                "name": "x",
                "topology": {"nodes": {"a": {}}, "links": [{"endpoints": ["a:eth1", "b:eth1"]}]},
            }
        )
