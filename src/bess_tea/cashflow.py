"""Annual project cash-flow model (real, pre-tax, unlevered) and metrics."""
from __future__ import annotations

import numpy as np
import pandas as pd

from .physics import energy_profile


def capex_usd(p: dict) -> float:
    """Overnight capex: (energy $/kWh * duration * (1 + overbuild) + power $/kW) * kW * multiplier."""
    per_kw = p["energy_cost_per_kwh"] * p["duration_h"] * (1.0 + p.get("initial_overbuild", 0.0)) + p["power_cost_per_kw"]
    return per_kw * p["cost_multiplier"] * p["power_mw"] * 1_000.0


def augmentation_price_per_kwh(p: dict, calendar_year: int) -> float:
    """NREL energy-cost component scaled by the NREL Mid curve for that year.

    The cost multiplier relative to the Mid curve in the build year is kept, so
    a cheap/expensive build (Monte Carlo) also implies cheap/expensive
    augmentation.
    """
    curve = p["nrel_mid_curve"]
    yr = min(max(calendar_year, min(curve)), max(curve))
    rel = p["cost_multiplier"] / curve[int(p["build_year"])]
    return p["energy_cost_per_kwh"] * curve[yr] * rel


def build_cashflows(p: dict) -> pd.DataFrame:
    """Year-by-year cash flows; row 0 is construction (capex)."""
    n = int(p["lifetime_years"])
    prof = energy_profile(p)
    capex = capex_usd(p)
    years = np.arange(n + 1)
    cal = int(p["build_year"]) + years

    dis = np.r_[0.0, prof["discharged_mwh"]]
    chg = np.r_[0.0, prof["charged_mwh"]]
    aug_mwh = np.r_[0.0, prof["aug_mwh"]]
    aug_cost = np.array([aug_mwh[t] * 1_000.0 * augmentation_price_per_kwh(p, cal[t]) for t in years])

    arb_rev = dis * p["discharge_price_per_mwh"]
    cap_rev = np.r_[0.0, np.full(n, p["capacity_revenue_per_kw_yr"] * p["power_mw"] * 1_000.0)]
    if p.get("capacity_credit_tracks_soh", False):
        # duration-based accreditation: a battery that can no longer hold full power for the rated
        # duration is credited pro rata (ILLUSTRATIVE rule; default off)
        cap_rev[1:] *= np.minimum(prof["soh"], 1.0)
    charge_cost = chg * p["charge_price_per_mwh"]
    fom = np.r_[0.0, np.full(n, p["fixed_om_pct_capex"] * capex)]
    vom = dis * p["variable_om_per_mwh"]
    capex_col = np.r_[capex, np.zeros(n)]
    residual = np.zeros(n + 1)
    residual[-1] = p["residual_value_pct_capex"] * capex

    df = pd.DataFrame(
        {
            "year": years,
            "calendar_year": cal,
            "discharged_mwh": dis,
            "charged_mwh": chg,
            "augmentation_mwh": aug_mwh,
            "arbitrage_revenue": arb_rev,
            "capacity_revenue": cap_rev,
            "charging_cost": -charge_cost,
            "fixed_om": -fom,
            "variable_om": -vom,
            "augmentation": -aug_cost,
            "capex": -capex_col,
            "residual_value": residual,
        }
    )
    flow_cols = [c for c in df.columns if c not in ("year", "calendar_year", "discharged_mwh", "charged_mwh", "augmentation_mwh")]
    df["net_cash_flow"] = df[flow_cols].sum(axis=1)
    df["cumulative_cash_flow"] = df["net_cash_flow"].cumsum()
    return df


def npv(rate: float, flows: np.ndarray) -> float:
    t = np.arange(len(flows))
    return float(np.sum(flows / (1.0 + rate) ** t))


def irr(flows: np.ndarray, lo: float = -0.99, hi: float = 1.0, tol: float = 1e-9) -> float:
    """IRR by bisection; returns NaN if NPV does not change sign on [lo, hi]."""
    f_lo, f_hi = npv(lo, flows), npv(hi, flows)
    if np.sign(f_lo) == np.sign(f_hi):
        return float("nan")
    for _ in range(200):
        mid = 0.5 * (lo + hi)
        f_mid = npv(mid, flows)
        if np.sign(f_mid) == np.sign(f_lo):
            lo, f_lo = mid, f_mid
        else:
            hi = mid
        if hi - lo < tol:
            break
    return 0.5 * (lo + hi)


def irr_vectorised(flows: np.ndarray, lo: float = -0.99, hi: float = 1.0, iters: int = 80) -> np.ndarray:
    """Bisection IRR for a 2-D array (draws x years). NaN where no sign change."""
    flows = np.asarray(flows, dtype=float)
    t = np.arange(flows.shape[1])

    def _npv(r):
        return np.sum(flows / (1.0 + r[:, None]) ** t, axis=1)

    lo_v = np.full(flows.shape[0], lo)
    hi_v = np.full(flows.shape[0], hi)
    f_lo = _npv(lo_v)
    valid = np.sign(f_lo) != np.sign(_npv(hi_v))
    for _ in range(iters):
        mid = 0.5 * (lo_v + hi_v)
        f_mid = _npv(mid)
        same = np.sign(f_mid) == np.sign(f_lo)
        lo_v = np.where(same, mid, lo_v)
        f_lo = np.where(same, f_mid, f_lo)
        hi_v = np.where(same, hi_v, mid)
    out = 0.5 * (lo_v + hi_v)
    out[~valid] = np.nan
    return out


def payback_years(flows: np.ndarray, rate: float = 0.0) -> float:
    """(Discounted) payback with linear interpolation inside the crossing year."""
    t = np.arange(len(flows))
    disc = flows / (1.0 + rate) ** t
    cum = np.cumsum(disc)
    idx = np.where(cum >= 0)[0]
    idx = idx[idx > 0]
    if len(idx) == 0:
        return float("nan")
    k = idx[0]
    return float((k - 1) + (-cum[k - 1]) / disc[k])


def project_metrics(p: dict) -> dict[str, float]:
    df = build_cashflows(p)
    flows = df["net_cash_flow"].to_numpy()
    return {
        "capex_usd": capex_usd(p),
        "npv_usd": npv(p["discount_rate"], flows),
        "irr": irr(flows),
        "simple_payback_years": payback_years(flows, 0.0),
        "discounted_payback_years": payback_years(flows, p["discount_rate"]),
    }
