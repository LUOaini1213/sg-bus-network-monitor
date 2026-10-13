"""Forecasting: time grid, sample windows, day-blocked split, baselines, leakage, bootstrap and the graph net.

All data here is synthetic and only exercises the code; no result in the README comes from these fixtures.
"""
import datetime as dt
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import forecast as fc  # noqa: E402


def T(*s):
    return np.array(s, dtype="datetime64[m]")


def test_grid_keeps_the_closest_snapshot_and_drops_far_ones():
    times = T("2026-10-14T08:02", "2026-10-14T07:59", "2026-10-14T08:31", "2026-10-14T08:49")
    kmh = np.array([[1.0], [2.0], [3.0], [4.0]])
    grid, X = fc.to_grid(times, kmh, step_min=20, max_offset_min=9)
    assert [str(g) for g in grid] == ["2026-10-14T08:00", "2026-10-14T08:20", "2026-10-14T08:40"]
    # 07:59 (1 min off) beats 08:02 for 08:00. 08:31 and 08:49 are both 9 min from 08:40 (the first is kept)
    # and more than 9 min from 08:20 and 09:00, which stay empty.
    np.testing.assert_allclose(X[:, 0], [2.0, np.nan, 3.0], equal_nan=True)


def test_anchors_need_every_input_slot_and_every_target_slot():
    X = np.ones((14, 2))
    X[4] = np.nan  # an empty slot
    got = fc.anchors(X, lags=3, horizons=(1, 2, 3)).tolist()
    # t runs 2..10; slot 4 missing rules out t = 2, 3 (as a target) and t = 4, 5, 6 (as an input).
    assert got == [7, 8, 9, 10]
    X2 = np.ones((10, 2))
    X2[4, 0] = np.nan  # one link missing does not empty the slot
    assert fc.anchors(X2, lags=3, horizons=(1, 2, 3)).tolist() == [2, 3, 4, 5, 6]


def test_split_is_by_whole_days_in_time_order_and_drops_midnight_windows():
    grid = pd.date_range("2026-10-10 00:00", "2026-10-15 23:40", freq="20min").to_numpy().astype("datetime64[m]")
    idx = np.arange(2, len(grid) - 3)
    train, val, test = fc.split(grid, idx, test_from=dt.date(2026, 10, 14), val_days=2)
    days = lambda ix: sorted({str(d) for d in pd.DatetimeIndex(grid[ix]).date})  # noqa: E731
    assert days(train) == ["2026-10-10", "2026-10-11"]
    assert days(val) == ["2026-10-12", "2026-10-13"]
    assert days(test) == ["2026-10-14", "2026-10-15"]
    for part in (train, val, test):
        first = pd.DatetimeIndex(grid[part - 2]).date
        last = pd.DatetimeIndex(grid[part + 3]).date
        assert (first == last).all()
    assert train.max() < val.min() and val.max() < test.min()


def test_historical_average_uses_day_type_then_falls_back():
    grid = pd.date_range("2026-10-10 08:00", periods=4, freq="1D").to_numpy().astype("datetime64[m]")
    # Sat 10th, Sun 11th, Mon 12th, Tue 13th at 08:00; link 1 missing on weekdays.
    X = np.array([[10.0, 50.0], [20.0, 60.0], [30.0, np.nan], [50.0, np.nan]])
    table, tod, wk = fc.ha_table(X, grid, np.array([0, 1, 2, 3]), slots_per_day=72)
    slot = tod[0]
    assert table[0, slot, 0] == pytest.approx(15.0)   # weekend mean
    assert table[1, slot, 0] == pytest.approx(40.0)   # weekday mean
    assert table[1, slot, 1] == pytest.approx(55.0)   # no weekday data: all days at that time
    other = (slot + 1) % 72
    assert table[1, other, 0] == pytest.approx(27.5)  # no data at that time at all: overall mean


def test_neighbour_mean_uses_both_directions_and_ignores_missing():
    # edges 0->1, 1->2: node 1's neighbours are 0 and 2.
    src, dst = np.array([0, 1]), np.array([1, 2])
    x = np.array([[10.0, 99.0, 30.0], [10.0, 99.0, np.nan]])
    nb = fc.neighbour_mean(x, src, dst, 3)
    np.testing.assert_allclose(nb[0], [99.0, 20.0, 99.0])
    np.testing.assert_allclose(nb[1], [99.0, 10.0, 99.0])


def test_graph_joins_successors_but_not_u_turns():
    links = pd.DataFrame({
        "StartLat": [1.3000, 1.3010, 1.3010, 1.3000], "StartLon": [103.80, 103.80, 103.80, 103.81],
        "EndLat": [1.3010, 1.3020, 1.3000, 1.3010], "EndLon": [103.80, 103.80, 103.80, 103.81]})
    # 0 ends where 1 and 2 start; 2 is 0 reversed (U-turn); 3 is elsewhere.
    src, dst = fc.build_graph(links, tol_m=15)
    # 2 -> 0 is also a reversal pair, so the only edge is 0 -> 1.
    assert sorted(zip(src.tolist(), dst.tolist())) == [(0, 1)]


def test_lightgbm_features_never_look_ahead():
    X = np.arange(30, dtype=float).reshape(10, 3)
    nb = X + 100
    tf = np.zeros((10, 3))
    f = fc.lgbm_frame(X, nb, tf, np.array([5]), np.zeros(3), np.ones(3), h=1)
    assert f.lag0.tolist() == X[5].tolist()
    assert f.lag2.tolist() == X[3].tolist()
    assert f.nb_lag1.tolist() == nb[4].tolist()
    feats = f[[c for c in f if c.startswith(("lag", "nb_lag"))]].to_numpy()
    assert feats.max() <= nb[5].max()  # nothing from slot 6 or later


def test_errors_skip_missing_targets_and_rmse_is_root_mean_square():
    pred = np.array([[10.0, 20.0, 30.0], [5.0, 5.0, 5.0]])
    true = np.array([[12.0, np.nan, 26.0], [5.0, 8.0, 5.0]])
    st = fc.errors_by_anchor(pred, true)
    assert st[2].tolist() == [2, 3]
    mae, rmse = fc.mae_rmse(st)
    assert mae == pytest.approx((2 + 4 + 0 + 3 + 0) / 5)
    assert rmse == pytest.approx(np.sqrt((4 + 16 + 9) / 5))


def test_block_bootstrap_draws_contiguous_blocks():
    rng = np.random.default_rng(0)
    idx = fc.block_indices(20, 5, 50, rng)
    assert idx.shape == (50, 20)
    assert idx.min() >= 0 and idx.max() < 20
    for row in idx:
        for b in range(0, 20, 5):
            assert (np.diff(row[b:b + 5]) == 1).all()


def test_paired_bootstrap_interval_of_a_constant_improvement():
    a = np.full(30, 2.0)
    c = np.full(30, 1)
    base = (a + 1.0, a, c)
    st = (a, a, c)
    rng = np.random.default_rng(1)
    ci = fc.boot_ci(st, fc.block_indices(30, 6, 200, rng), base)
    assert ci["mae_lo"] == pytest.approx(2.0) and ci["mae_hi"] == pytest.approx(2.0)
    assert ci["diff_vs_persistence_lo"] == pytest.approx(-1.0)
    assert ci["diff_vs_persistence_hi"] == pytest.approx(-1.0)
    # Varying errors give a real interval, ordered.
    st2 = (np.linspace(1, 3, 30), a, c)
    ci2 = fc.boot_ci(st2, fc.block_indices(30, 6, 500, np.random.default_rng(2)))
    assert 1.0 < ci2["mae_lo"] < 2.0 < ci2["mae_hi"] < 3.0


def test_bootstrap_interval_is_the_2_5_and_97_5_percentiles_of_resampled_mae():
    rng = np.random.default_rng(5)
    a = rng.uniform(0, 10, 40)
    c = rng.integers(1, 5, 40)
    idx = fc.block_indices(40, 4, 300, np.random.default_rng(9))
    maes = a[idx].sum(1) / c[idx].sum(1)
    ci = fc.boot_ci((a, a, c), idx)
    assert ci["mae_lo"] == pytest.approx(np.percentile(maes, 2.5))
    assert ci["mae_hi"] == pytest.approx(np.percentile(maes, 97.5))


torch = pytest.importorskip("torch")
import forecast_gnn as fg  # noqa: E402


def test_transition_rows_sum_to_one():
    a = fg.transition(np.array([0, 0, 1]), np.array([1, 2, 2]), 3, "cpu").to_dense()
    np.testing.assert_allclose(a.sum(1).numpy(), [1.0, 1.0, 0.0])
    assert a[0, 1].item() == pytest.approx(0.5)


def test_gnn_with_a_zero_head_predicts_persistence():
    """The net predicts a change from the last observed speed: with zero output weights it is persistence."""
    rng = np.random.default_rng(0)
    X = rng.choice([5.0, 15.0, 25.0, 35.0], size=(40, 4)).astype(np.float32)
    grid = pd.date_range("2026-10-14", periods=40, freq="20min").to_numpy().astype("datetime64[m]")
    tf = fc.time_features(grid)
    src, dst = np.array([0, 1, 2]), np.array([1, 2, 3])
    train, val, test = np.arange(2, 20), np.arange(20, 28), np.arange(28, 36)
    orig = fg.SpeedGNN.__init__

    def zero_head(self, *a, **k):
        orig(self, *a, **k)
        for p in self.head.parameters():
            torch.nn.init.zeros_(p)
    fg.SpeedGNN.__init__ = zero_head
    try:
        preds, _ = fg.train_and_predict(X, grid, tf, src, dst, train, val, test, X.mean(0), seeds=(0,), epochs=1,
                                        lr=0.0, device="cpu", verbose=False)
    finally:
        fg.SpeedGNN.__init__ = orig
    for h in (1, 2, 3):
        np.testing.assert_allclose(preds["mean"][h], X[test], atol=1e-5)


def test_gnn_learns_an_upstream_signal_that_persistence_cannot_see():
    """Node 1 copies node 0's speed one step later; the graph net should beat persistence on node 1."""
    rng = np.random.default_rng(3)
    n_t = 400
    up = rng.choice([10.0, 30.0, 50.0], size=n_t)
    X = np.c_[up, np.r_[up[0], up[:-1]]].astype(np.float32)
    grid = pd.date_range("2026-10-01", periods=n_t, freq="20min").to_numpy().astype("datetime64[m]")
    tf = fc.time_features(grid)
    train, val, test = np.arange(2, 300), np.arange(300, 350), np.arange(350, 396)
    preds, _ = fg.train_and_predict(X, grid, tf, np.array([0]), np.array([1]), train, val, test, X.mean(0),
                                    horizons=(1,), seeds=(0,), epochs=40, patience=40, device="cpu", verbose=False, batch=32,
                                    lr=5e-3)
    gnn_err = np.abs(preds["mean"][1][:, 1] - X[test + 1, 1]).mean()
    pers_err = np.abs(X[test, 1] - X[test + 1, 1]).mean()
    assert pers_err > 10
    assert gnn_err < 0.5 * pers_err
