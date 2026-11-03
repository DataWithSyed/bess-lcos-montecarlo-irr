"""Project-finance layer: merchant vs tolling revenue and DSCR-sculpted debt sizing.

All flows stay in the toolkit's real, pre-tax basis (no tax shield, no inflation), so the
debt rate is a *real* rate and equity IRRs are real, pre-tax.

Revenue structures
------------------
* ``merchant``: the project keeps arbitrage + capacity revenue and pays for charging energy
  (the existing unlevered cash-flow model).
* ``tolled``: an offtaker pays a fixed toll in $/kW-yr for ``toll_term_years``, takes all market
  revenue and pays for charging energy. The toll is scaled by usable energy (min(SOH, 1)), i.e.
  the owner keeps the capacity/availability obligation and still pays O&M and augmentation.
  After the toll term the asset is merchant again.

Augmentation reserve
--------------------
Lenders normally require a maintenance/augmentation reserve account (MRA) rather than letting a
lumpy augmentation spend hit one year's CFADS. With ``augmentation_reserve`` on (default), each
augmentation spend is pre-funded by equal contributions over the years since the previous one
(real terms, no interest on the reserve). Total spend is unchanged; only the timing moves.

Debt sizing (sculpted)
----------------------
For each year t <= tenor, debt service DS_t = CFADS_t / DSCR_target on the lender's sizing
case; debt = PV(DS, debt rate). If that exceeds ``max_gearing * capex`` all DS_t are scaled
down pro rata, which keeps the sculpted profile. Principal_t = DS_t - interest_t.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from .cashflow import build_cashflows, capex_usd, irr, irr_vectorised
from .montecarlo import sample_inputs
from .physics import energy_profile

STRUCTURES = ("merchant", "tolled")


def merchant_margin_per_kw_yr(p: dict) -> float:
    """Year-1 merchant gross margin (arbitrage + capacity - charging) in $/kW-yr."""
    cf = build_cashflows(p).iloc[1]
    m = cf["arbitrage_revenue"] + cf["capacity_revenue"] + cf["charging_cost"]
    return float(m / (p["power_mw"] * 1_000.0))


def reserve_contributions(aug: np.ndarray) -> np.ndarray:
    """Spread each (negative) augmentation spend evenly over the years since the previous spend
    (operating years only; index 0 is construction). Sum is preserved."""
    aug = np.asarray(aug, float)
    out = np.zeros_like(aug)
    start = 1
    for k in range(1, len(aug)):
        if aug[k] != 0.0:
            out[start:k + 1] += aug[k] / (k + 1 - start)
            start = k + 1
    return out


def operating_cashflows(p: dict, structure: str, toll_usd_per_kw_yr: float = 0.0,
                        toll_term_years: int = 0, augmentation_reserve: bool = True) -> pd.DataFrame:
    """Years 0..N with revenue, opex and CFADS for a revenue structure (row 0 = capex)."""
    if structure not in STRUCTURES:
        raise ValueError(f"unknown structure {structure}")
    cf = build_cashflows(p)
    market = (cf["arbitrage_revenue"] + cf["capacity_revenue"] + cf["charging_cost"]).to_numpy()
    revenue = market.copy()
    if structure == "tolled":
        soh = np.r_[0.0, np.minimum(energy_profile(p)["soh"], 1.0)]
        toll = toll_usd_per_kw_yr * p["power_mw"] * 1_000.0 * soh
        in_term = (cf["year"].to_numpy() >= 1) & (cf["year"].to_numpy() <= toll_term_years)
        revenue = np.where(in_term, toll, market)
    aug = cf["augmentation"].to_numpy()
    if augmentation_reserve:
        aug = reserve_contributions(aug)
    opex = (cf["fixed_om"] + cf["variable_om"]).to_numpy() + aug
    cfads = revenue + opex + cf["residual_value"].to_numpy()
    cfads[0] = 0.0
    return pd.DataFrame({"year": cf["year"], "revenue": revenue, "opex": opex, "cfads": cfads,
                         "capex": cf["capex"].to_numpy()})


def size_debt(cfads: np.ndarray, dscr_target: float, tenor: int, rate: float,
              max_debt: float) -> tuple[float, np.ndarray]:
    """Sculpted debt amount and the debt-service vector (index 0 = year 0, always 0)."""
    cfads = np.asarray(cfads, float)
    ds = np.zeros_like(cfads)
    t = np.arange(len(cfads))
    k = (t >= 1) & (t <= tenor)
    ds[k] = np.maximum(cfads[k], 0.0) / dscr_target
    debt = float(np.sum(ds / (1.0 + rate) ** t))
    if debt > max_debt > 0:
        ds *= max_debt / debt
        debt = max_debt
    elif max_debt <= 0:
        ds[:] = 0.0
        debt = 0.0
    return debt, ds


def debt_schedule(debt: float, ds: np.ndarray, rate: float) -> pd.DataFrame:
    bal, rows = debt, []
    for t, d in enumerate(ds):
        if t == 0:
            rows.append((t, 0.0, 0.0, bal))
            continue
        interest = bal * rate
        principal = d - interest
        bal -= principal
        rows.append((t, interest, principal, bal))
    return pd.DataFrame(rows, columns=["year", "interest", "principal", "closing_balance"])


def structure_terms(f: dict, structure: str) -> dict:
    return {k: f[f"{structure}_{k}"] for k in ("dscr_target", "tenor_years", "max_gearing")}


def finance_case(p: dict, f: dict, structure: str, toll_usd_per_kw_yr: float | None = None) -> dict:
    """Base-case financing metrics for one structure."""
    if toll_usd_per_kw_yr is None:
        toll_usd_per_kw_yr = f["toll_pct_of_merchant_margin"] * merchant_margin_per_kw_yr(p)
    ops = operating_cashflows(p, structure, toll_usd_per_kw_yr, int(f["toll_term_years"]))
    capex = capex_usd(p)
    terms = structure_terms(f, structure)
    debt, ds = size_debt(ops["cfads"].to_numpy(), terms["dscr_target"], int(terms["tenor_years"]),
                         f["debt_rate_real"], terms["max_gearing"] * capex)
    sched = debt_schedule(debt, ds, f["debt_rate_real"])
    eq = ops["cfads"].to_numpy() - ds
    eq[0] = -(capex - debt)
    proj = ops["cfads"].to_numpy().copy()
    proj[0] = -capex
    k = ds > 0
    dscr = ops["cfads"].to_numpy()[k] / ds[k] if k.any() else np.array([np.nan])
    return {
        "structure": structure,
        "toll_usd_per_kw_yr": toll_usd_per_kw_yr if structure == "tolled" else None,
        "capex_usd": capex,
        "debt_usd": debt,
        "gearing": debt / capex,
        "gearing_capped": bool(debt >= terms["max_gearing"] * capex - 1e-6),
        "min_dscr": float(np.min(dscr)),
        "avg_dscr": float(np.mean(dscr)),
        "project_irr": irr(proj),
        "equity_irr": irr(eq),
        "equity_usd": capex - debt,
        "debt_service": ds,
        "schedule": sched,
        "cfads": ops["cfads"].to_numpy(),
    }


def finance_monte_carlo(p: dict, f: dict, dists: dict, n: int, seed: int,
                        base_cases: dict[str, dict]) -> pd.DataFrame:
    """Hold debt and toll fixed at financial close (base case) and stress CFADS with the
    existing input distributions. Returns per-draw equity IRR, min DSCR, and default flag."""
    # capex and the discount rate are fixed at financial close; only operating risks are drawn
    dists = {k: v for k, v in dists.items() if k not in ("cost_multiplier", "discount_rate")}
    samples = sample_inputs(dists, n, seed)
    out = {}
    for s in STRUCTURES:
        bc = base_cases[s]
        ds = bc["debt_service"]
        toll = bc["toll_usd_per_kw_yr"] or 0.0
        eq_rows, min_dscr = [], []
        for row in samples.to_dict("records"):
            q = dict(p, **row)
            cf = operating_cashflows(q, s, toll, int(f["toll_term_years"]))["cfads"].to_numpy()
            eq = cf - ds
            eq[0] = -bc["equity_usd"]
            eq_rows.append(eq)
            k = ds > 0
            min_dscr.append(float(np.min(cf[k] / ds[k])) if k.any() else np.nan)
        out[f"{s}_equity_irr"] = irr_vectorised(np.vstack(eq_rows))
        out[f"{s}_min_dscr"] = np.array(min_dscr)
    return pd.DataFrame(out)


def summarise_finance_mc(mc: pd.DataFrame, equity_hurdle: float) -> dict:
    res = {}
    for s in STRUCTURES:
        e = mc[f"{s}_equity_irr"].to_numpy()
        d = mc[f"{s}_min_dscr"].to_numpy()
        v = e[~np.isnan(e)]
        res[s] = {
            "equity_irr_p10": float(np.percentile(v, 10)) if len(v) else None,
            "equity_irr_p50": float(np.percentile(v, 50)) if len(v) else None,
            "equity_irr_p90": float(np.percentile(v, 90)) if len(v) else None,
            "prob_equity_irr_above_hurdle": float(np.sum(v > equity_hurdle) / len(e)),
            "n_equity_irr_undefined": int(np.isnan(e).sum()),
            "prob_min_dscr_below_1": float(np.mean(d < 1.0)),
            "prob_min_dscr_below_1_10": float(np.mean(d < 1.10)),
            "min_dscr_p10": float(np.percentile(d, 10)),
        }
    return res


def breakeven_toll_pct(p: dict, f: dict, target_equity_irr: float, lo: float = 0.3, hi: float = 3.0) -> float:
    """Toll (fraction of base-case merchant margin) at which base-case equity IRR hits the target."""
    m = merchant_margin_per_kw_yr(p)
    g = lambda x: finance_case(p, f, "tolled", x * m)["equity_irr"] - target_equity_irr
    if g(lo) > 0 or g(hi) < 0:
        return float("nan")
    for _ in range(60):
        mid = 0.5 * (lo + hi)
        lo, hi = (mid, hi) if g(mid) < 0 else (lo, mid)
    return 0.5 * (lo + hi)


def toll_sweep(p: dict, f: dict, pcts: list[float]) -> pd.DataFrame:
    base_margin = merchant_margin_per_kw_yr(p)
    rows = []
    for pct in pcts:
        c = finance_case(p, f, "tolled", pct * base_margin)
        rows.append({"toll_pct_of_merchant_margin": pct, "toll_usd_per_kw_yr": pct * base_margin,
                     "debt_musd": c["debt_usd"] / 1e6, "gearing": c["gearing"], "project_irr": c["project_irr"],
                     "equity_irr": c["equity_irr"]})
    return pd.DataFrame(rows)
