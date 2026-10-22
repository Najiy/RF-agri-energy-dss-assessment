"""Task 2.1 / 2.3 formulas (water demand, pump power) and Task 3 (pump tier, solar energy, surplus)."""

from __future__ import annotations

import dataclasses
import math

import pandas as pd
import pytest

import dss_pipeline as dss

KW_PER_M3_DAY = 1000 * 9.81 * 40 / (3.6e6 * 6 * 0.55)  # pump power per m3/day with the spec's default values


# --- Task 2.1 and 2.3: water demand and pump power ------------------------------------------------

def test_hydraulic_power_reference_value():
    # 1000 * 9.81 * 245 * 40 / (3.6e6 * 6 * 0.55) = 96,138,000 / 11,880,000
    assert dss.hydraulic_power_kw(245) == pytest.approx(8.092424, rel=1e-6)
    assert dss.hydraulic_power_kw(0) == 0.0


def test_hydraulic_power_scales_as_physics_says():
    base = dss.hydraulic_power_kw(100)

    assert dss.hydraulic_power_kw(200) == pytest.approx(2 * base)  # twice the water, twice the power
    assert dss.hydraulic_power_kw(100, head_m=80) == pytest.approx(2 * base)  # twice the lift, twice the power
    assert dss.hydraulic_power_kw(100, pumping_hours=12) == pytest.approx(base / 2)  # same water over twice the time
    assert dss.hydraulic_power_kw(100, efficiency=1.0) == pytest.approx(base * 0.55)  # perfect pump needs less


@pytest.mark.parametrize(
    "kwargs",
    [
        {"q_day_m3": -1},
        {"q_day_m3": math.nan},
        {"q_day_m3": 100, "head_m": 0},
        {"q_day_m3": 100, "efficiency": 0},
        {"q_day_m3": 100, "efficiency": 1.2},
        {"q_day_m3": 100, "pumping_hours": 0},
        {"q_day_m3": 100, "pumping_hours": 25},
    ],
)
def test_hydraulic_power_rejects_bad_input(kwargs):
    with pytest.raises(ValueError):
        dss.hydraulic_power_kw(**kwargs)


def test_daily_water_demand(synthetic_parcels, site, expected):
    cluster = dss.extract_cluster(synthetic_parcels, *site)

    assert dss.daily_water_demand_m3(cluster) == pytest.approx(expected.q_zone_m3, rel=1e-4)


def test_water_demand_table(synthetic_parcels, site):
    table = dss.water_demand_table(dss.extract_cluster(synthetic_parcels, *site)).set_index("crop_category")

    assert list(table.index) == ["10", "20", "30", "50", dss.FALLBACK]
    assert table.loc["50", "q_day_m3"] == pytest.approx(0.4 * 55)
    assert table["n_parcels"].sum() == 5


# --- Task 3.1: pump tier --------------------------------------------------------------------------

def test_pump_catalog_matches_reference_table_2():
    ratings = dss.PUMP_CATALOG[["motor_kw", "pv_kwp", "capex_usd", "max_delivery_m3_day"]].values.tolist()

    assert ratings == [[3.7, 5.0, 7500, 250], [5.5, 7.5, 10500, 400], [7.5, 10.0, 14000, 580], [11.0, 15.0, 19500, 850]]


@pytest.mark.parametrize(
    "power_kw, tier",
    [(0.5, 1), (3.7, 1), (3.7001, 2), (5.5, 2), (5.51, 3), (7.5, 3), (7.6, 4), (11.0, 4)],
)
def test_smallest_tier_that_covers_the_power(power_kw, tier):
    pump = dss.select_pump(power_kw, q_day_m3=100)

    assert (pump.tier, pump.n_units, pump.multiple_pumps_required) == (tier, 1, False)


def test_daily_delivery_is_also_enforced():
    # 3.0 kW fits Tier 1's motor, but 300 m3/day is more than Tier 1's 250 m3/day.
    assert dss.select_pump(3.0, 300).tier == 2


@pytest.mark.parametrize(
    "power_kw, q_day_m3, n_units",
    [
        (12.85, 389, 2),  # power needs 12.85 / 11 -> 2 units
        (22.0, 700, 2),
        (25.0, 800, 3),
        (11.5, 100, 2),  # just over one Tier 4 motor
        (5.0, 1800, 3),  # water, not power, is the limit: 1800 / 850 -> 3 units
        (67.56, 2045.5, 7),
    ],
)
def test_beyond_tier_4_repeats_tier_4(power_kw, q_day_m3, n_units):
    pump = dss.select_pump(power_kw, q_day_m3)

    assert (pump.tier, pump.n_units, pump.multiple_pumps_required) == (4, n_units, True)
    assert pump.total_motor_kw >= power_kw
    assert pump.total_capex_usd == n_units * 19_500


@pytest.mark.parametrize("power_kw, q_day_m3", [(0, 100), (-1, 100), (math.nan, 100), (3, 0)])
def test_select_pump_rejects_bad_input(power_kw, q_day_m3):
    with pytest.raises(ValueError):
        dss.select_pump(power_kw, q_day_m3)


def test_catalog_can_be_swapped():
    single = pd.DataFrame([{"tier": 9, "motor_kw": 2.0, "pv_kwp": 3.0, "capex_usd": 5000, "max_delivery_m3_day": 120}])

    pump = dss.select_pump(5.0, 100, single)

    assert (pump.tier, pump.n_units) == (9, 3)  # 5 kW / 2 kW per unit -> 3 units


def test_select_pump_leaves_the_catalog_untouched():
    before = dss.PUMP_CATALOG.copy()

    dss.select_pump(25.0, 800)

    pd.testing.assert_frame_equal(dss.PUMP_CATALOG, before)


# --- Task 3.2 to 3.4: energy ----------------------------------------------------------------------

def test_energy_reference_values():
    assert dss.pv_energy_kwh_day(5.0, 4.90) == pytest.approx(18.375)  # kWp x hours x derate
    assert dss.pump_energy_kwh_day(3.7) == pytest.approx(22.2)  # kW x hours
    assert dss.surplus_energy_kwh_day(18.375, 22.2) == 0.0  # clamped at zero, never negative
    assert dss.surplus_energy_kwh_day(dss.pv_energy_kwh_day(5.0, 7.0), 22.2) == pytest.approx(4.05)


@pytest.mark.parametrize(
    "call",
    [
        lambda: dss.pv_energy_kwh_day(-1, 5),
        lambda: dss.pv_energy_kwh_day(5, -1),
        lambda: dss.pv_energy_kwh_day(5, 5, derate=0),
        lambda: dss.pump_energy_kwh_day(-1),
        lambda: dss.pump_energy_kwh_day(3.7, 30),
        lambda: dss.surplus_energy_kwh_day(-1, 0),
    ],
)
def test_energy_functions_reject_bad_input(call):
    with pytest.raises(ValueError):
        call()


def test_energy_balance_with_no_surplus():
    balance = dss.model_energy_balance(2.2394, 67.8, 4.90)

    assert balance.pump.tier == 1
    assert (balance.e_pv_kwh_day, balance.e_pump_kwh_day) == pytest.approx((18.375, 22.2))
    assert balance.e_excess_kwh_day == 0.0
    assert balance.e_deficit_kwh_day == pytest.approx(3.825)
    assert balance.breakeven_psh == pytest.approx(22.2 / (5.0 * 0.75))


def test_breakeven_psh_is_where_surplus_starts():
    psh0 = dss.model_energy_balance(2.2394, 67.8, 4.90).breakeven_psh

    assert dss.model_energy_balance(2.2394, 67.8, psh0).e_excess_kwh_day == pytest.approx(0.0, abs=1e-9)

    above = dss.model_energy_balance(2.2394, 67.8, psh0 + 0.1)
    assert above.e_excess_kwh_day == pytest.approx(5.0 * 0.1 * 0.75)
    assert above.e_deficit_kwh_day == 0.0


def test_several_pumps_scale_the_energy():
    balance = dss.model_energy_balance(25.0, 800, 6.5)  # 3 x Tier 4

    assert (balance.pump.tier, balance.pump.n_units) == (4, 3)
    assert balance.e_pv_kwh_day == pytest.approx(3 * 15.0 * 6.5 * 0.75)
    assert balance.e_pump_kwh_day == pytest.approx(3 * 11.0 * 6)
    assert balance.e_excess_kwh_day == pytest.approx(219.375 - 198.0)


# --- All tasks together ---------------------------------------------------------------------------

def test_evaluate_site_chains_the_tasks(synthetic_parcels, site, expected, stub_solar):
    a = dss.evaluate_site(synthetic_parcels, *site, get_solar=stub_solar)

    assert a.q_day_m3 == pytest.approx(expected.q_zone_m3, rel=1e-4)
    assert a.hydraulic_power_kw == pytest.approx(expected.q_zone_m3 * KW_PER_M3_DAY, rel=1e-4)
    assert a.energy.pump.tier == 1  # ~96 m3/day needs ~3.2 kW
    assert a.energy.e_excess_kwh_day == 0.0  # 5 kWp x 4.90 h x 0.75 is less than 3.7 kW x 6 h


def test_evaluate_site_applies_design_parameters(synthetic_parcels, site, stub_solar):
    params = dataclasses.replace(dss.DEFAULT_PARAMS, pumping_hours=8)

    a = dss.evaluate_site(synthetic_parcels, *site, params, get_solar=stub_solar)

    assert a.hydraulic_power_kw == pytest.approx(dss.hydraulic_power_kw(a.q_day_m3, pumping_hours=8))
    assert a.energy.e_pump_kwh_day == pytest.approx(a.energy.pump.total_motor_kw * 8)  # same hours in both places


def test_design_parameters_are_frozen():
    with pytest.raises(dataclasses.FrozenInstanceError):
        dss.DEFAULT_PARAMS.head_m = 60  # type: ignore[misc]

    assert dataclasses.replace(dss.DEFAULT_PARAMS, head_m=60).head_m == 60
