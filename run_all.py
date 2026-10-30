"""Run the full BESS LCOS / cash-flow / tornado / Monte Carlo pipeline.

Usage:  python run_all.py [--config config/assumptions.yaml] [--draws N]
Outputs: results/*.csv, results/summary.json, figures/*.png
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))

from bess_tea import plots  # noqa: E402
from bess_tea.augmentation import evaluate_grid, robustness, strategy_grid  # noqa: E402
from bess_tea.cashflow import build_cashflows, project_metrics  # noqa: E402
from bess_tea.config import DEFAULT_CONFIG, base_params, load_config  # noqa: E402
from bess_tea.lcos import COMPONENTS, lcos_breakdown  # noqa: E402
from bess_tea.montecarlo import rank_correlations, run_monte_carlo, summarise  # noqa: E402
from bess_tea.sensitivity import tornado  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=str(DEFAULT_CONFIG))
    ap.add_argument("--draws", type=int, default=None, help="override Monte Carlo draws")
    ap.add_argument("--aug-draws", type=int, default=None, help="override augmentation robustness draws (0 = skip)")
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

    # 4) augmentation / overbuild strategy study (two capacity-revenue rules) + Monte Carlo robustness
    acfg = cfg["augmentation_study"]
    strategies = strategy_grid(acfg["intervals_years"], acfg["overbuilds"], int(p["lifetime_years"]))
    rules = {"fixed": False, "soh_linked": True}
    aug = pd.concat([evaluate_grid(dict(p, capacity_credit_tracks_soh=v), strategies).assign(capacity_rule=k)
                     for k, v in rules.items()], ignore_index=True)
    aug.round(5).to_csv(res_dir / "augmentation_strategies.csv", index=False)
    n_aug = acfg["mc_draws"] if args.aug_draws is None else args.aug_draws
    rob = pd.DataFrame()
    if n_aug > 0:
        rob = pd.concat([robustness(dict(p, capacity_credit_tracks_soh=v), strategies, mc_cfg["distributions"],
                                    n_aug, mc_cfg["seed"]).assign(capacity_rule=k) for k, v in rules.items()],
                        ignore_index=True)
        rob.round(5).to_csv(res_dir / "augmentation_robustness.csv", index=False)
    aug_summary = {}
    for k in rules:
        g = aug[aug.capacity_rule == k]
        base_row = g[(g.initial_overbuild == 0.0) & (g.interval_years == 5)].iloc[0]
        bn, bl = g.loc[g.npv_musd.idxmax()], g.loc[g.lcos_usd_per_mwh.idxmin()]
        aug_summary[k] = {
            "base_case_strategy": {"npv_musd": base_row.npv_musd, "lcos": base_row.lcos_usd_per_mwh, "irr": base_row.irr},
            "best_npv_strategy": {"strategy": bn.strategy, "npv_musd": bn.npv_musd, "lcos": bn.lcos_usd_per_mwh, "irr": bn.irr},
            "lowest_lcos_strategy": {"strategy": bl.strategy, "npv_musd": bl.npv_musd, "lcos": bl.lcos_usd_per_mwh, "irr": bl.irr},
        }
        if len(rob):
            r = rob[rob.capacity_rule == k].sort_values("share_best_npv", ascending=False)
            aug_summary[k]["mc_draws"] = int(n_aug)
            aug_summary[k]["mc_most_often_best"] = r.head(3)[["strategy", "share_best_npv", "mean_regret_musd"]].to_dict("records")
            br = rob[(rob.capacity_rule == k) & (rob.strategy == "OB 0% / augment every 5 y")].iloc[0]
            aug_summary[k]["mc_base_case_strategy"] = {"share_best_npv": br.share_best_npv, "mean_regret_musd": br.mean_regret_musd}

    # 5) figures
    plots.lcos_waterfall(bd, fig_dir / "lcos_breakdown.png")
    plots.tornado_plot(tor, fig_dir / "tornado_irr_lcos.png", hurdle)
    plots.mc_irr_plot(res, summ, hurdle, m["irr"], fig_dir / "mc_irr_distribution.png")
    plots.mc_npv_lcos_plot(res, summ, fig_dir / "mc_npv_lcos_distribution.png")
    plots.cashflow_plot(cf, fig_dir / "base_case_cashflows.png")
    plots.augmentation_heatmaps(aug, fig_dir / "augmentation_strategies.png")

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
        "augmentation_study": aug_summary,
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
    for k, v in aug_summary.items():
        print(f"Augmentation study [{k} capacity payment]: base (OB 0%/5 y) NPV {v['base_case_strategy']['npv_musd']:.2f} M$, "
              f"LCOS {v['base_case_strategy']['lcos']:.2f} | best NPV: {v['best_npv_strategy']['strategy']} "
              f"{v['best_npv_strategy']['npv_musd']:.2f} M$ (LCOS {v['best_npv_strategy']['lcos']:.2f}) | lowest LCOS: "
              f"{v['lowest_lcos_strategy']['strategy']} {v['lowest_lcos_strategy']['lcos']:.2f} (NPV {v['lowest_lcos_strategy']['npv_musd']:.2f} M$)")
        if "mc_most_often_best" in v:
            print("   MC most often best:", [(d["strategy"], round(d["share_best_npv"], 3), round(d["mean_regret_musd"], 3)) for d in v["mc_most_often_best"]],
                  "| base-case strategy best in", round(v["mc_base_case_strategy"]["share_best_npv"], 3), "regret", round(v["mc_base_case_strategy"]["mean_regret_musd"], 3), "M$")


if __name__ == "__main__":
    main()
