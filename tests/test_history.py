import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from bess_tea.cashflow import build_cashflows
from bess_tea.config import base_params, load_config
from bess_tea.history import (
    constant_window_metrics,
    dispatch_window,
    load_history,
    to_real_usd,
    window_stats,
    year_bootstrap,
    year_varying_flows,
)
from bess_tea.market import load_da_prices


@pytest.fixture
def p():
    return base_params(load_config())


@pytest.mark.parametrize("cap,track", [(50.0, False), (0.0, False), (50.0, True)])
def test_year_varying_flows_match_build_cashflows(p, cap, track):
    q = dict(p, capacity_revenue_per_kw_yr=cap, capacity_credit_tracks_soh=track,
             discharge_price_per_mwh=180.0, charge_price_per_mwh=40.0, cycles_per_year=310.0)
    n = int(q["lifetime_years"])
    ref = build_cashflows(q)["net_cash_flow"].to_numpy()
    got = year_varying_flows(q, np.full(n, 180.0), np.full(n, 40.0), np.full(n, 310.0))[0]
    np.testing.assert_allclose(got, ref, rtol=1e-10, atol=1e-6)


def test_to_real_usd_deflates_and_converts():
    months = pd.period_range("2024-01", "2024-12", freq="M").append(pd.PeriodIndex(["2026-01"], freq="M"))
    m = pd.DataFrame({"month": months, "usd_per_eur": [1.1] * 12 + [1.3], "hicp_de_2015_100": [120.0] * 12 + [132.0]})
    ts = pd.to_datetime(["2024-06-01 10:00", "2026-01-05 10:00"]).tz_localize("Europe/Berlin")
    out = to_real_usd(pd.DataFrame({"timestamp": ts, "da_price_eur_mwh": [100.0, 110.0]}), m)
    # 2024: unchanged EUR, converted at the 2024 mean rate; 2026: 110 * 120/132 = 100 real EUR
    np.testing.assert_allclose(out["da_price_usd_mwh"], [110.0, 110.0])


def _days(prices_by_day):
    rows = []
    for d, prices in prices_by_day:
        for h, v in enumerate(prices):
            rows.append({"timestamp": pd.Timestamp(d, tz="Europe/Berlin") + pd.Timedelta(hours=h), "da_price_usd_mwh": v})
    return pd.DataFrame(rows)


def test_persistence_equals_perfect_when_days_repeat():
    shape = [30, 20, 10, 15, 40, 80, 120, 90] + [60] * 16
    df = _days([("2024-03-04", shape), ("2024-03-05", shape), ("2024-03-06", shape)])
    perf = dispatch_window(df, 100.0, 2.0, 0.85, "perfect")
    pers = dispatch_window(df, 100.0, 2.0, 0.85, "persistence")
    assert not pers.loc[0, "cycled"] and pers.loc[0, "skipped_no_history"]
    np.testing.assert_allclose(pers["net_usd"].iloc[1:], perf["net_usd"].iloc[1:])


def test_persistence_can_lose_money_when_shape_flips():
    up = [10.0] * 12 + [100.0] * 12
    df = _days([("2024-03-04", up), ("2024-03-05", up[::-1])])
    pers = dispatch_window(df, 100.0, 2.0, 0.85, "persistence")
    assert pers.loc[1, "cycled"] and pers.loc[1, "net_usd"] < 0
    st = window_stats(pers, 100.0, 2.0)
    assert st["n_loss_days"] == 1


def test_shipped_history_is_complete_and_consistent():
    h = load_history()
    counts = h.groupby("window").size()
    assert list(counts.index) == list(range(2018, 2026))
    assert set(counts.values) <= {8760, 8784}
    assert h["timestamp"].is_unique
    # The 2025/26 window is the 2026-11-12 file, unchanged
    old = load_da_prices()
    w = h[h["window"] == 2025].reset_index(drop=True)
    np.testing.assert_allclose(w["da_price_eur_mwh"].to_numpy(), old["da_price_eur_mwh"].to_numpy())


def test_single_window_bootstrap_collapses_to_constant_case(p):
    st = {"vwap_discharge_usd_mwh": 200.0, "vwap_charge_usd_mwh": 50.0, "cycles": 330.0}
    one = pd.DataFrame([st])
    r = year_bootstrap(p, one, n_draws=50, seed=1, capacity=True, outer=3, inner=20)
    c = constant_window_metrics(p, st, capacity=True)
    assert r["irr_p10"] == pytest.approx(r["irr_p90"])
    assert r["irr_p50"] == pytest.approx(c["irr"], abs=1e-6)
