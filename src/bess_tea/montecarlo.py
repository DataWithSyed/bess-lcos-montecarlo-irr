"""Monte Carlo simulation over uncertain techno-economic inputs."""
from __future__ import annotations

import numpy as np
import pandas as pd

from .cashflow import build_cashflows, irr_vectorised
from .lcos import lcos


def sample_inputs(dists: dict, n: int, seed: int) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    out = {}
    for name, spec in dists.items():
        kind = spec["dist"]
        if kind == "triangular":
            out[name] = rng.triangular(spec["low"], spec["mode"], spec["high"], n)
        elif kind == "uniform":
            out[name] = rng.uniform(spec["low"], spec["high"], n)
        elif kind == "normal":
            out[name] = rng.normal(spec["mean"], spec["sd"], n)
        else:
            raise ValueError(f"unknown distribution '{kind}' for {name}")
    return pd.DataFrame(out)


def run_monte_carlo(base: dict, dists: dict, n: int, seed: int) -> pd.DataFrame:
    samples = sample_inputs(dists, n, seed)
    flows, npvs, lcoss = [], [], []
    for row in samples.to_dict("records"):
        p = dict(base, **row)
        cf = build_cashflows(p)["net_cash_flow"].to_numpy()
        flows.append(cf)
        t = np.arange(len(cf))
        npvs.append(float(np.sum(cf / (1.0 + p["discount_rate"]) ** t)))
        lcoss.append(lcos(p))
    res = samples.copy()
    res["irr"] = irr_vectorised(np.vstack(flows))
    res["npv_usd"] = npvs
    res["lcos_usd_per_mwh"] = lcoss
    return res


def summarise(res: pd.DataFrame, hurdle: float) -> dict[str, float]:
    irr = res["irr"].to_numpy()
    valid = irr[~np.isnan(irr)]
    pct = lambda x, q: float(np.percentile(x, q))
    return {
        "n_draws": int(len(res)),
        "n_irr_undefined": int(np.isnan(irr).sum()),
        "irr_p10": pct(valid, 10), "irr_p50": pct(valid, 50), "irr_p90": pct(valid, 90),
        "irr_mean": float(valid.mean()),
        # undefined IRR (never pays back) counts as below hurdle
        "prob_irr_above_hurdle": float(np.sum(valid > hurdle) / len(irr)),
        "npv_p10": pct(res["npv_usd"], 10), "npv_p50": pct(res["npv_usd"], 50), "npv_p90": pct(res["npv_usd"], 90),
        "prob_npv_positive": float((res["npv_usd"] > 0).mean()),
        "lcos_p10": pct(res["lcos_usd_per_mwh"], 10), "lcos_p50": pct(res["lcos_usd_per_mwh"], 50),
        "lcos_p90": pct(res["lcos_usd_per_mwh"], 90),
    }


def rank_correlations(res: pd.DataFrame, inputs: list[str], target: str = "irr") -> pd.Series:
    """Spearman rank correlation of each input with the target (NaNs dropped)."""
    d = res.dropna(subset=[target])
    ranks = d[inputs + [target]].rank()
    return ranks[inputs].corrwith(ranks[target]).sort_values(key=np.abs, ascending=False)
