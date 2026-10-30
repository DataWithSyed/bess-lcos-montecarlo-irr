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


# ---- augmentation / overbuild strategies -----------------------------------------------------
from bess_tea.augmentation import augmentation_years, evaluate_strategy, robustness, strategy_grid  # noqa: E402
from bess_tea.physics import _mean_capped  # noqa: E402


def test_new_options_default_to_previous_model(p):
    q = dict(p, initial_overbuild=0.0, capacity_credit_tracks_soh=False)
    assert capex_usd(q) == pytest.approx(capex_usd(p))
    assert np.allclose(build_cashflows(q)["net_cash_flow"], build_cashflows(p)["net_cash_flow"])


def test_mean_capped_is_exact():
    assert _mean_capped(1.2, 1.1) == 1.0
    assert _mean_capped(0.9, 0.8) == pytest.approx(0.85)
    assert _mean_capped(1.01, 0.99) == pytest.approx(0.5 * 1.0 + 0.5 * 0.995)


def test_overbuild_delays_fade_and_costs_energy_capex(p):
    q = dict(p, initial_overbuild=0.10, augmentation_years=[])
    prof = energy_profile(q)
    assert np.allclose(prof["soh"][:5], 1.0)                 # 10 % headroom / 2 %/yr = 5 full years
    assert prof["soh"][5] == pytest.approx(0.99)
    extra = p["energy_cost_per_kwh"] * p["duration_h"] * 0.10 * p["cost_multiplier"] * p["power_mw"] * 1000
    assert capex_usd(q) - capex_usd(p) == pytest.approx(extra)
    aug = energy_profile(dict(q, augmentation_years=[5]))
    assert aug["aug_mwh"][4] == pytest.approx(0.10 * p["power_mw"] * p["duration_h"])  # back to design level 1.1


def test_augmentation_schedule():
    assert augmentation_years(5, 15) == [5, 10]
    assert augmentation_years(4, 15) == [4, 8, 12]
    assert augmentation_years(0, 15) == []
    assert len(strategy_grid([0, 5], [0.0, 0.1], 15)) == 4


def test_soh_linked_capacity_revenue(p):
    fixed = build_cashflows(p)["capacity_revenue"].to_numpy()
    linked = build_cashflows(dict(p, capacity_credit_tracks_soh=True))["capacity_revenue"].to_numpy()
    assert np.allclose(linked[1:], fixed[1:] * energy_profile(p)["soh"])
    nofade = dict(p, annual_fade=0.0)
    assert np.allclose(build_cashflows(dict(nofade, capacity_credit_tracks_soh=True))["capacity_revenue"],
                       build_cashflows(nofade)["capacity_revenue"])


def test_strategy_reproduces_base_case(p):
    s = {"strategy": "base", "initial_overbuild": 0.0, "interval_years": 5, "augmentation_years": [5, 10]}
    r = evaluate_strategy(p, s)
    assert r["lcos_usd_per_mwh"] == pytest.approx(lcos_breakdown(p)["total"])
    assert r["npv_musd"] * 1e6 == pytest.approx(npv(p["discount_rate"], build_cashflows(p)["net_cash_flow"].to_numpy()))


def test_robustness_shares_sum_to_one(p):
    cfg = load_config()
    st = strategy_grid([0, 5], [0.0, 0.1], int(p["lifetime_years"]))
    rob = robustness(p, st, cfg["monte_carlo"]["distributions"], 20, 1)
    assert rob["share_best_npv"].sum() == pytest.approx(1.0)
    assert (rob["mean_regret_musd"] >= 0).all()
