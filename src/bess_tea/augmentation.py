"""Augmentation / overbuild strategy study.

A strategy is (day-1 overbuild, augmentation interval). Each strategy is priced with the same cash-flow and
LCOS model as the base case: overbuild energy at the build-year NREL energy-cost component, augmentation at the
NREL Mid curve for the calendar year it happens (cheaper later), routine fixed O&M as a % of (larger) capex.
Two capacity-revenue rules are compared: a fixed $/kW-yr payment (base case) and a payment that is scaled by the
usable energy, as with duration-based capacity accreditation (``capacity_credit_tracks_soh``).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from .cashflow import build_cashflows, capex_usd, irr, npv
from .lcos import lcos_breakdown
from .montecarlo import sample_inputs
from .physics import energy_profile


def augmentation_years(interval: int, lifetime: int) -> list[int]:
    """End-of-year augmentation events every ``interval`` years (0 = never); none in the final year."""
    if interval <= 0:
        return []
    return [y for y in range(interval, lifetime, interval)]


def strategy_grid(intervals: list[int], overbuilds: list[float], lifetime: int) -> list[dict]:
    out = []
    for ob in overbuilds:
        for k in intervals:
            name = f"OB {ob*100:.0f}% / " + ("no augmentation" if k <= 0 else f"augment every {k} y")
            out.append({"strategy": name, "initial_overbuild": float(ob), "interval_years": int(k),
                        "augmentation_years": augmentation_years(int(k), lifetime)})
    return out


def evaluate_strategy(p: dict, s: dict) -> dict:
    q = dict(p, initial_overbuild=s["initial_overbuild"], augmentation_years=s["augmentation_years"])
    cf = build_cashflows(q)
    flows = cf["net_cash_flow"].to_numpy()
    prof = energy_profile(q)
    bd = lcos_breakdown(q)
    return {
        "strategy": s["strategy"], "initial_overbuild": s["initial_overbuild"], "interval_years": s["interval_years"],
        "n_augmentations": len(s["augmentation_years"]),
        "capex_musd": capex_usd(q) / 1e6,
        "augmentation_mwh_total": float(prof["aug_mwh"].sum()),
        "pv_augmentation_musd": float(-(cf["augmentation"] / (1 + q["discount_rate"]) ** cf["year"]).sum()) / 1e6,
        "lifetime_discharged_gwh": float(cf["discharged_mwh"].sum()) / 1e3,
        "min_usable_soh": float(prof["soh"].min()),
        "lcos_usd_per_mwh": bd["total"],
        "npv_musd": npv(q["discount_rate"], flows) / 1e6,
        "irr": irr(flows),
    }


def evaluate_grid(p: dict, strategies: list[dict]) -> pd.DataFrame:
    return pd.DataFrame([evaluate_strategy(p, s) for s in strategies])


def robustness(p: dict, strategies: list[dict], dists: dict, n: int, seed: int) -> pd.DataFrame:
    """Re-evaluate every strategy on the same Monte Carlo draws; report how often each one has the highest
    NPV and its mean NPV regret (best NPV in that draw minus the strategy's NPV)."""
    samples = sample_inputs(dists, n, seed)
    npvs = np.empty((n, len(strategies)))
    for i, row in enumerate(samples.to_dict("records")):
        base = dict(p, **row)
        for j, s in enumerate(strategies):
            q = dict(base, initial_overbuild=s["initial_overbuild"], augmentation_years=s["augmentation_years"])
            npvs[i, j] = npv(q["discount_rate"], build_cashflows(q)["net_cash_flow"].to_numpy())
    best = npvs.argmax(axis=1)
    regret = npvs.max(axis=1, keepdims=True) - npvs
    return pd.DataFrame({
        "strategy": [s["strategy"] for s in strategies],
        "share_best_npv": [(best == j).mean() for j in range(len(strategies))],
        "mean_npv_musd": npvs.mean(axis=0) / 1e6,
        "mean_regret_musd": regret.mean(axis=0) / 1e6,
        "p90_regret_musd": np.percentile(regret, 90, axis=0) / 1e6,
    })
