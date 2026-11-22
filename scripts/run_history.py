#!/usr/bin/env python3
"""Eight DE-LU day-ahead windows (2018/19 → 2025/26), real 2024 US$, perfect vs
persistence foresight, and a year-bootstrap Monte Carlo of the 15-year IRR.

No network needed (processed inputs are shipped; refresh with fetch_history.py).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from bess_tea.config import base_params, load_config
from bess_tea.history import (
    constant_window_metrics,
    dispatch_window,
    load_history,
    to_real_usd,
    window_label,
    window_stats,
    year_bootstrap,
)
from bess_tea.market import attach_usd, dispatch_path, load_da_prices, summarise_path

RESULTS = ROOT / "results"
FIGURES = ROOT / "figures"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--draws", type=int, default=10_000)
    ap.add_argument("--seed", type=int, default=42)
    a = ap.parse_args()
    p = base_params(load_config())
    P, D, RTE = p["power_mw"], p["duration_h"], p["round_trip_efficiency"]
    prices = to_real_usd(load_history())
    rows = []
    for w, g in prices.groupby("window", sort=True):
        g = g.reset_index(drop=True)
        rec = {"window": window_label(int(w)), "start_year": int(w), "n_hours": int(len(g)),
               "da_mean_eur_nominal": float(g["da_price_eur_mwh"].mean()),
               "da_mean_usd_real2024": float(g["da_price_usd_mwh"].mean()),
               "da_min_eur_nominal": float(g["da_price_eur_mwh"].min()),
               "da_max_eur_nominal": float(g["da_price_eur_mwh"].max()),
               "neg_hour_share": float((g["da_price_eur_mwh"] < 0).mean())}
        for pol in ("perfect", "persistence"):
            st = window_stats(dispatch_window(g, P, D, RTE, policy=pol), P, D)
            for k, v in st.items():
                rec[f"{pol}_{k}"] = v
            for cap in (False, True):
                m = constant_window_metrics(p, st, capacity=cap)
                tag = "cap" if cap else "arb"
                rec[f"{pol}_irr_{tag}"] = m["irr"]
                rec[f"{pol}_npv_{tag}_usd"] = m["npv_usd"]
        rec["persistence_capture"] = rec["persistence_net_usd"] / rec["perfect_net_usd"]
        rows.append(rec)
        print(rec["window"], round(rec["perfect_net_usd_per_kw_yr"], 1), round(rec["persistence_net_usd_per_kw_yr"], 1),
              round(rec["persistence_capture"], 3), round(rec["perfect_irr_arb"], 4), round(rec["persistence_irr_arb"], 4))
    tab = pd.DataFrame(rows)
    RESULTS.mkdir(exist_ok=True)
    FIGURES.mkdir(exist_ok=True)
    tab.to_csv(RESULTS / "history_windows.csv", index=False)

    # Reconciliation with the 2026-11-12 nominal-FX path for 2025/26
    nominal = summarise_path(dispatch_path(attach_usd(load_da_prices()), P, D, RTE), attach_usd(load_da_prices()), p)

    out = {"basis": f"real {2024} US$: German HICP (Eurostat prc_hicp_minr, 2015=100) deflator to the 2024 mean, "
                    f"then 2024 mean ECB USD/EUR {prices.attrs['fx_base']:.4f}",
           "hicp_2024_mean": prices.attrs["hicp_base"], "fx_2024_mean": prices.attrs["fx_base"],
           "windows": tab["window"].tolist(),
           "reconcile_2025_26_nominal_fx": {"net_usd_per_kw_yr": nominal["arbitrage_net_usd_per_kw_yr"],
                                             "irr_arb": nominal["tea_market_arbitrage_only"]["irr"]},
           "bootstrap": {}, "regime_hold_one_window": {}}
    bmask = {"all_8": np.ones(len(tab), bool),
             "excl_2021_22_and_2022_23": ~tab["start_year"].isin([2021, 2022]).to_numpy()}
    irr_store = {}
    for pol in ("perfect", "persistence"):
        st = tab[[f"{pol}_vwap_discharge_usd_mwh", f"{pol}_vwap_charge_usd_mwh", f"{pol}_cycles"]].copy()
        st.columns = ["vwap_discharge_usd_mwh", "vwap_charge_usd_mwh", "cycles"]
        for name, mask in bmask.items():
            for cap in (False, True):
                key = f"{pol}|{name}|{'synthetic_capacity' if cap else 'arbitrage_only'}"
                r = year_bootstrap(p, st[mask].reset_index(drop=True), a.draws, a.seed, capacity=cap)
                irr_store[key] = r.pop("_irrs")
                out["bootstrap"][key] = r
                print(key, {k: (round(v, 4) if isinstance(v, float) else v) for k, v in r.items()})
        for cap in (False, True):
            col = f"{pol}_irr_{'cap' if cap else 'arb'}"
            out["regime_hold_one_window"][f"{pol}|{'synthetic_capacity' if cap else 'arbitrage_only'}"] = {
                "min": float(tab[col].min()), "median": float(tab[col].median()), "max": float(tab[col].max()),
                "n_windows_above_hurdle": int((tab[col] > p["hurdle_irr"]).sum()),
                "n_windows_irr_undefined": int(tab[col].isna().sum())}
    (RESULTS / "history_bootstrap.json").write_text(json.dumps(out, indent=2))

    # Figure
    fig, ax = plt.subplots(1, 2, figsize=(12, 4.6))
    x = np.arange(len(tab))
    ax[0].bar(x - 0.2, tab["perfect_net_usd_per_kw_yr"], 0.4, label="Perfect daily foresight", color="#4C78A8")
    ax[0].bar(x + 0.2, tab["persistence_net_usd_per_kw_yr"], 0.4, label="Persistence (D-1 prices)", color="#F2A93B")
    synth = P * D * 365 * (p["discharge_price_per_mwh"] - p["charge_price_per_mwh"] / RTE) / (P * 1_000.0)
    ax[0].axhline(synth, color="#E15759", ls="--", lw=1,
                  label="Synthetic 115/35 case")
    ax[0].set_xticks(x, tab["window"], rotation=30)
    ax[0].set_ylabel("Arbitrage net, $/kW-yr (real 2024)")
    ax[0].set_title("DE-LU day-ahead, 100 MW / 200 MWh, 1 cycle/day, no fees")
    ax[0].legend(frameon=False, fontsize=8)
    ax[0].grid(axis="y", alpha=0.3)
    for key, c, lab in [("perfect|all_8|synthetic_capacity", "#4C78A8", "Perfect, all 8"),
                        ("persistence|all_8|synthetic_capacity", "#F2A93B", "Persistence, all 8"),
                        ("persistence|excl_2021_22_and_2022_23|synthetic_capacity", "#59A14F", "Persistence, excl. 21/22–22/23")]:
        v = irr_store[key]
        ax[1].hist(v[~np.isnan(v)] * 100, bins=60, histtype="step", lw=1.4, color=c, label=lab)
    ax[1].axvline(p["hurdle_irr"] * 100, color="#333", ls=":", lw=1, label="8 % hurdle")
    ax[1].set_xlabel("Unlevered project IRR with SYNTHETIC 50 $/kW-yr capacity (%)")
    ax[1].set_ylabel("Draws")
    ax[1].set_title(f"Year bootstrap of historical windows ({a.draws:,} draws)")
    ax[1].legend(frameon=False, fontsize=8, loc="upper left")
    fig.tight_layout()
    fig.savefig(FIGURES / "history_bootstrap.png", dpi=120)
    plt.close(fig)


if __name__ == "__main__":
    main()
