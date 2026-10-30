"""Annual energy throughput, state of health and augmentation schedule."""
from __future__ import annotations

import numpy as np


def _mean_capped(a: float, b: float, cap: float = 1.0) -> float:
    """Exact mean of min(x, cap) for x falling linearly from ``a`` to ``b`` (a >= b) over one year."""
    if b >= cap:
        return cap
    if a <= cap:
        return 0.5 * (a + b)
    f = (a - cap) / (a - b)          # share of the year spent above the cap
    return f * cap + (1.0 - f) * 0.5 * (cap + b)


def energy_profile(p: dict) -> dict[str, np.ndarray]:
    """Return per-operating-year arrays (index 0 = operating year 1).

    * ``soh``: usable-energy fraction of nameplate during the year (time average, capped at 1).
    * ``installed``: installed-energy fraction of nameplate at the end of the year, before augmentation.
    * ``discharged_mwh``: energy delivered to the grid.
    * ``charged_mwh``: energy drawn from the grid (= discharged / RTE).
    * ``aug_mwh``: energy capacity added at the *end* of that year.

    Fade is linear in installed energy (``annual_fade`` of nameplate per year). ``initial_overbuild``
    (default 0) installs extra energy on day 1; usable energy is capped at nameplate (power x duration),
    so the overbuild only delays the loss of usable capacity. At each augmentation year the installed
    energy is topped back up to the design level (1 + overbuild).
    """
    n = int(p["lifetime_years"])
    e_nom = p["power_mw"] * p["duration_h"]
    fade = float(p["annual_fade"])
    ob = float(p.get("initial_overbuild", 0.0))
    if ob < 0:
        raise ValueError("initial_overbuild must be >= 0")
    design = 1.0 + ob
    aug_years = set(int(y) for y in p["augmentation_years"])

    soh = np.empty(n)
    installed = np.empty(n)
    aug_mwh = np.zeros(n)
    level = design  # installed energy at the start of the year (fraction of nameplate)
    for i in range(n):
        end = max(level - fade, 0.0)
        soh[i] = _mean_capped(level, end)
        installed[i] = end
        year = i + 1
        if year in aug_years and year < n:
            aug_mwh[i] = (design - end) * e_nom
            level = design
        else:
            level = end

    discharged = e_nom * p["cycles_per_year"] * p["availability"] * soh
    charged = discharged / p["round_trip_efficiency"]
    return {"soh": soh, "installed": installed, "discharged_mwh": discharged, "charged_mwh": charged, "aug_mwh": aug_mwh}
