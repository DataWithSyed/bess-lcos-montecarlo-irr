"""One-at-a-time (tornado) sensitivity on IRR, NPV and LCOS."""
from __future__ import annotations

import pandas as pd

from .cashflow import project_metrics
from .lcos import lcos


def tornado(base: dict, swings: dict[str, list[float]]) -> pd.DataFrame:
    base_m = project_metrics(base)
    base_l = lcos(base)
    rows = []
    for name, (lo, hi) in swings.items():
        rec = {"parameter": name, "base_value": base[name], "low_value": lo, "high_value": hi}
        for tag, val in (("low", lo), ("high", hi)):
            p = dict(base, **{name: val})
            m = project_metrics(p)
            rec[f"irr_{tag}"] = m["irr"]
            rec[f"npv_{tag}"] = m["npv_usd"]
            rec[f"lcos_{tag}"] = lcos(p)
        rec["irr_swing"] = abs(rec["irr_high"] - rec["irr_low"])
        rec["npv_swing"] = abs(rec["npv_high"] - rec["npv_low"])
        rec["lcos_swing"] = abs(rec["lcos_high"] - rec["lcos_low"])
        rows.append(rec)
    df = pd.DataFrame(rows)
    df.attrs.update(base_irr=base_m["irr"], base_npv=base_m["npv_usd"], base_lcos=base_l)
    return df
