"""Hand-calculated walks using the real module and actual Parquet inputs.

Every main component has 1,000 nodes, so production thresholds are unchanged.
The far-away tail makes that component eligible without adding a shortcut.
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import coverage_walk as walk  # noqa: E402


def network(tmp_path, monkeypatch, length=280., extra=(), small_component=False):
    x = np.r_[0., length, np.arange(10000., 10998.)]
    nodes = pd.DataFrame({"node": np.arange(1000), "x": x, "y": np.zeros(1000)})
    edges = pd.DataFrame({"u": np.arange(999), "v": np.arange(1, 1000), "length_m": np.diff(x)})
    if extra:
        # Put alternatives first so taking the first record cannot masquerade
        # as a shortest-path reduction when a longer parallel edge is listed.
        edges = pd.concat([pd.DataFrame(extra, columns=["u", "v", "length_m"]), edges], ignore_index=True)
    if small_component:
        nodes = pd.concat([nodes, pd.DataFrame({"node": [2000, 2001], "x": [5000., 5001.], "y": [0., 0.]})], ignore_index=True)
        edges = pd.concat([edges, pd.DataFrame({"u": [2000], "v": [2001], "length_m": [1.]})], ignore_index=True)
    nodes.to_parquet(tmp_path / "walk_nodes.parquet", index=False)
    edges.to_parquet(tmp_path / "walk_edges.parquet", index=False)
    monkeypatch.setattr(walk, "PROC", tmp_path)
    return nodes, edges


def distance(stops, home):
    tree, distances, unsnapped = walk.walk_distance_to_stop(np.asarray(stops, dtype=float).reshape(-1, 2))
    snap, index = tree.query(np.asarray([home], dtype=float))
    value = float(distances[index[0]] + snap[0])
    return value, value <= walk.RADIUS_M, unsnapped


@pytest.mark.parametrize("stops", [
    [[0., 100.], [0., -100.]], [[0., 100.], [0., -50.]],
    [[0., -50.], [0., 100.]], [[0., 100.], [0., 100.]],
])
def test_stops_sharing_a_snap_node_use_nearest_access_not_sum(tmp_path, monkeypatch, stops):
    network(tmp_path, monkeypatch)
    observed, covered, unsnapped = distance(stops, [280., 0.])
    assert observed == pytest.approx(280. + min(abs(y) for _, y in stops))
    assert covered and unsnapped == 0


@pytest.mark.parametrize("extra", [
    [(0, 1, 280.)], [(1, 0, 280.)], [(0, 1, 350.), (1, 0, 500.)],
    [(1, 0, 500.), (0, 1, 350.)], [(0, 0, 10.)],
])
def test_parallel_undirected_edges_use_shortest_path(tmp_path, monkeypatch, extra):
    network(tmp_path, monkeypatch, extra=extra)
    observed, covered, unsnapped = distance([[0., 100.]], [280., 0.])
    best_edge = min([280.] + [length for u, v, length in extra if u != v])
    assert observed == pytest.approx(100. + best_edge)
    assert covered and unsnapped == 0


@pytest.mark.parametrize("length,covered", [(399.999, True), (400., True), (400.001, False)])
def test_zero_snap_and_inclusive_400_m_boundary(tmp_path, monkeypatch, length, covered):
    network(tmp_path, monkeypatch, length=length)
    observed, actual_covered, unsnapped = distance([[0., 0.]], [length, 0.])
    assert observed == pytest.approx(length, abs=1e-9)
    assert actual_covered == covered and unsnapped == 0


def test_multiple_source_nodes_choose_the_nearest_complete_walk(tmp_path, monkeypatch):
    network(tmp_path, monkeypatch)
    observed, covered, unsnapped = distance([[0., 100.], [280., 30.]], [0., 0.])
    assert observed == pytest.approx(100.)  # other stop costs 30 + 280 = 310 m
    assert covered and unsnapped == 0


def test_parcel_snap_distance_is_part_of_the_400_m_walk(tmp_path, monkeypatch):
    network(tmp_path, monkeypatch)
    observed, covered, unsnapped = distance([[0., 100.]], [280., 20.])
    assert observed == pytest.approx(100. + 280. + 20.)
    assert covered and unsnapped == 0


@pytest.mark.parametrize("offset,unsnapped,expected", [(149.999, 0, 399.999), (150., 0, 400.), (150.001, 1, np.inf)])
def test_150_m_stop_snap_limit_is_preserved(tmp_path, monkeypatch, offset, unsnapped, expected):
    network(tmp_path, monkeypatch, length=250.)
    observed, covered, rejected = distance([[0., offset]], [250., 0.])
    assert rejected == unsnapped
    assert observed == pytest.approx(expected)
    assert covered == (unsnapped == 0)


def test_small_component_cannot_supply_a_shortcut(tmp_path, monkeypatch):
    network(tmp_path, monkeypatch, small_component=True)
    observed, covered, unsnapped = distance([[5000., 0.]], [5001., 0.])
    assert np.isinf(observed) and not covered and unsnapped == 1


def test_no_eligible_stops_leaves_nodes_unreachable(tmp_path, monkeypatch):
    network(tmp_path, monkeypatch)
    observed, covered, unsnapped = distance([], [280., 0.])
    assert np.isinf(observed) and not covered and unsnapped == 0
