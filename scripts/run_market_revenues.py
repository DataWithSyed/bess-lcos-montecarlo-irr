#!/usr/bin/env python3
"""Compare synthetic 115/35 $/MWh revenues with an Energy-Charts DE-LU DA path."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from bess_tea.config import base_params, load_config
from bess_tea.market import attach_usd, dispatch_path, load_da_prices, summarise_path

RESULTS = ROOT / "results"
FIGURES = ROOT / "figures"


def _json_safe(obj):
    if isinstance(obj, dict):
        return {str(k): _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (float, int, str, bool)) or obj is None:
        return obj
    if isinstance(obj, list):
        return [_json_safe(v) for v in obj]
    return float(obj) if hasattr(obj, "item") else str(obj)


def plot_spread(daily: pd.DataFrame, summary: dict, path: Path) -> None:
    fig, axes = plt.subplots(2, 1, figsize=(10.5, 6.2), sharex=False)
    d = daily.copy()
    d["day"] = pd.to_datetime(d["date"])
    axes[0].plot(d["day"], d["net_usd"] / 1000.0, color="#4C78A8", lw=0.9)
    axes[0].axhline(0, color="#333", lw=0.5)
    axes[0].set_ylabel("Daily arbitrage net (kUSD)")
    axes[0].set_title(
        "Energy-Charts DE-LU day-ahead, 100 MW / 200 MWh, 1 cycle/day if profitable "
        f"({summary['n_days_cycled']}/{summary['n_days']} days)"
    )
    axes[0].grid(axis="y", alpha=0.3)

    cycled = d[d["cycled"]]
    axes[1].hist(
        cycled["vwap_discharge_usd"] - cycled["vwap_charge_usd"],
        bins=30,
        color="#F2A93B",
        edgecolor="white",
    )
    axes[1].axvline(
        summary["synthetic_spread_usd_mwh"],
        color="#E15759",
        ls="--",
        lw=1.2,
        label=f"Synthetic spread {summary['synthetic_spread_usd_mwh']:.0f} $/MWh",
    )
    axes[1].axvline(
        summary["spread_usd_mwh"],
        color="#4C78A8",
        lw=1.2,
        label=f"Realised VWAP spread {summary['spread_usd_mwh']:.1f} $/MWh",
    )
    axes[1].set_xlabel("Daily realised discharge−charge spread ($/MWh, via monthly ECB FX)")
    axes[1].set_ylabel("Days")
    axes[1].legend(frameon=False, fontsize=8)
    axes[1].grid(axis="y", alpha=0.3)
    fig.tight_layout()
    fig.savefig(path, dpi=120)
    plt.close(fig)


def main() -> None:
    p = base_params(load_config())
    prices = attach_usd(load_da_prices())
    daily = dispatch_path(
        prices,
        power_mw=p["power_mw"],
        duration_h=p["duration_h"],
        rte=p["round_trip_efficiency"],
    )
    summary = summarise_path(daily, prices, p)
    RESULTS.mkdir(parents=True, exist_ok=True)
    FIGURES.mkdir(parents=True, exist_ok=True)
    daily.to_csv(RESULTS / "market_daily_dispatch.csv", index=False)
    (RESULTS / "market_revenues.json").write_text(json.dumps(_json_safe(summary), indent=2))
    plot_spread(daily, summary, FIGURES / "market_da_arbitrage.png")
    print("days cycled", summary["n_days_cycled"], "/", summary["n_days"])
    print("VWAP dis/chg", round(summary["vwap_discharge_usd_mwh"], 2), round(summary["vwap_charge_usd_mwh"], 2))
    print("spread market/synthetic", round(summary["spread_usd_mwh"], 2), summary["synthetic_spread_usd_mwh"])
    print("IRR market+synth cap", summary["tea_market_with_synthetic_capacity"]["irr"])
    print("IRR synthetic+synth cap", summary["tea_synthetic_with_capacity"]["irr"])
    print("IRR market arb only", summary["tea_market_arbitrage_only"]["irr"])


if __name__ == "__main__":
    main()
