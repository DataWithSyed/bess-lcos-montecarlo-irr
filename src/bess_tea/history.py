"""Multi-year DE-LU day-ahead arbitrage history (2018/19 → 2025/26) in real 2024 US$.

The 2026-11-12 market path used one 12-month window (2025-11 → 2026-10), nominal
EUR at contemporaneous FX and daily perfect foresight. This module asks three
follow-up questions with real public data only:

1. **How typical was that window?** Eight Nov→Oct windows of Energy-Charts /
   SMARD DE-LU hourly day-ahead prices (CC BY 4.0), deflated to 2024 EUR with the
   German HICP (Eurostat prc_hicp_minr, 2015 = 100) and converted at the 2024
   average ECB USD/EUR rate, so every window is in the model's real-2024-US$ basis.
2. **How much of the perfect-foresight value survives a causal rule?** The
   ``persistence`` policy picks charge / discharge hours from day D-1's prices
   (same local clock hour; known before the D-1 12:00 gate) and is settled at
   day D's realised prices. It can lose money on a day. It is a simple lower
   benchmark, not a forecast model.
3. **What does year-to-year revenue variability do to the 15-year IRR?** A
   year bootstrap draws one historical window (i.i.d., with replacement) for
   every operating year; a "regime" variant holds one window for all 15 years.

Fees, imbalance, grid charges and the capacity payment are as in ``market.py``:
no fees; the 50 $/kW-yr capacity payment stays SYNTHETIC wherever it is used.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from .cashflow import augmentation_price_per_kwh, build_cashflows, capex_usd, irr_vectorised, npv
from .market import DEFAULT_PRICES, _best_cycle
from .physics import energy_profile

ROOT = Path(__file__).resolve().parents[2]
PROCESSED = ROOT / "data" / "processed"
HISTORY_PRICES = PROCESSED / "de_lu_da_hourly_2018_2025.csv.gz"
FX_HICP = PROCESSED / "fx_hicp_monthly.csv"
BASE_YEAR = 2024
FIRST_WINDOW = 2018  # 2018-11 → 2019-10; DE-LU zone exists from 2018-10-01
LAST_WINDOW = 2025   # 2025-11 → 2026-10 (the 2026-11-12 window)


def window_label(start_year: int) -> str:
    return f"{start_year}/{str(start_year + 1)[-2:]}"


def load_history(paths: tuple[Path, ...] = (HISTORY_PRICES, DEFAULT_PRICES)) -> pd.DataFrame:
    frames = []
    for path in paths:
        df = pd.read_csv(path)
        df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True).dt.tz_convert("Europe/Berlin")
        frames.append(df[["timestamp", "da_price_eur_mwh"]])
    df = pd.concat(frames).drop_duplicates("timestamp").sort_values("timestamp").reset_index(drop=True)
    if df["da_price_eur_mwh"].isna().any():
        raise ValueError("missing DA prices")
    local = df["timestamp"].dt.tz_localize(None)
    df["window"] = np.where(local.dt.month >= 11, local.dt.year, local.dt.year - 1)
    return df


def load_fx_hicp(path: Path = FX_HICP) -> pd.DataFrame:
    m = pd.read_csv(path)
    m["month"] = pd.PeriodIndex(m["month"].astype(str), freq="M")
    return m


def to_real_usd(prices: pd.DataFrame, fx_hicp: pd.DataFrame | None = None, base_year: int = BASE_YEAR) -> pd.DataFrame:
    """Nominal EUR/MWh → real ``base_year`` EUR (German HICP) → USD at the base-year mean ECB rate."""
    m = load_fx_hicp() if fx_hicp is None else fx_hicp
    base = m[m["month"].dt.year == base_year]
    if len(base) != 12:
        raise ValueError(f"need 12 months of FX/HICP for {base_year}")
    hicp_base = float(base["hicp_de_2015_100"].mean())
    fx_base = float(base["usd_per_eur"].mean())
    out = prices.copy()
    out["month"] = out["timestamp"].dt.tz_localize(None).dt.to_period("M")
    out = out.merge(m[["month", "hicp_de_2015_100", "usd_per_eur"]], on="month", how="left", validate="many_to_one")
    if out["hicp_de_2015_100"].isna().any():
        bad = out.loc[out["hicp_de_2015_100"].isna(), "month"].astype(str).unique().tolist()
        raise ValueError(f"missing HICP for {bad}")
    out["deflator"] = hicp_base / out["hicp_de_2015_100"]
    out["da_price_usd_mwh"] = out["da_price_eur_mwh"] * out["deflator"] * fx_base
    out.attrs.update(hicp_base=hicp_base, fx_base=fx_base)
    return out


def _persistence_forecast(day: pd.DataFrame, prev: pd.DataFrame | None) -> np.ndarray | None:
    """Day D-1 price at the same local clock hour (DST: missing hour → D-1 mean)."""
    if prev is None or prev.empty:
        return None
    by_hour = prev.groupby(prev["timestamp"].dt.hour)["da_price_usd_mwh"].mean()
    hrs = day["timestamp"].dt.hour
    return hrs.map(by_hour).fillna(float(prev["da_price_usd_mwh"].mean())).to_numpy(dtype=float)


def dispatch_window(
    prices: pd.DataFrame,
    power_mw: float,
    duration_h: float,
    rte: float,
    policy: str = "perfect",
) -> pd.DataFrame:
    """Daily dispatch on ``da_price_usd_mwh``. ``policy`` = 'perfect' or 'persistence'."""
    if policy not in ("perfect", "persistence"):
        raise ValueError(policy)
    df = prices.copy()
    df["date"] = df["timestamp"].dt.date
    days = list(df.groupby("date", sort=True))
    rows = []
    prev = None
    for date, day in days:
        day = day.reset_index(drop=True)
        decide = day
        if policy == "persistence":
            fc = _persistence_forecast(day, prev)
            if fc is None:
                prev = day
                rows.append({"date": date, "cycled": False, "discharge_mwh": 0.0, "charge_mwh": 0.0,
                             "revenue_usd": 0.0, "charge_cost_usd": 0.0, "net_usd": 0.0, "skipped_no_history": True})
                continue
            decide = day.assign(da_price_usd_mwh=fc)
        cyc = _best_cycle(decide, power_mw=power_mw, duration_h=duration_h, rte=rte)
        prev = day
        if cyc is None:
            rows.append({"date": date, "cycled": False, "discharge_mwh": 0.0, "charge_mwh": 0.0,
                         "revenue_usd": 0.0, "charge_cost_usd": 0.0, "net_usd": 0.0, "skipped_no_history": False})
            continue
        actual = dict(zip(day["timestamp"].to_numpy(), day["da_price_usd_mwh"].to_numpy(dtype=float)))
        rev = float(sum(actual[h] * e for h, e in zip(cyc["discharge_hours"], cyc["discharge_mwh"])))
        cost = float(sum(actual[h] * e for h, e in zip(cyc["charge_hours"], cyc["charge_mwh"])))
        rows.append({"date": date, "cycled": True, "discharge_mwh": float(sum(cyc["discharge_mwh"])),
                     "charge_mwh": float(sum(cyc["charge_mwh"])), "revenue_usd": rev, "charge_cost_usd": cost,
                     "net_usd": rev - cost, "skipped_no_history": False})
    return pd.DataFrame(rows)


def window_stats(daily: pd.DataFrame, power_mw: float, duration_h: float) -> dict:
    e_dis = float(daily["discharge_mwh"].sum())
    e_chg = float(daily["charge_mwh"].sum())
    rev = float(daily["revenue_usd"].sum())
    cost = float(daily["charge_cost_usd"].sum())
    return {
        "n_days": int(len(daily)),
        "n_days_cycled": int(daily["cycled"].sum()),
        "n_loss_days": int((daily["net_usd"] < 0).sum()),
        "discharged_mwh": e_dis,
        "cycles": e_dis / (power_mw * duration_h),
        "vwap_discharge_usd_mwh": rev / e_dis if e_dis else float("nan"),
        "vwap_charge_usd_mwh": cost / e_chg if e_chg else float("nan"),
        "net_usd": rev - cost,
        "net_usd_per_kw_yr": (rev - cost) / (power_mw * 1_000.0),
    }


def year_varying_flows(p: dict, dis_price: np.ndarray, chg_price: np.ndarray, cycles: np.ndarray) -> np.ndarray:
    """Net cash flows (draws x (n+1)) when price/cycle inputs vary by operating year.

    Mirrors ``cashflow.build_cashflows`` exactly: discharged energy is linear in
    cycles (energy_profile with 1 cycle/yr gives the per-cycle SOH-scaled energy),
    augmentation does not depend on cycles (fade is calendar-linear).
    """
    dis_price = np.atleast_2d(dis_price)
    chg_price = np.atleast_2d(chg_price)
    cycles = np.atleast_2d(cycles)
    n = int(p["lifetime_years"])
    if dis_price.shape[1] != n:
        raise ValueError("inputs must have one column per operating year")
    unit = dict(p, cycles_per_year=1.0)
    prof = energy_profile(unit)
    capex = capex_usd(p)
    years = np.arange(1, n + 1)
    cal = int(p["build_year"]) + years
    aug_cost = np.array([prof["aug_mwh"][i] * 1_000.0 * augmentation_price_per_kwh(p, int(cal[i])) for i in range(n)])
    dis = cycles * prof["discharged_mwh"][None, :]
    chg = dis / p["round_trip_efficiency"]
    cap = np.full(n, p["capacity_revenue_per_kw_yr"] * p["power_mw"] * 1_000.0)
    if p.get("capacity_credit_tracks_soh", False):
        cap = cap * np.minimum(prof["soh"], 1.0)
    op = dis * dis_price - chg * chg_price + cap[None, :] - p["fixed_om_pct_capex"] * capex - dis * p["variable_om_per_mwh"] - aug_cost[None, :]
    op[:, -1] += p["residual_value_pct_capex"] * capex
    return np.hstack([np.full((op.shape[0], 1), -capex), op])


def constant_window_metrics(p: dict, st: dict, capacity: bool) -> dict:
    q = dict(p)
    q["discharge_price_per_mwh"] = st["vwap_discharge_usd_mwh"]
    q["charge_price_per_mwh"] = st["vwap_charge_usd_mwh"]
    q["cycles_per_year"] = st["cycles"]
    if not capacity:
        q["capacity_revenue_per_kw_yr"] = 0.0
    flows = build_cashflows(q)["net_cash_flow"].to_numpy()
    r = irr_vectorised(flows[None, :])[0]
    return {"irr": float(r), "npv_usd": npv(p["discount_rate"], flows)}


def year_bootstrap(p: dict, stats: pd.DataFrame, n_draws: int, seed: int, capacity: bool,
                   outer: int = 200, inner: int = 2000) -> dict:
    """i.i.d. bootstrap of historical windows across the 15 operating years.

    ``irr_p50_ci95_window_resample`` and ``p_irr_above_hurdle_ci95_window_resample`` come from an outer bootstrap
    over the windows (``outer`` resamples x ``inner`` draws): they express how
    little 8 years of history pin down, not Monte Carlo noise.
    """
    rng = np.random.default_rng(seed)
    n = int(p["lifetime_years"])
    idx = rng.integers(0, len(stats), size=(n_draws, n))
    dp = stats["vwap_discharge_usd_mwh"].to_numpy()[idx]
    cp = stats["vwap_charge_usd_mwh"].to_numpy()[idx]
    cy = stats["cycles"].to_numpy()[idx]
    q = dict(p)
    if not capacity:
        q["capacity_revenue_per_kw_yr"] = 0.0
    flows = year_varying_flows(q, dp, cp, cy)
    irrs = irr_vectorised(flows)
    t = np.arange(flows.shape[1])
    npvs = (flows / (1.0 + p["discount_rate"]) ** t).sum(axis=1)
    hurdle = float(p.get("hurdle_irr", 0.08))
    ok = ~np.isnan(irrs)
    # Undefined IRR = the draw never recovers capex (no sign change down to -99 %).
    # Percentiles treat it as -100 % instead of dropping it (dropping biases them up).
    irr_f = np.where(ok, irrs, -1.0)
    # Epistemic CI: with only len(stats) historical windows, resample the windows
    # themselves (outer bootstrap), then redraw operating years (inner draws).
    rng2 = np.random.default_rng(seed + 1)
    boot_p, boot_med = [], []
    m = len(stats)
    for _ in range(outer):
        pick = rng2.integers(0, m, m)
        idx_inner = rng2.integers(0, m, size=(inner, n))
        sub = pick[idx_inner]
        fl = year_varying_flows(q, stats["vwap_discharge_usd_mwh"].to_numpy()[sub],
                                stats["vwap_charge_usd_mwh"].to_numpy()[sub], stats["cycles"].to_numpy()[sub])
        ir = irr_vectorised(fl)
        ir = np.where(np.isnan(ir), -1.0, ir)
        boot_p.append(float(np.mean(ir > hurdle)))
        boot_med.append(float(np.median(ir)))
    return {
        "n_draws": n_draws,
        "irr_defined_share": float(ok.mean()),
        "irr_p10": float(np.percentile(irr_f, 10)),
        "irr_p50": float(np.percentile(irr_f, 50)),
        "irr_p90": float(np.percentile(irr_f, 90)),
        "irr_p50_ci95_window_resample": [float(np.percentile(boot_med, 2.5)), float(np.percentile(boot_med, 97.5))],
        "npv_p10_usd": float(np.percentile(npvs, 10)),
        "npv_p50_usd": float(np.percentile(npvs, 50)),
        "npv_p90_usd": float(np.percentile(npvs, 90)),
        "p_irr_above_hurdle": float(np.mean(np.nan_to_num(irrs, nan=-1.0) > hurdle)),
        "p_irr_above_hurdle_ci95_window_resample": [float(np.percentile(boot_p, 2.5)), float(np.percentile(boot_p, 97.5))],
        "p_npv_positive": float(np.mean(npvs > 0)),
        "_irrs": irrs,
    }
