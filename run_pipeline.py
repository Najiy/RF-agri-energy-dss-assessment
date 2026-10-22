"""Runner for dss_pipeline (Tasks 1 to 3).

    python run_pipeline.py                              # verification suite (the spec's test locations)
    python run_pipeline.py --lat 38.0829 --lon -8.1252  # one custom site
    python run_pipeline.py --download                   # fetch data/PT.zip first

Per site it prints: Task 1 cluster -> Task 2 water demand, NASA sunshine, pump power
-> Task 3 pump tier, solar energy made vs used, surplus, and the same balance month by month.

Verification suite
    [1] Spec Alentejo point (38.015, -7.865): this parcel file has no parcel within 150 m, so we expect
        EmptyClusterError, then re-site to the nearest parcel [1b].
    [2] Alentejo cluster with mixed crops.
    [3] Central Lisbon (38.722, -9.139): urban, so EmptyClusterError is expected.

Exit code: 0 = everything worked, 1 = dataset, input or NASA error (or a custom site with no parcels).
"""

from __future__ import annotations

import argparse
import sys
import time
from collections.abc import Callable
from functools import partial
from pathlib import Path

import geopandas as gpd
import pandas as pd

import dss_pipeline as dss

SPEC_ALENTEJO = (38.015, -7.865)
SPEC_LISBON = (38.722, -9.139)
ALENTEJO_MIXED = (37.97883, -7.73698)  # found by scanning for a 150 m circle with several crop types

PAD = "         | "  # left gutter that nests tables under their task line


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    """Read command-line flags. --lat and --lon must come together."""
    parser = argparse.ArgumentParser(description="Agri-Energy DSS: clustering, NASA sunshine, pump and energy sizing.")
    parser.add_argument("--data", type=Path, default=dss.DEFAULT_DATA_PATH, help="path to PT.zip")
    parser.add_argument("--download", action="store_true", help="download PT.zip from Zenodo if missing")
    parser.add_argument("--lat", type=float, help="site latitude, decimal degrees (use with --lon)")
    parser.add_argument("--lon", type=float, help="site longitude, decimal degrees (use with --lat)")
    parser.add_argument("--nasa-timeout", type=float, default=10.0, help="seconds to wait for NASA POWER")

    args = parser.parse_args(argv)

    if (args.lat is None) != (args.lon is None):
        parser.error("--lat and --lon must be given together")

    return args


def _indent(text: str) -> str:
    """Put the left gutter in front of every line."""
    return "\n".join(PAD + line for line in text.splitlines())


def _table(frame: pd.DataFrame) -> str:
    """DataFrame as plain text: no row numbers, 2 decimals, indented under its task line."""
    return _indent(frame.to_string(index=False, float_format=lambda v: f"{v:,.2f}"))


def _parcel_table(cluster: dss.Cluster) -> str:
    """Task 1: one row per parcel: id, crop code and name, category, whole area, area inside the circle."""
    columns = {
        dss.ID_COL: "OSA_ID",
        dss.HCAT_COL: "HCAT",
        dss.LABEL_COL: "HCAT class",
        "crop_category": "cat",
        "area_ha": "parcel_ha",
        "area_in_zone_ha": "in_zone_ha",
    }
    present = {name: label for name, label in columns.items() if name in cluster.parcels.columns}

    return _table(cluster.parcels[list(present)].rename(columns=present))


def _demand_table(cluster: dss.Cluster) -> str:
    """Task 2.1: water demand per crop category."""
    table = dss.water_demand_table(cluster).rename(
        columns={
            "crop_category": "cat",
            "crop_category_name": "category",
            "water_demand_m3_ha_day": "WD m3/ha/day",
            "n_parcels": "n",
            "area_in_zone_ha": "in_zone_ha",
            "area_ha": "parcel_ha",
            "q_day_m3": "Q m3/day",
        }
    )

    return _table(table)


def _monthly_table(profile: pd.DataFrame) -> str:
    """Task 3: sunshine and surplus energy for each month, as two rows."""
    rows = profile[["psh_kwh_m2_day", "e_excess_kwh_day"]].T
    rows.index = ["PSH kWh/m2/day", "E_excess kWh/day"]

    return _indent(rows.to_string(float_format=lambda v: f"{v:.2f}"))


def _report(a: dss.SiteAssessment) -> None:
    """Print one site's results in task order: cluster, water and sunshine, pump and energy."""
    cluster, solar, energy, pump, params = a.cluster, a.solar, a.energy, a.energy.pump, a.params

    # Task 1
    print(
        f"  Task 1 | {cluster.n_parcels} parcel(s) | farmland in zone {cluster.area_in_zone_ha:.2f}"
        f" of {cluster.zone_ha:.2f} ha | whole parcels {cluster.parcel_area_ha:.2f} ha"
    )
    print(_parcel_table(cluster))
    print(PAD.rstrip())
    print(_demand_table(cluster))

    # Task 2
    print(f"  Task 2 | Q_day = {a.q_day_m3:,.1f} m3/day")
    print(
        f"{PAD}NASA POWER at ({solar.lat:.2f}, {solar.lon:.2f}): PSH_avg {solar.psh_avg:.2f} kWh/m2/day"
        f" | dullest month {solar.design_month} {solar.design_psh:.2f}"
    )
    print(
        f"{PAD}P_hyd = rho.g.Q.H / (3.6e6.t.eta) = {a.hydraulic_power_kw:.2f} kW"
        f"  (H {params.head_m:g} m, eta {params.pump_efficiency:g}, t_pump {params.pumping_hours:g} h/day)"
    )

    # Task 3
    fleet = f"Tier {pump.tier} x {pump.n_units}"
    if pump.multiple_pumps_required:
        fleet += "  [multiple pumps required]"

    print(
        f"  Task 3 | {fleet} for P_hyd {pump.required_power_kw:.2f} kW | motor {pump.total_motor_kw:g} kW"
        f" | PV {pump.total_pv_kwp:g} kWp | CAPEX ${pump.total_capex_usd:,.0f}"
    )

    pv_formula = f"{pump.total_pv_kwp:g} kWp x {energy.psh_avg_kwh_m2_day:.2f} h/day x {energy.pv_derate:g}"
    lines = (
        ("E_PV", pv_formula, energy.e_pv_kwh_day),
        ("E_pump", f"{pump.total_motor_kw:g} kW x {energy.pumping_hours:g} h/day", energy.e_pump_kwh_day),
        ("E_excess", "max(0, E_PV - E_pump)", energy.e_excess_kwh_day),
    )
    for name, formula, value in lines:
        print(f"{PAD}{name:<8} = {formula:<30} = {value:7.2f} kWh/day")

    # Zero surplus is a result, not a failure: say by how much and what sunshine would be needed.
    if energy.e_excess_kwh_day == 0:
        short = f"short by {energy.e_deficit_kwh_day:.2f} kWh/day; needs PSH > {energy.breakeven_psh:.2f}"
        print(f"{PAD}no surplus at PSH_avg: {short}")

    profile = a.monthly_profile()
    surplus_months = [month for month, value in profile["e_excess_kwh_day"].items() if value > 0]
    print(f"{PAD}by month, surplus in: {', '.join(surplus_months) or 'none'}")
    print(_monthly_table(profile))


def _site(
    parcels: gpd.GeoDataFrame,
    label: str,
    lat: float,
    lon: float,
    params: dss.DesignParameters,
    get_solar: Callable[[float, float], dss.SolarResource],
) -> bool:
    """Evaluate and print one site. True if it was evaluated, False if its command zone was empty."""
    print(f"\n{label}  ({lat:.5f}, {lon:.5f})")
    start = time.perf_counter()

    try:
        assessment = dss.evaluate_site(parcels, lat, lon, params, get_solar=get_solar)
    except dss.EmptyClusterError as exc:
        print(f"  EmptyClusterError: {exc}")

        hint = dss.nearest_parcel(parcels, lat, lon)
        if hint is not None:
            where = f"({hint.lat:.5f}, {hint.lon:.5f})"
            print(f"  nearest parcel {hint.parcel_id} is {hint.distance_m:,.0f} m away at {where}")

        return False

    _report(assessment)
    print(f"  done in {(time.perf_counter() - start) * 1e3:.0f} ms (includes the NASA request unless cached)")

    return True


def main(argv: list[str] | None = None, get_solar: Callable[[float, float], dss.SolarResource] | None = None) -> int:
    """Load the parcels, then run one custom site or the verification suite. Returns the exit code.

    `get_solar` replaces the NASA request (used by tests to run offline).
    """
    args = _parse_args(argv)

    if get_solar is None:
        get_solar = partial(dss.fetch_solar_resource, timeout_s=args.nasa_timeout)

    try:
        path = dss.ensure_dataset(args.data) if args.download else args.data
        start = time.perf_counter()
        parcels = dss.load_parcels(path)
    except dss.DatasetError as exc:
        print(f"Dataset error: {exc}", file=sys.stderr)
        return 1

    params = dss.DesignParameters()
    load_s = time.perf_counter() - start
    print(f"EuroCrops PT | {len(parcels):,} parcels | loaded in {load_s:.1f} s | CRS {dss.PT_TM06}")
    print(
        f"Design | radius {params.radius_m:g} m | head {params.head_m:g} m | eta {params.pump_efficiency:g}"
        f" | t_pump {params.pumping_hours:g} h/day | PV derate {params.pv_derate:g}"
    )
    print(f"Solar | NASA POWER climatology at each cluster centre (timeout {args.nasa_timeout:g} s)")

    try:
        if args.lat is not None:
            return 0 if _site(parcels, "Custom site", args.lat, args.lon, params, get_solar) else 1

        # Suite: [1] and [3] are expected to find no parcels; only [1] triggers the re-site.
        if not _site(parcels, "[1] Spec Alentejo point", *SPEC_ALENTEJO, params, get_solar):
            hint = dss.nearest_parcel(parcels, *SPEC_ALENTEJO)
            _site(parcels, "[1b] Alentejo, re-sited to nearest parcel", hint.lat, hint.lon, params, get_solar)

        _site(parcels, "[2] Alentejo mixed-crop cluster", *ALENTEJO_MIXED, params, get_solar)
        _site(parcels, "[3] Central Lisbon (urban)", *SPEC_LISBON, params, get_solar)
    except (TypeError, ValueError) as exc:  # bad coordinates or timeout value
        print(f"Invalid input: {exc}", file=sys.stderr)
        return 1
    except dss.SolarDataError as exc:
        print(f"NASA POWER error: {exc}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
