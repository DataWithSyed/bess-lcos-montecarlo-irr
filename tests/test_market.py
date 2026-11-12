import sys
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from bess_tea.config import base_params, load_config


@pytest.fixture
def p():
    return base_params(load_config())
from bess_tea.market import (
    WINDOW_END,
    WINDOW_START,
    _best_cycle,
    attach_usd,
    dispatch_path,
    load_da_prices,
    load_fx,
    summarise_path,
)


BERLIN = ZoneInfo("Europe/Berlin")


def _hour(h, price, day="2026-01-15"):
    ts = pd.Timestamp(f"{day} {h:02d}:00", tz=BERLIN)
    return {"timestamp": ts, "da_price_usd_mwh": price}


def test_profitable_cycle_picks_expensive_discharge_and_cheap_charge():
    # 6 hours: cheap 10, 20 then mid 50, 60 then expensive 100, 120
    rows = [
        _hour(0, 10),
        _hour(1, 20),
        _hour(2, 50),
        _hour(3, 60),
        _hour(4, 100),
        _hour(5, 120),
    ]
    day = pd.DataFrame(rows)
    cyc = _best_cycle(day, power_mw=100.0, duration_h=2.0, rte=0.85)
    assert cyc is not None
    # discharge 200 MWh at 100 and 120; charge 200/0.85 MWh from the cheapest hours
    assert pytest.approx(sum(cyc["discharge_mwh"])) == 200.0
    assert pytest.approx(sum(cyc["charge_mwh"])) == 200.0 / 0.85
    dis_p = sorted(cyc["discharge_prices"])
    assert dis_p == pytest.approx([100.0, 120.0])
    assert cyc["net_usd"] > 0


def test_unprofitable_flat_day_does_not_cycle():
    rows = [_hour(h, 50.0) for h in range(8)]
    day = pd.DataFrame(rows)
    assert _best_cycle(day, power_mw=100.0, duration_h=2.0, rte=0.85) is None


def test_attach_usd_uses_month_fx():
    fx = pd.DataFrame({"month": pd.PeriodIndex(["2026-01"], freq="M"), "usd_per_eur": [2.0]})
    prices = pd.DataFrame(
        {
            "timestamp": [pd.Timestamp("2026-01-02 12:00", tz=BERLIN)],
            "da_price_eur_mwh": [100.0],
        }
    )
    out = attach_usd(prices, fx)
    assert out["da_price_usd_mwh"].iloc[0] == pytest.approx(200.0)


def test_shipped_window_is_twelve_months_and_complete():
    prices = load_da_prices()
    assert len(prices) == 8760
    start = pd.Timestamp(WINDOW_START, tz=BERLIN)
    end = pd.Timestamp(WINDOW_END, tz=BERLIN)
    assert prices["timestamp"].min() >= start
    assert prices["timestamp"].max() < end
    assert prices["da_price_eur_mwh"].notna().all()
    fx = load_fx()
    assert set(fx["month"].astype(str)) == {
        "2025-11", "2025-12", "2026-01", "2026-02", "2026-03", "2026-04",
        "2026-05", "2026-06", "2026-07", "2026-08", "2026-09", "2026-10",
    }


def test_summarise_does_not_overwrite_synthetic_base(p):
    # A one-day profitable path should change market prices but leave the
    # original config object untouched.
    prices = attach_usd(load_da_prices().head(48))  # two days, just for the FX join test shape
    # use a tiny constructed week instead of the two days (which may include unprofitable days)
    rows = []
    for d in (15, 16):
        for h, pr in enumerate([10, 15, 20, 40, 80, 120, 90, 30]):
            rows.append(_hour(h, pr, day=f"2026-01-{d:02d}"))
    toy = pd.DataFrame(rows)
    toy["da_price_eur_mwh"] = toy["da_price_usd_mwh"]  # unused in dispatch_path
    toy["usd_per_eur"] = 1.0
    daily = dispatch_path(toy, power_mw=p["power_mw"], duration_h=p["duration_h"], rte=p["round_trip_efficiency"])
    before = (p["discharge_price_per_mwh"], p["charge_price_per_mwh"], p["cycles_per_year"])
    out = summarise_path(daily, toy, p)
    assert (p["discharge_price_per_mwh"], p["charge_price_per_mwh"], p["cycles_per_year"]) == before
    assert out["n_days_cycled"] == 2
    assert out["tea_synthetic_with_capacity"]["irr"] == pytest.approx(
        __import__("bess_tea.cashflow", fromlist=["project_metrics"]).project_metrics(p)["irr"]
    )
