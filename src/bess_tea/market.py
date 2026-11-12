"""Energy-Charts DE-LU day-ahead arbitrage path (CC BY 4.0).

Complements the synthetic 115/35 $/MWh revenue case. Daily TTF/capacity prices
are not used; the capacity/ancillary payment stays SYNTHETIC.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from .cashflow import project_metrics
from .lcos import lcos
from .config import base_params, load_config

ROOT = Path(__file__).resolve().parents[2]
PROCESSED = ROOT / "data" / "processed"
DEFAULT_PRICES = PROCESSED / "de_lu_da_hourly.csv.gz"
DEFAULT_FX = PROCESSED / "ecb_usd_eur_monthly.csv"

WINDOW_START = "2025-11-01"
WINDOW_END = "2026-11-01"  # exclusive, Europe/Berlin


def load_da_prices(path: Path = DEFAULT_PRICES) -> pd.DataFrame:
    df = pd.read_csv(path, parse_dates=["timestamp"])
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True).dt.tz_convert("Europe/Berlin")
    if df["da_price_eur_mwh"].isna().any():
        raise ValueError("DA price series contains missing values")
    return df.sort_values("timestamp").reset_index(drop=True)


def load_fx(path: Path = DEFAULT_FX) -> pd.DataFrame:
    fx = pd.read_csv(path)
    fx["month"] = pd.PeriodIndex(fx["month"].astype(str), freq="M")
    return fx


def attach_usd(prices: pd.DataFrame, fx: pd.DataFrame | None = None) -> pd.DataFrame:
    """Convert EUR/MWh to USD/MWh with the month's ECB USD/EUR average.

    ``usd_per_eur`` is dollars per euro, so p_usd = p_eur * usd_per_eur.
    """
    fx = load_fx() if fx is None else fx
    out = prices.copy()
    ts = out["timestamp"].dt.tz_convert("Europe/Berlin")
    out["month"] = ts.dt.tz_localize(None).dt.to_period("M")
    out = out.merge(fx[["month", "usd_per_eur"]], on="month", how="left", validate="many_to_one")
    if out["usd_per_eur"].isna().any():
        missing = out.loc[out["usd_per_eur"].isna(), "month"].astype(str).unique().tolist()
        raise ValueError(f"Missing ECB FX for months: {missing}")
    out["da_price_usd_mwh"] = out["da_price_eur_mwh"] * out["usd_per_eur"]
    return out


def _best_cycle(day: pd.DataFrame, power_mw: float, duration_h: float, rte: float) -> dict | None:
    """One daily cycle with daily foresight: discharge ``duration_h`` full-power hours
    and charge enough cheapest remaining hours to cover E_dis / RTE (partial last hour ok).

    Hours may be in any clock order (ASSUMED SoC can start the day high or low).
    Returns None if the cycle would lose money or there is not enough energy.
    """
    if rte <= 0 or rte > 1:
        raise ValueError("RTE must be in (0, 1]")
    if len(day) < 2:
        return None
    prices = day["da_price_usd_mwh"].to_numpy(dtype=float)
    hours = day["timestamp"].to_numpy()
    n = len(prices)
    e_dis_target = power_mw * duration_h
    e_chg_target = e_dis_target / rte
    # Need at least one discharge hour and enough charge hours besides it
    n_dis = min(int(np.ceil(duration_h)), n - 1)
    if n_dis < 1:
        return None

    # Candidate discharge hours: the most expensive n_dis hours, but we will
    # drop from the cheap end of that set if charge energy cannot be filled.
    order_exp = np.argsort(-prices)
    dis_idx = list(order_exp[:n_dis])
    remaining = [i for i in range(n) if i not in set(dis_idx)]
    if not remaining:
        return None

    def fill_charge(avail: list[int]) -> tuple[list[int], list[float]] | None:
        if not avail:
            return None
        cheap = sorted(avail, key=lambda i: prices[i])
        ch_idx, ch_mwh = [], []
        need = e_chg_target
        for i in cheap:
            take = min(power_mw, need)
            if take <= 0:
                break
            ch_idx.append(i)
            ch_mwh.append(take)
            need -= take
        if need > 1e-9:
            return None
        return ch_idx, ch_mwh

    filled = fill_charge(remaining)
    if filled is None:
        return None
    ch_idx, ch_mwh = filled
    dis_mwh = [power_mw] * len(dis_idx)
    # If duration_h is not integer, last discharge hour may be partial
    if duration_h != n_dis:
        dis_mwh[-1] = power_mw * (duration_h - (n_dis - 1))

    rev = float(sum(prices[i] * e for i, e in zip(dis_idx, dis_mwh)))
    cost = float(sum(prices[i] * e for i, e in zip(ch_idx, ch_mwh)))
    if rev - cost <= 0:
        return None
    return {
        "discharge_hours": hours[dis_idx],
        "charge_hours": hours[ch_idx],
        "discharge_mwh": dis_mwh,
        "charge_mwh": ch_mwh,
        "discharge_prices": [float(prices[i]) for i in dis_idx],
        "charge_prices": [float(prices[i]) for i in ch_idx],
        "revenue_usd": rev,
        "charge_cost_usd": cost,
        "net_usd": rev - cost,
    }


def dispatch_path(
    prices: pd.DataFrame,
    power_mw: float = 100.0,
    duration_h: float = 2.0,
    rte: float = 0.85,
) -> pd.DataFrame:
    """Day-by-day dispatch. ``prices`` must already have ``da_price_usd_mwh``."""
    df = prices.copy()
    df["date"] = df["timestamp"].dt.tz_convert("Europe/Berlin").dt.date
    rows = []
    for date, day in df.groupby("date", sort=True):
        cyc = _best_cycle(day, power_mw=power_mw, duration_h=duration_h, rte=rte)
        if cyc is None:
            rows.append(
                {
                    "date": date,
                    "cycled": False,
                    "discharge_mwh": 0.0,
                    "charge_mwh": 0.0,
                    "revenue_usd": 0.0,
                    "charge_cost_usd": 0.0,
                    "net_usd": 0.0,
                    "vwap_discharge_usd": float("nan"),
                    "vwap_charge_usd": float("nan"),
                }
            )
            continue
        e_dis = float(sum(cyc["discharge_mwh"]))
        e_chg = float(sum(cyc["charge_mwh"]))
        rows.append(
            {
                "date": date,
                "cycled": True,
                "discharge_mwh": e_dis,
                "charge_mwh": e_chg,
                "revenue_usd": cyc["revenue_usd"],
                "charge_cost_usd": cyc["charge_cost_usd"],
                "net_usd": cyc["net_usd"],
                "vwap_discharge_usd": cyc["revenue_usd"] / e_dis,
                "vwap_charge_usd": cyc["charge_cost_usd"] / e_chg,
            }
        )
    return pd.DataFrame(rows)


def summarise_path(daily: pd.DataFrame, prices: pd.DataFrame, p: dict | None = None) -> dict:
    """Volume-weighted prices, throughput and a TEA case that uses them.

    Availability is applied when mapping onto the 15-year model (same as the
    synthetic case), not inside the hourly dispatch.
    """
    p = base_params(load_config()) if p is None else p
    cycled = daily[daily["cycled"]]
    e_dis = float(daily["discharge_mwh"].sum())
    e_chg = float(daily["charge_mwh"].sum())
    rev = float(daily["revenue_usd"].sum())
    cost = float(daily["charge_cost_usd"].sum())
    n_days = int(len(daily))
    n_cycled = int(cycled.shape[0])
    vwap_dis = rev / e_dis if e_dis else float("nan")
    vwap_chg = cost / e_chg if e_chg else float("nan")
    # Equivalent cycles before availability, on nameplate energy
    e_nom = p["power_mw"] * p["duration_h"]
    cycles_raw = e_dis / e_nom if e_nom else float("nan")
    # Historical dispatch assumes the unit was available in every window hour.
    # The 15-year TEA still applies the 97% availability haircut, as in the
    # synthetic case, so cycles_per_year equals the raw historical cycle count.
    cycles_for_tea = cycles_raw

    market_p = dict(p)
    market_p["discharge_price_per_mwh"] = vwap_dis
    market_p["charge_price_per_mwh"] = vwap_chg
    market_p["cycles_per_year"] = cycles_for_tea
    # capacity revenue remains the synthetic default unless the caller changes it
    metrics_with_cap = project_metrics(market_p)
    metrics_with_cap["lcos_usd_per_mwh"] = float(lcos(market_p))
    zero_cap = dict(market_p)
    zero_cap["capacity_revenue_per_kw_yr"] = 0.0
    metrics_arb_only = project_metrics(zero_cap)
    metrics_arb_only["lcos_usd_per_mwh"] = float(lcos(zero_cap))
    synth = dict(p)
    metrics_synth = project_metrics(synth)
    metrics_synth["lcos_usd_per_mwh"] = float(lcos(synth))
    synth_arb = dict(p)
    synth_arb["capacity_revenue_per_kw_yr"] = 0.0
    metrics_synth_arb = project_metrics(synth_arb)
    metrics_synth_arb["lcos_usd_per_mwh"] = float(lcos(synth_arb))

    fx_mean = float(prices["usd_per_eur"].mean())
    return {
        "window": f"{WINDOW_START} to {WINDOW_END} Europe/Berlin (exclusive end)",
        "n_hours": int(len(prices)),
        "n_days": n_days,
        "n_days_cycled": n_cycled,
        "share_days_cycled": n_cycled / n_days if n_days else float("nan"),
        "da_mean_eur_mwh": float(prices["da_price_eur_mwh"].mean()),
        "da_min_eur_mwh": float(prices["da_price_eur_mwh"].min()),
        "da_max_eur_mwh": float(prices["da_price_eur_mwh"].max()),
        "ecb_usd_per_eur_unweighted_mean": fx_mean,
        "discharged_mwh": e_dis,
        "charged_mwh": e_chg,
        "cycles_raw": cycles_raw,
        "cycles_per_year_for_tea": cycles_for_tea,
        "vwap_discharge_usd_mwh": vwap_dis,
        "vwap_charge_usd_mwh": vwap_chg,
        "spread_usd_mwh": vwap_dis - vwap_chg,
        "arbitrage_net_usd": rev - cost,
        "arbitrage_net_usd_per_kw_yr": (rev - cost) / (p["power_mw"] * 1_000.0),
        "synthetic_discharge_usd_mwh": p["discharge_price_per_mwh"],
        "synthetic_charge_usd_mwh": p["charge_price_per_mwh"],
        "synthetic_spread_usd_mwh": p["discharge_price_per_mwh"] - p["charge_price_per_mwh"],
        "synthetic_year1_discharged_mwh": p["power_mw"] * p["duration_h"] * p["cycles_per_year"] * p["availability"],
        "tea_market_with_synthetic_capacity": metrics_with_cap,
        "tea_market_arbitrage_only": metrics_arb_only,
        "tea_synthetic_with_capacity": metrics_synth,
        "tea_synthetic_arbitrage_only": metrics_synth_arb,
        "capacity_revenue_note": (
            "The 50 $/kW-yr capacity/ancillary payment is still SYNTHETIC and is "
            "unchanged in the 'with_synthetic_capacity' cases. Arbitrage-only cases set it to 0."
        ),
        "attribution": (
            "Energy-Charts / SMARD DE-LU day-ahead prices, CC BY 4.0. "
            "ECB USD/EUR monthly averages (as used in epm-data-pipeline)."
        ),
    }
