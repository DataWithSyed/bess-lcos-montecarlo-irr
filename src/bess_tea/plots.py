"""Matplotlib figures (saved as PNG)."""
from __future__ import annotations

from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from .lcos import COMPONENTS  # noqa: E402

plt.rcParams["text.parse_math"] = False  # treat "$" literally in labels

LABELS = {
    "cost_multiplier": "Capex (NREL 2026 Low/High)",
    "discharge_price_per_mwh": "Discharge price",
    "charge_price_per_mwh": "Charging price",
    "capacity_revenue_per_kw_yr": "Capacity revenue",
    "annual_fade": "Degradation rate",
    "availability": "Availability",
    "fixed_om_pct_capex": "Fixed O&M (% capex)",
    "round_trip_efficiency": "Round-trip efficiency",
    "discount_rate": "Discount rate",
}


def _fmt(name, v):
    if name in ("annual_fade", "availability", "fixed_om_pct_capex", "round_trip_efficiency", "discount_rate"):
        return f"{v*100:.1f}%"
    if name == "cost_multiplier":
        return f"x{v:.2f}"
    return f"{v:g}"


def lcos_waterfall(bd: dict, path: Path) -> None:
    items = [(k, bd[k]) for k in COMPONENTS if abs(bd[k]) > 1e-9]
    fig, ax = plt.subplots(figsize=(10, 5.5))
    cum = 0.0
    colors = plt.cm.tab10(np.arange(len(items)))
    for i, (k, v) in enumerate(items):
        ax.bar(i, v, bottom=cum if v >= 0 else cum + v, color=colors[i], edgecolor="black", linewidth=0.5)
        ax.text(i, cum + v + 1.5, f"{v:,.1f}", ha="center", va="bottom", fontsize=9)
        cum += v
    ax.bar(len(items), cum, color="dimgray", edgecolor="black", linewidth=0.5)
    ax.text(len(items), cum + 1.5, f"{cum:,.1f}", ha="center", va="bottom", fontsize=10, fontweight="bold")
    ax.set_xticks(range(len(items) + 1))
    ax.set_xticklabels([k for k, _ in items] + ["LCOS"], rotation=25, ha="right")
    ax.set_ylabel("$/MWh discharged (real 2024$)")
    ax.set_title("LCOS breakdown - 100 MW / 200 MWh BESS (base case)")
    ax.grid(axis="y", alpha=0.3)
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)


def tornado_plot(df: pd.DataFrame, path: Path, hurdle: float) -> None:
    fig, axes = plt.subplots(1, 2, figsize=(14, 5.5))
    specs = [("irr", "Project IRR (real, pre-tax)", 100.0, "%", df.attrs["base_irr"]),
             ("lcos", "LCOS ($/MWh)", 1.0, "", df.attrs["base_lcos"])]
    for ax, (m, title, scale, unit, base) in zip(axes, specs):
        d = df.sort_values(f"{m}_swing")
        y = np.arange(len(d))
        for yi, (_, r) in zip(y, d.iterrows()):
            lo, hi = r[f"{m}_low"] * scale, r[f"{m}_high"] * scale
            ax.barh(yi, lo - base * scale, left=base * scale, color="tab:blue", alpha=0.8)
            ax.barh(yi, hi - base * scale, left=base * scale, color="tab:orange", alpha=0.8)
        ax.axvline(base * scale, color="black", lw=1)
        if m == "irr":
            ax.axvline(hurdle * 100, color="red", ls="--", lw=1, label=f"Hurdle {hurdle*100:.0f}%")
            ax.legend(loc="lower right")
        ax.set_yticks(y)
        ax.set_yticklabels([f"{LABELS.get(n, n)}  [{_fmt(n, lo)} / {_fmt(n, hi)}]"
                            for n, lo, hi in zip(d["parameter"], d["low_value"], d["high_value"])], fontsize=8.5)
        ax.set_title(f"{title}\nbase = {base*scale:.2f}{unit}   (blue = low input, orange = high input)", fontsize=10)
        ax.grid(axis="x", alpha=0.3)
    fig.suptitle("One-at-a-time sensitivity (tornado)")
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)


def mc_irr_plot(res: pd.DataFrame, summ: dict, hurdle: float, base_irr: float, path: Path) -> None:
    irr = res["irr"].dropna().to_numpy() * 100
    fig, axes = plt.subplots(1, 2, figsize=(13, 5))
    ax = axes[0]
    ax.hist(irr, bins=60, color="tab:blue", alpha=0.75, edgecolor="white")
    for q, ls in (("p10", ":"), ("p50", "-"), ("p90", ":")):
        v = summ[f"irr_{q}"] * 100
        ax.axvline(v, color="black", ls=ls, lw=1.2, label=f"{q.upper()} = {v:.2f}%")
    ax.axvline(hurdle * 100, color="red", ls="--", lw=1.2, label=f"Hurdle = {hurdle*100:.0f}%")
    ax.axvline(base_irr * 100, color="tab:green", lw=1.2, label=f"Base case = {base_irr*100:.2f}%")
    ax.set_xlabel("Project IRR (%, real, pre-tax)")
    ax.set_ylabel("Draws")
    ax.set_title(f"Monte Carlo IRR distribution (n = {summ['n_draws']:,})")
    ax.legend(fontsize=8)
    ax = axes[1]
    xs = np.sort(irr)
    ax.plot(xs, np.arange(1, len(xs) + 1) / summ["n_draws"], color="tab:blue")
    ax.axvline(hurdle * 100, color="red", ls="--", lw=1.2)
    ax.axhline(1 - summ["prob_irr_above_hurdle"], color="gray", ls=":", lw=1)
    ax.set_xlabel("Project IRR (%, real, pre-tax)")
    ax.set_ylabel("Cumulative probability")
    ax.set_title(f"CDF - P(IRR > {hurdle*100:.0f}%) = {summ['prob_irr_above_hurdle']*100:.1f}%")
    ax.grid(alpha=0.3)
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)


def mc_npv_lcos_plot(res: pd.DataFrame, summ: dict, path: Path) -> None:
    fig, axes = plt.subplots(1, 2, figsize=(13, 4.8))
    npv = res["npv_usd"].to_numpy() / 1e6
    axes[0].hist(npv, bins=60, color="tab:purple", alpha=0.75, edgecolor="white")
    axes[0].axvline(0, color="red", ls="--")
    for q in ("p10", "p50", "p90"):
        axes[0].axvline(summ[f"npv_{q}"] / 1e6, color="black", ls="-" if q == "p50" else ":",
                        label=f"{q.upper()} = {summ[f'npv_{q}']/1e6:,.1f} M$")
    axes[0].set_xlabel("NPV (M$, real 2024$)")
    axes[0].set_title(f"NPV distribution - P(NPV > 0) = {summ['prob_npv_positive']*100:.1f}%")
    axes[0].legend(fontsize=8)
    l = res["lcos_usd_per_mwh"].to_numpy()
    axes[1].hist(l, bins=60, color="tab:gray", alpha=0.8, edgecolor="white")
    for q in ("p10", "p50", "p90"):
        axes[1].axvline(summ[f"lcos_{q}"], color="black", ls="-" if q == "p50" else ":",
                        label=f"{q.upper()} = {summ[f'lcos_{q}']:,.1f} $/MWh")
    axes[1].set_xlabel("LCOS ($/MWh discharged)")
    axes[1].set_title("LCOS distribution")
    axes[1].legend(fontsize=8)
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)


def cashflow_plot(df: pd.DataFrame, path: Path) -> None:
    cols = ["arbitrage_revenue", "capacity_revenue", "charging_cost", "fixed_om", "augmentation", "capex"]
    fig, ax = plt.subplots(figsize=(11, 5.5))
    pos = np.zeros(len(df)); neg = np.zeros(len(df))
    for c in cols:
        v = df[c].to_numpy() / 1e6
        bottom = np.where(v >= 0, pos, neg)
        ax.bar(df["year"], v, bottom=bottom, label=c.replace("_", " "), width=0.7)
        pos += np.where(v >= 0, v, 0); neg += np.where(v < 0, v, 0)
    ax.plot(df["year"], df["cumulative_cash_flow"] / 1e6, color="black", marker="o", ms=3,
            label="cumulative net cash flow")
    ax.axhline(0, color="black", lw=0.6)
    lo = min(neg.min(), (df["cumulative_cash_flow"] / 1e6).min())
    hi = max(pos.max(), (df["cumulative_cash_flow"] / 1e6).max())
    ax.set_ylim(lo * 1.08, hi * 1.15 + 2)
    ax.set_xlabel("Project year (0 = construction)")
    ax.set_ylabel("M$ (real 2024$)")
    ax.set_title("Base-case annual cash flows and cumulative net cash flow (pre-tax, unlevered)")
    ax.legend(fontsize=8, loc="lower right", ncol=2)
    ax.grid(axis="y", alpha=0.3)
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)


def augmentation_heatmaps(grid: pd.DataFrame, path: Path) -> None:
    """LCOS and NPV over (overbuild x augmentation interval) for both capacity-revenue rules."""
    fig, axes = plt.subplots(1, 3, figsize=(17, 4.8))
    panels = [("lcos_usd_per_mwh", "fixed", "LCOS ($/MWh), lower is better", "viridis_r", "min"),
              ("npv_musd", "fixed", "NPV (M$), fixed $/kW-yr capacity payment", "viridis", "max"),
              ("npv_musd", "soh_linked", "NPV (M$), capacity payment scaled by usable energy", "viridis", "max")]
    for ax, (col, rule, title, cmap, best) in zip(axes, panels):
        g = grid[grid.capacity_rule == rule]
        hm = g.pivot(index="initial_overbuild", columns="interval_years", values=col)
        im = ax.imshow(hm.values, origin="lower", aspect="auto", cmap=cmap)
        ax.set_xticks(range(len(hm.columns)))
        ax.set_xticklabels(["never" if c == 0 else str(c) for c in hm.columns])
        ax.set_yticks(range(len(hm.index)))
        ax.set_yticklabels([f"{v*100:.0f}%" for v in hm.index])
        ax.set_xlabel("augmentation interval (years)")
        ax.set_ylabel("day-1 overbuild")
        ax.set_title(title, fontsize=10)
        bi = np.unravel_index(np.nanargmin(hm.values) if best == "min" else np.nanargmax(hm.values), hm.shape)
        for i in range(hm.shape[0]):
            for j in range(hm.shape[1]):
                ax.text(j, i, f"{hm.values[i, j]:.1f}", ha="center", va="center", fontsize=7,
                        color="k" if (i, j) == bi else "w", fontweight="bold" if (i, j) == bi else None)
        ax.add_patch(plt.Rectangle((bi[1] - 0.5, bi[0] - 0.5), 1, 1, fill=False, ec="gold", lw=2.5))
        if 5 in list(hm.columns) and 0.0 in list(hm.index):  # base case marker
            ax.add_patch(plt.Rectangle((list(hm.columns).index(5) - 0.5, list(hm.index).index(0.0) - 0.5), 1, 1,
                                       fill=False, ec="red", lw=1.5, ls="--"))
        fig.colorbar(im, ax=ax, fraction=0.046)
    fig.suptitle("Augmentation / overbuild strategies (gold = best, red dashed = base case: no overbuild, augment every 5 y)", fontsize=10)
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)
