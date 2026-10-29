r"""Levelized Cost of Storage (LCOS).

.. math::

    LCOS = \frac{CAPEX_0 + \sum_{t=1}^{N}\frac{FOM_t + VOM_t + AUG_t + P^{ch}_t E^{ch}_t}{(1+r)^t}
                 - \frac{RV_N}{(1+r)^N}}
                {\sum_{t=1}^{N}\frac{E^{dis}_t}{(1+r)^t}}

with :math:`E^{ch}_t = E^{dis}_t/\eta_{RT}`. The charging term is split into
the value of the energy actually delivered (:math:`P^{ch}E^{dis}`) and the
round-trip-efficiency losses (:math:`P^{ch}E^{dis}(1/\eta_{RT}-1)`).

The *degradation* component is the increase in LCOS caused by lost
throughput: every cost item is first levelized over the throughput the
system would deliver with **no** fade (SOH = 1), and the difference to the
actual LCOS is reported as "degradation (lost throughput)". The components
therefore add up exactly to the LCOS.
"""
from __future__ import annotations

import numpy as np

from .cashflow import build_cashflows

COMPONENTS = [
    "Capex",
    "Fixed O&M",
    "Variable O&M",
    "Augmentation",
    "Charging energy",
    "RTE losses",
    "Residual value",
    "Degradation (lost throughput)",
]


def lcos_breakdown(p: dict) -> dict[str, float]:
    """LCOS components in $/MWh-discharged; ``total`` is their sum."""
    df = build_cashflows(p)
    r = p["discount_rate"]
    t = df["year"].to_numpy()
    d = 1.0 / (1.0 + r) ** t

    e_dis = df["discharged_mwh"].to_numpy()
    e_actual = np.sum(e_dis * d)
    # throughput with no fade: discharged / soh  (soh>0 in operating years)
    n = int(p["lifetime_years"])
    e_ideal_year = p["power_mw"] * p["duration_h"] * p["cycles_per_year"] * p["availability"]
    e_ideal = np.sum(np.r_[0.0, np.full(n, e_ideal_year)] * d)

    pv = lambda col: float(np.sum(-df[col].to_numpy() * d))  # costs as positive PV
    charge_delivered = float(np.sum(e_dis * p["charge_price_per_mwh"] * d))
    charge_total = pv("charging_cost")
    costs = {
        "Capex": pv("capex"),
        "Fixed O&M": pv("fixed_om"),
        "Variable O&M": pv("variable_om"),
        "Augmentation": pv("augmentation"),
        "Charging energy": charge_delivered,
        "RTE losses": charge_total - charge_delivered,
        "Residual value": pv("residual_value"),  # negative = credit
    }
    total_cost = sum(costs.values())
    lcos = total_cost / e_actual
    out = {k: v / e_ideal for k, v in costs.items()}
    out["Degradation (lost throughput)"] = lcos - sum(out.values())
    out["total"] = lcos
    out["pv_discharged_mwh"] = e_actual
    out["pv_total_cost_usd"] = total_cost
    return out


def lcos(p: dict) -> float:
    return lcos_breakdown(p)["total"]
