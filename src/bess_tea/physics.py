"""Annual energy throughput, state of health and augmentation schedule."""
from __future__ import annotations

import numpy as np


def energy_profile(p: dict) -> dict[str, np.ndarray]:
    """Return per-operating-year arrays (index 0 = operating year 1).

    * ``soh``: usable-energy fraction during the year (average of start/end).
    * ``discharged_mwh``: energy delivered to the grid.
    * ``charged_mwh``: energy drawn from the grid (= discharged / RTE).
    * ``aug_mwh``: energy capacity added at the *end* of that year.

    Fade is linear in usable energy; at each augmentation year the lost
    capacity is topped back up to 100 % of nameplate.
    """
    n = int(p["lifetime_years"])
    e_nom = p["power_mw"] * p["duration_h"]
    fade = float(p["annual_fade"])
    aug_years = set(int(y) for y in p["augmentation_years"])

    soh = np.empty(n)
    aug_mwh = np.zeros(n)
    level = 1.0  # SOH at the start of the year
    for i in range(n):
        end = max(level - fade, 0.0)
        soh[i] = 0.5 * (level + end)
        year = i + 1
        if year in aug_years and year < n:
            aug_mwh[i] = (1.0 - end) * e_nom
            level = 1.0
        else:
            level = end

    discharged = e_nom * p["cycles_per_year"] * p["availability"] * soh
    charged = discharged / p["round_trip_efficiency"]
    return {"soh": soh, "discharged_mwh": discharged, "charged_mwh": charged, "aug_mwh": aug_mwh}
