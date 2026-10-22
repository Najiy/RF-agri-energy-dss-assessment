"""Task 2.1 / 2.3 formulas (demand, hydraulic power) and Task 3: tier matching, PV and pump energy, surplus."""

from __future__ import annotations

import dataclasses
import math

import pandas as pd
import pytest

import dss_pipeline as dss

KW_PER_M3_DAY = 1000 * 9.81 * 40 / (3.6e6 * 6 * 0.55)  # spec defaults: P [kW] = Q [m3/day] x this


# --- Bridge: demand and hydraulic power -----------------------------------------------------------
def test_hydraulic_power_reference_value():
    # 1000 * 9.81 * 245 * 40 / (3.6e6 * 6 * 0.55) = 96,138,000 / 11,880,000
    assert dss.hydraulic_power_kw(245) == pytest.approx(8.092424, rel=1e-6)
    assert dss.hydraulic_power_kw(0) == 0.0


def test_hydraulic_power_dimensional_scaling():
    base = dss.hydraulic_power_kw(100)
    assert dss.hydraulic_power_kw(200) == pytest.approx(2 * base)  # proportional to Q
    assert dss.hydraulic_power_kw(100, head_m=80) == pytest.approx(2 * base)  # proportional to H
    assert dss.hydraulic_power_kw(100, pumping_hours=12) == pytest.approx(base / 2)  # same volume, longer window
    assert dss.hydraulic_power_kw(100, efficiency=1.0) == pytest.approx(base * 0.55)  # eta inflates input power


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
def test_hydraulic_power_rejects_invalid(kwargs):
    with pytest.raises(ValueError):
        dss.hydraulic_power_kw(**kwargs)


def test_daily_water_demand_by_area_basis(synthetic_parcels, site, expected):
    cluster = dss.extract_cluster(synthetic_parcels, *site)
    assert dss.daily_water_demand_m3(cluster) == pytest.approx(expected.q_zone_m3, rel=1e-4)
    assert dss.daily_water_demand_m3(cluster, "parcel") == pytest.approx(expected.q_parcel_m3)


def test_water_demand_table(synthetic_parcels, site):
    table = dss.water_demand_table(dss.extract_cluster(synthetic_parcels, *site)).set_index("crop_category")
    assert list(table.index) == ["10", "20", "30", "50", dss.FALLBACK]
    assert table.loc["50", "q_day_m3"] == pytest.approx(0.4 * 55)
    assert table["n_parcels"].sum() == 5


def test_invalid_area_basis(synthetic_parcels, site):
    with pytest.raises(ValueError, match="area_basis"):
        dss.daily_water_demand_m3(dss.extract_cluster(synthetic_parcels, *site), "hectares")


# --- Task 3.1: tier matching ----------------------------------------------------------------------
def test_pump_catalog_matches_reference_table_2():
    ratings = dss.PUMP_CATALOG[["motor_kw", "pv_kwp", "capex_usd", "max_delivery_m3_day"]].values.tolist()
    assert ratings == [[3.7, 5.0, 7500, 250], [5.5, 7.5, 10500, 400], [7.5, 10.0, 14000, 580], [11.0, 15.0, 19500, 850]]


@pytest.mark.parametrize(
    "power_kw, tier",
    [(0.5, 1), (3.7, 1), (3.7001, 2), (5.5, 2), (5.51, 3), (7.5, 3), (7.6, 4), (11.0, 4)],
)
def test_smallest_tier_meeting_motor_power(power_kw, tier):
    pump = dss.select_pump(power_kw, q_day_m3=100)
    assert (pump.tier, pump.n_units, pump.multiple_pumps_required) == (tier, 1, False)


def test_tier_boundary_survives_float_noise():
    q = 3.7 / KW_PER_M3_DAY  # demand whose power is 3.7 kW up to rounding
    assert dss.select_pump(dss.hydraulic_power_kw(q), q).tier == 1


def test_rated_delivery_is_also_enforced():
    # 3.0 kW fits Tier 1's motor, but 300 m3/day exceeds its 250 m3/day rating.
    assert dss.select_pump(3.0, 300).tier == 2


@pytest.mark.parametrize(
    "power_kw, q_day_m3, tier, n_units",
    [
        (12.85, 389, 3, 2),  # 2 x 7.5 kW suffices; 2 x Tier 4 would over-build
        (15.0, 1000, 3, 2),  # exactly 7.5 kW and 500 m3/day per unit
        (22.0, 700, 4, 2),
        (25.0, 800, 4, 3),
        (67.56, 2045.5, 4, 7),  # whole-parcel basis at the Alentejo mixed site
    ],
)
def test_multiple_pumps_beyond_tier_4(power_kw, q_day_m3, tier, n_units):
    pump = dss.select_pump(power_kw, q_day_m3)
    assert (pump.tier, pump.n_units, pump.multiple_pumps_required) == (tier, n_units, True)
    assert pump.total_motor_kw >= power_kw and pump.total_delivery_m3_day >= q_day_m3
    assert pump.total_capex_usd == n_units * pump.capex_usd


@pytest.mark.parametrize(
    "power_kw, q_day_m3, error",
    [
        (0, 100, ValueError),
        (-1, 100, ValueError),
        (math.nan, 100, ValueError),
        (3, 0, ValueError),
        ("3", 100, TypeError),
    ],
)
def test_select_pump_rejects_invalid(power_kw, q_day_m3, error):
    with pytest.raises(error):
        dss.select_pump(power_kw, q_day_m3)


def test_catalog_is_injectable_and_validated():
    single = pd.DataFrame([{"tier": 9, "motor_kw": 2.0, "pv_kwp": 3.0, "capex_usd": 5000, "max_delivery_m3_day": 120}])
    pump = dss.select_pump(5.0, 100, single)
    assert (pump.tier, pump.n_units) == (9, 3)
    with pytest.raises(ValueError, match="lacks columns"):
        dss.select_pump(1.0, 10, single.drop(columns="pv_kwp"))
    with pytest.raises(ValueError, match="empty"):
        dss.select_pump(1.0, 10, single.iloc[0:0])
    with pytest.raises(ValueError, match="positive"):
        dss.select_pump(1.0, 10, single.assign(motor_kw=0.0))


def test_select_pump_leaves_catalog_untouched():
    before = dss.PUMP_CATALOG.copy()
    dss.select_pump(25.0, 800)
    pd.testing.assert_frame_equal(dss.PUMP_CATALOG, before)


# --- Task 3.2-3.4: energy -------------------------------------------------------------------------
def test_energy_reference_values():
    assert dss.pv_energy_kwh_day(5.0, 4.90) == pytest.approx(18.375)  # kWp x h/day x derate
    assert dss.pump_energy_kwh_day(3.7) == pytest.approx(22.2)  # kW x h/day
    assert dss.surplus_energy_kwh_day(18.375, 22.2) == 0.0  # clamped, never negative
    assert dss.surplus_energy_kwh_day(dss.pv_energy_kwh_day(5.0, 7.0), 22.2) == pytest.approx(4.05)


def test_psh_in_megajoules_is_rejected():
    with pytest.raises(ValueError, match="MJ"):
        dss.pv_energy_kwh_day(5.0, 17.65)  # NASA POWER community=AG annual value at the Alentejo point
    assert dss.pv_energy_kwh_day(5.0, 17.65 / 3.6) == pytest.approx(5.0 * 4.90278 * 0.75, rel=1e-5)


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
def test_energy_inputs_validated(call):
    with pytest.raises(ValueError):
        call()


def test_energy_balance_zero_surplus():
    balance = dss.model_energy_balance(2.2394, 67.8, 4.90)
    assert balance.pump.tier == 1
    assert (balance.e_pv_kwh_day, balance.e_pump_kwh_day) == pytest.approx((18.375, 22.2))
    assert balance.e_excess_kwh_day == 0.0
    assert balance.e_deficit_kwh_day == pytest.approx(3.825)
    assert balance.breakeven_psh == pytest.approx(22.2 / (5.0 * 0.75))


def test_breakeven_psh_is_the_surplus_threshold():
    psh0 = dss.model_energy_balance(2.2394, 67.8, 4.90).breakeven_psh
    assert dss.model_energy_balance(2.2394, 67.8, psh0).e_excess_kwh_day == pytest.approx(0.0, abs=1e-9)
    above = dss.model_energy_balance(2.2394, 67.8, psh0 + 0.1)
    assert above.e_excess_kwh_day == pytest.approx(5.0 * 0.1 * 0.75)
    assert above.e_deficit_kwh_day == 0.0


def test_fleet_energy_scales_with_units():
    balance = dss.model_energy_balance(15.0, 1000, 6.5)
    assert (balance.pump.tier, balance.pump.n_units) == (3, 2)
    assert balance.e_pv_kwh_day == pytest.approx(2 * 10.0 * 6.5 * 0.75)
    assert balance.e_pump_kwh_day == pytest.approx(2 * 7.5 * 6)
    assert balance.e_excess_kwh_day == pytest.approx(97.5 - 90.0)


# --- End to end -----------------------------------------------------------------------------------
def test_evaluate_site_chains_tasks(synthetic_parcels, site, expected, stub_solar):
    a = dss.evaluate_site(synthetic_parcels, *site, solar_client=stub_solar)
    assert a.q_day_m3 == pytest.approx(expected.q_zone_m3, rel=1e-4)
    assert a.hydraulic_power_kw == pytest.approx(expected.q_zone_m3 * KW_PER_M3_DAY, rel=1e-4)
    assert a.energy.pump.tier == 1  # ~96 m3/day -> ~3.2 kW
    assert a.energy.e_excess_kwh_day == 0.0  # 5 kWp x 4.90 h x 0.75 < 3.7 kW x 6 h


def test_evaluate_site_applies_design_parameters(synthetic_parcels, site, expected, stub_solar):
    params = dss.DesignParameters(area_basis="parcel", pumping_hours=8)
    a = dss.evaluate_site(synthetic_parcels, *site, params, solar_client=stub_solar)
    assert a.q_day_m3 == pytest.approx(expected.q_parcel_m3)
    assert a.hydraulic_power_kw == pytest.approx(dss.hydraulic_power_kw(expected.q_parcel_m3, pumping_hours=8))
    assert a.energy.e_pump_kwh_day == pytest.approx(a.energy.pump.total_motor_kw * 8)  # same t_pump both sides


@pytest.mark.parametrize(
    "name, value",
    [
        ("pump_efficiency", 0),
        ("pump_efficiency", 1.5),
        ("pumping_hours", 0),
        ("pv_derate", 1.1),
        ("head_m", -40),
        ("radius_m", 0),
        ("area_basis", "hectares"),
    ],
)
def test_design_parameters_validated(name, value):
    with pytest.raises(ValueError):
        dss.DesignParameters(**{name: value})


def test_design_parameters_are_immutable():
    with pytest.raises(dataclasses.FrozenInstanceError):
        dss.DEFAULT_PARAMS.head_m = 60  # type: ignore[misc]
    assert dataclasses.replace(dss.DEFAULT_PARAMS, head_m=60).head_m == 60
