# Walking-graph review — 12 October 2026

Base: `035d91bb9ed127e79f6adf4dc0c106f16289da53` (the existing priority/OD review). This change preserves those fixes and corrects how the walking graph handles alternative paths. It is a graph-function review using synthetic Parquet inputs, not a new estimate of Singapore's coverage.

## Problem and behavior

`coo_matrix(...).tocsr()` sums duplicate row/column entries ([SciPy documentation](https://docs.scipy.org/doc/scipy-1.14.1/reference/generated/scipy.sparse.coo_matrix.tocsr.html)). That operation is appropriate for many matrices but does not choose the shortest of parallel walking arcs:

- Multiple stops snapping to one walking node gave the virtual source several arcs to that node. Their access distances were summed, although a resident can walk to any one stop.
- Repeated or reversed physical edge records were mirrored and then summed. Parallel paths must use the minimum cost for this shortest-distance calculation.
- Replacing a zero snap distance with 0.001 m excluded a walk of exactly 400 m. An explicit zero-weight sparse arc is traversable; no offset is needed.

Physical edges now use one minimum length per unordered endpoint pair, then receive their reverse arc. Self-loops are represented once. The virtual source uses the minimum access distance per snapped node, including zero. The rules remain **components of at least 1,000 nodes**, **stop snap distance at most 150 m**, **coverage distance at most 400 m**. Parcel-to-node distance remains part of the full walk, and the search remains capped at 401 m.

## Offline evidence

Tests import the production `coverage_walk` module, write real `walk_nodes.parquet`/`walk_edges.parquet` files, and call its normal reader/function. They do not extract AST definitions or mock Parquet reads. The main component in each small fixture has 1,000 nodes; a distant tail makes it eligible without creating a shortcut. Nineteen hand-calculated cases cover shared source nodes, unequal access costs, stop ordering, duplicate/reversed/parallel edges, a self-loop, multiple source nodes, zero snap, both sides of 400 m, parcel access, the 150 m snap boundary, an excluded small component and no source stops.

The same 19 tests run against the unchanged old module in an isolated source copy produced **11 assertion failures, 8 passes, no errors/skips**. The corrected implementation passes all 19 cases. The separate [synthetic evidence file](../outputs/walk_review_20261012.json) records 12 representative before/after cases, actual code/test/input hashes and package versions.

| Synthetic case | Previous graph result | Corrected walk |
|---|---:|---:|
| Two stops each 100 m from one node, then a 280 m path | Unreachable in the capped search | 380 m, covered |
| Stops 100 m and 50 m from one node, then a 280 m path | Unreachable in the capped search | 330 m, covered |
| Reversed duplicate of a 280 m edge, with 100 m stop access | Unreachable in the capped search | 380 m, covered |
| Longer parallel edges listed before the 280 m edge | Unreachable in the capped search | 380 m, covered |
| Zero stop snap plus exactly 400 m | 400.001 m, not covered | 400 m, covered |
| Zero stop snap plus 400.001 m | 400.002 m, not covered | 400.001 m, not covered |

“Unreachable” above is the capped Dijkstra result; it does not assert that no longer route exists. These are known synthetic regression cases, not a representative transport benchmark or independent human annotations.

## Historical data and reproduction

The review checkout has no original `data/processed/walk_nodes.parquet` or `walk_edges.parquet`, and no large OSM/source-data download was performed. The city/population pipeline was not rerun. All existing coverage outputs, the **79.7%** archived walking estimate, and the priority/OD evidence are preserved. The synthetic cases demonstrate a real defect in the graph builder; they do not quantify how often it occurs in the original network or establish a corrected city-wide percentage.

```bash
pip install -r requirements.txt
python -m pytest -q tests/test_coverage_walk.py
python -m pytest -q tests
python tests/mutate.py
```

The full local suite passed **113 tests** (original 94 plus 19 walking cases). The complete mutation gate passed **33/33** variants (original 28 plus five walking defects), with no escapes. It ran on a raw-byte-identical isolated copy of all 18 source/test Python files; the restored copy matched the untouched implementation checkout. The five added variants replace physical min with sum/first, remove undirected normalization, sum shared source access, or restore the zero-snap epsilon. The existing CI workflow runs the same full test/mutation commands; these are local results, not a claim that a new remote head has passed CI. No merge, site publication or deployment is part of this review.

To recalculate the city estimate later, first recover the original versioned walking Parquets, bus-network database, population/subzone and housing inputs; record their hashes and rebuild into a separate output directory. A fresh download of today's OSM/network data would be a different corpus and cannot be described as a controlled correction of the archived 79.7% estimate.
