"""Run the full BESS LCOS / cash-flow / tornado / Monte Carlo pipeline.

Usage:  python run_all.py [--config config/assumptions.yaml] [--draws N]
Outputs: results/*.csv, results/summary.json, figures/*.png
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))

from bess_tea import plots  # noqa: E402
from bess_tea.cashflow import build_cashflows, project_metrics  # noqa: E402
from bess_tea.config import DEFAULT_CONFIG, base_params, load_config  # noqa: E402
from bess_tea.lcos import COMPONENTS, lcos_breakdown  # noqa: E402
from bess_tea.montecarlo import rank_correlations, run_monte_carlo, summarise  # noqa: E402
from bess_tea.sensitivity import tornado  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=str(DEFAULT_CONFIG))
    ap.add_argument("--draws", type=int, default=None, help="override Monte Carlo draws")
    args = ap.parse_args()

    cfg = load_config(args.config)
    p = base_params(cfg)
    fig_dir, res_dir = ROOT / "figures", ROOT / "results"
    fig_dir.mkdir(exist_ok=True); res_dir.mkdir(exist_ok=True)
    hurdle = cfg["finance"]["hurdle_irr"]

    # 1) base case
    cf = build_cashflows(p)
    cf.round(2).to_csv(res_dir / "base_case_cashflows.csv", index=False)
    m = project_metrics(p)
    bd = lcos_breakdown(p)
    capex = m["capex_usd"]
    ops = cf.iloc[1:]
    implied_om_aug_pct = float((-(ops["fixed_om"] + ops["augmentation"]).mean()) / capex)

    # 2) tornado
    tor = tornado(p, cfg["tornado"])
    tor.to_csv(res_dir / "tornado.csv", index=False)
    top_irr = tor.sort_values("irr_swing", ascending=False).iloc[0]
    top_lcos = tor.sort_values("lcos_swing", ascending=False).iloc[0]

    # 3) Monte Carlo
    mc_cfg = cfg["monte_carlo"]
    n = args.draws or mc_cfg["n_draws"]
    res = run_monte_carlo(p, mc_cfg["distributions"], n, mc_cfg["seed"])
    res.to_csv(res_dir / "monte_carlo_draws.csv.gz", index=False, float_format="%.6g", compression="gzip")
    summ = summarise(res, hurdle)
    rc = rank_correlations(res, list(mc_cfg["distributions"]), "irr")
    rc.rename("spearman_rho_vs_irr").to_csv(res_dir / "mc_rank_correlation_irr.csv")

    # 4) figures
    plots.lcos_waterfall(bd, fig_dir / "lcos_breakdown.png")
    plots.tornado_plot(tor, fig_dir / "tornado_irr_lcos.png", hurdle)
    plots.mc_irr_plot(res, summ, hurdle, m["irr"], fig_dir / "mc_irr_distribution.png")
    plots.mc_npv_lcos_plot(res, summ, fig_dir / "mc_npv_lcos_distribution.png")
    plots.cashflow_plot(cf, fig_dir / "base_case_cashflows.png")

    summary = {
        "base_case": {
            "capex_usd": capex,
            "capex_usd_per_kw": capex / (p["power_mw"] * 1000),
            "capex_usd_per_kwh": capex / (p["power_mw"] * p["duration_h"] * 1000),
            "lcos_usd_per_mwh": bd["total"],
            "lcos_breakdown_usd_per_mwh": {k: bd[k] for k in COMPONENTS},
            "npv_usd": m["npv_usd"],
            "irr": m["irr"],
            "simple_payback_years": m["simple_payback_years"],
            "discounted_payback_years": m["discounted_payback_years"],
            "year1_discharged_mwh": float(cf.loc[1, "discharged_mwh"]),
            "lifetime_discharged_mwh": float(cf["discharged_mwh"].sum()),
            "implied_fom_plus_augmentation_pct_capex_per_yr": implied_om_aug_pct,
            "discount_rate": p["discount_rate"],
            "hurdle_irr": hurdle,
        },
        "tornado": {
            "top_irr_driver": top_irr["parameter"],
            "top_irr_driver_range": [top_irr["irr_low"], top_irr["irr_high"]],
            "top_lcos_driver": top_lcos["parameter"],
            "top_lcos_driver_range": [top_lcos["lcos_low"], top_lcos["lcos_high"]],
            "irr_swing_ranking": tor.sort_values("irr_swing", ascending=False)[["parameter", "irr_low", "irr_high"]].to_dict("records"),
            "lcos_swing_ranking": tor.sort_values("lcos_swing", ascending=False)[["parameter", "lcos_low", "lcos_high"]].to_dict("records"),
        },
        "monte_carlo": {**summ, "seed": mc_cfg["seed"], "spearman_rho_vs_irr": rc.to_dict()},
    }
    def _clean(o):  # NaN -> null so summary.json is strict JSON
        if isinstance(o, dict):
            return {k: _clean(v) for k, v in o.items()}
        if isinstance(o, list):
            return [_clean(v) for v in o]
        if isinstance(o, float) and o != o:
            return None
        return o

    with open(res_dir / "summary.json", "w", encoding="utf-8") as fh:
        json.dump(_clean(summary), fh, indent=2)

    b = summary["base_case"]
    print(f"Capex: {b['capex_usd']/1e6:,.2f} M$  ({b['capex_usd_per_kw']:.1f} $/kW, {b['capex_usd_per_kwh']:.1f} $/kWh)")
    print(f"LCOS: {b['lcos_usd_per_mwh']:.2f} $/MWh")
    for k, v in b["lcos_breakdown_usd_per_mwh"].items():
        print(f"   {k:32s} {v:8.2f}")
    print(f"NPV @ {p['discount_rate']*100:.1f}%: {b['npv_usd']/1e6:,.2f} M$ | IRR: {b['irr']*100:.2f}% | "
          f"payback {b['simple_payback_years']:.2f} y (discounted: "
          f"{'not reached within life' if b['discounted_payback_years'] != b['discounted_payback_years'] else format(b['discounted_payback_years'], '.2f') + ' y'})")
    print(f"Implied FOM + augmentation: {implied_om_aug_pct*100:.2f}% of capex/yr (NREL 2025 benchmark: 4%)")
    print(f"Top IRR driver: {top_irr['parameter']} ({top_irr['irr_low']*100:.2f}% .. {top_irr['irr_high']*100:.2f}%)")
    print(f"Top LCOS driver: {top_lcos['parameter']} ({top_lcos['lcos_low']:.2f} .. {top_lcos['lcos_high']:.2f})")
    s = summary["monte_carlo"]
    print(f"MC n={s['n_draws']} seed={s['seed']}: IRR P10/P50/P90 = {s['irr_p10']*100:.2f}/{s['irr_p50']*100:.2f}/{s['irr_p90']*100:.2f}% | "
          f"P(IRR>{hurdle*100:.0f}%) = {s['prob_irr_above_hurdle']*100:.1f}% | undefined IRR: {s['n_irr_undefined']}")
    print(f"   NPV P10/P50/P90 = {s['npv_p10']/1e6:,.2f}/{s['npv_p50']/1e6:,.2f}/{s['npv_p90']/1e6:,.2f} M$ | P(NPV>0) = {s['prob_npv_positive']*100:.1f}%")
    print(f"   LCOS P10/P50/P90 = {s['lcos_p10']:.2f}/{s['lcos_p50']:.2f}/{s['lcos_p90']:.2f} $/MWh")
    print("   Spearman rho vs IRR:", {k: round(v, 3) for k, v in rc.items()})


if __name__ == "__main__":
    main()
