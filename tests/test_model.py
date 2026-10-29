import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from bess_tea.cashflow import build_cashflows, capex_usd, irr, irr_vectorised, npv, payback_years  # noqa: E402
from bess_tea.config import base_params, load_config  # noqa: E402
from bess_tea.lcos import COMPONENTS, lcos_breakdown  # noqa: E402
from bess_tea.montecarlo import sample_inputs  # noqa: E402
from bess_tea.physics import energy_profile  # noqa: E402


@pytest.fixture
def p():
    return base_params(load_config())


def test_capex_matches_nrel_formula(p):
    # (241 $/kWh * 2 h + 372 $/kW) * 0.92 * 100,000 kW
    assert capex_usd(p) == pytest.approx((241 * 2 + 372) * 0.92 * 100_000)


def test_irr_known_annuity():
    flows = np.r_[-100.0, np.full(10, 16.27454)]  # 10-yr annuity at ~10%
    assert irr(flows) == pytest.approx(0.10, abs=1e-5)
    assert npv(0.10, flows) == pytest.approx(0.0, abs=1e-3)
    v = irr_vectorised(np.vstack([flows, flows]))
    assert np.allclose(v, 0.10, atol=1e-5)


def test_payback_interpolates():
    assert payback_years(np.array([-100.0, 40.0, 40.0, 40.0])) == pytest.approx(2.5)


def test_irr_npv_consistent(p):
    flows = build_cashflows(p)["net_cash_flow"].to_numpy()
    assert npv(irr(flows), flows) == pytest.approx(0.0, abs=1.0)


def test_lcos_components_sum(p):
    bd = lcos_breakdown(p)
    assert sum(bd[k] for k in COMPONENTS) == pytest.approx(bd["total"])
    assert bd["pv_total_cost_usd"] / bd["pv_discharged_mwh"] == pytest.approx(bd["total"])


def test_no_degradation_means_no_aug_and_no_penalty(p):
    q = dict(p, annual_fade=0.0)
    bd = lcos_breakdown(q)
    assert bd["Augmentation"] == pytest.approx(0.0)
    assert bd["Degradation (lost throughput)"] == pytest.approx(0.0, abs=1e-9)
    assert lcos_breakdown(p)["total"] > bd["total"]


def test_rte_losses(p):
    bd = lcos_breakdown(dict(p, round_trip_efficiency=1.0))
    assert bd["RTE losses"] == pytest.approx(0.0, abs=1e-9)


def test_augmentation_restores_capacity(p):
    prof = energy_profile(p)
    e_nom = p["power_mw"] * p["duration_h"]
    assert prof["aug_mwh"][4] == pytest.approx(5 * p["annual_fade"] * e_nom)
    assert prof["soh"][5] == pytest.approx(1 - p["annual_fade"] / 2)


def test_sampling_reproducible():
    d = {"x": {"dist": "triangular", "low": 0, "mode": 1, "high": 2}}
    a, b = sample_inputs(d, 100, 7), sample_inputs(d, 100, 7)
    assert a.equals(b)
