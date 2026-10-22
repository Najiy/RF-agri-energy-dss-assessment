"""Execution runner for dss_pipeline (Tasks 1-3).

    python run_pipeline.py                                   # verification suite (spec sites)
    python run_pipeline.py --lat 38.0829 --lon -8.1252       # one custom site
    python run_pipeline.py --download                        # fetch data/PT.zip first

Per site: Task 1 cluster -> Task 2 demand, NASA POWER climatology at the cluster centroid, hydraulic power
-> Task 3 tier, E_PV from Task 2's PSH_avg, E_excess, and the same balance month by month.

Verification suite
    [1] Spec Alentejo point (38.015, -7.865). This release has no parcel within 150 m (A8) -> the error,
        then a re-site to the nearest parcel, evaluated end to end.
    [2] Alentejo olive/fallow cluster: mixed categories; zone vs whole-parcel area (A2).
    [3] Central Lisbon (38.722, -9.139): urban -> EmptyClusterError.

Exit codes: 0 ok | 1 dataset error | 2 invalid input, or custom site without parcels | 3 NASA POWER unavailable.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

import geopandas as gpd
import pandas as pd

import dss_pipeline as dss

SPEC_ALENTEJO = (38.015, -7.865)
SPEC_LISBON = (38.722, -9.139)
# Densest mixed-category 150 m cluster within 25 km of SPEC_ALENTEJO (scan of parcel representative points).
ALENTEJO_MIXED = (37.97883, -7.73698)
MAX_PARCEL_ROWS = 12
PAD = "         | "
EXIT_OK, EXIT_DATASET, EXIT_SITE, EXIT_SOLAR = 0, 1, 2, 3


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    """Parse CLI flags; `--lat`/`--lon` must come as a pair. Parser errors exit with code 2."""
    parser =argparse.ArgumentParser(description="Agri-Energy DSS: clustering, NASA climate, pump/energy sizing.")
    parser.add_argument("--data", type=Path, default=dss.DEFAULT_DATA_PATH, help="EuroCrops PT.zip path")
    parser.add_argument("--download", action="store_true", help="download PT.zip from Zenodo if missing")
    parser.add_argument("--lat", type=float, help="site latitude, decimal degrees (with --lon)")
    parser.add_argument("--lon", type=float, help="site longitude, decimal degrees (with --lat)")
    parser.add_argument(
        "--area-basis",
        choices=sorted(dss.AREA_COLUMNS),
        default="zone",
        help="area driving demand: clipped to the command zone (default) or whole parcels",
    )
    parser.add_argument("--nasa-timeout", type=float, default=10.0, help="seconds per NASA POWER attempt")
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    args = parser.parse_args(argv)
    if (args.lat is None) != (args.lon is None):
        parser.error("--lat and --lon must be given together")
    return args


def _indent(text: str) -> str:
    """Prefix every line with the report gutter (`PAD`) so tables nest under their task line."""
    return "\n".join(PAD + line for line in text.splitlines())


def _table(frame: pd.DataFrame) -> str:
    """Render a DataFrame as an indented text table: no index, floats to 2 dp with thousands separators."""
    return _indent(frame.to_string(index=False, float_format=lambda v: f"{v:,.2f}"))


def _parcel_table(cluster: dss.Cluster) -> str:
    """Task 1 view: one row per member parcel (id, HCAT code/class, category, whole vs in-zone ha).

    Capped at MAX_PARCEL_ROWS; the remainder is summarised as a count.
    """
    cols = {
        dss.ID_COL: "OSA_ID",
        dss.HCAT_COL: "HCAT",
        dss.LABEL_COL: "HCAT class",
        "crop_category": "cat",
        "area_ha": "parcel_ha",
        "area_in_zone_ha": "in_zone_ha",
    }
    present = {k: v for k, v in cols.items() if k in cluster.parcels.columns}
    rows = cluster.parcels[list(present)].rename(columns=present)
    text = _table(rows.head(MAX_PARCEL_ROWS))
    hidden = len(rows) - MAX_PARCEL_ROWS
    return text + (f"\n{PAD}... {hidden} more parcel(s)" if hidden > 0 else "")


def _demand_table(cluster: dss.Cluster, area_basis: dss.AreaBasis) -> str:
    """Task 2.1 view: per-category area and Q_day [m3/day] on the chosen area basis (zone or parcel)."""
    table =dss.water_demand_table(cluster, area_basis).rename(
        columns={
            "crop_category": "cat",
            "crop_category_name": "category",
            "water_demand_m3_ha_day": "WD m3/ha/day",
            "n_parcels": "n",
            "area_in_zone_ha": "in_zone_ha",
            "area_ha": "parcel_ha",
            "q_day_m3": f"Q m3/day ({area_basis})",
        }
    )
    return _table(table)


def _monthly_table(profile: pd.DataFrame) -> str:
    """Task 3 seasonal view: PSH and E_excess per month as a 2-row, 12-column table."""
    rows =profile[["psh_kwh_m2_day", "e_excess_kwh_day"]].T
    rows.index = ["PSH kWh/m2/day", "E_excess kWh/day"]
    return _indent(rows.to_string(float_format=lambda v: f"{v:.2f}"))


def _report(a: dss.SiteAssessment) -> None:
    """Print one site's full result in task order.

    Task 1: cluster and parcels. Task 2: Q_day, NASA PSH, hydraulic power.
    Task 3: tier, E_PV / E_pump / E_excess (with deficit and break-even PSH when surplus is zero), monthly surplus.
    """
    c, s, e, p, d =a.cluster, a.solar, a.energy, a.energy.pump, a.params
    print(
        f"  Task 1 | {c.n_parcels} parcel(s) | farmland in zone {c.area_in_zone_ha:.2f} of {c.zone_ha:.2f} ha"
        f" | whole parcels {c.parcel_area_ha:.2f} ha"
    )
    print(_parcel_table(c))
    print(PAD.rstrip())
    print(_demand_table(c, d.area_basis))

    print(f"  Task 2 | Q_day = {a.q_day_m3:,.1f} m3/day")
    print(
        f"{PAD}NASA POWER at centroid ({s.lat:.2f}, {s.lon:.2f}): PSH_avg {s.psh_avg:.2f} kWh/m2/day"
        f" | design month {s.design_month} {s.design_psh:.2f} kWh/m2/day"
    )
    print(f"{PAD}source: {s.source}, normalised to kWh/m2/day")
    print(
        f"{PAD}P_hyd = rho.g.Q.H / (3.6e6.t.eta) = {a.hydraulic_power_kw:.2f} kW"
        f"  (H {d.head_m:g} m, eta {d.pump_efficiency:g}, t_pump {d.pumping_hours:g} h/day)"
    )

    fleet = f"Tier {p.tier} x {p.n_units}" + ("  [multiple pumps required]" if p.multiple_pumps_required else "")
    print(
        f"  Task 3 | {fleet} for P_hyd {p.required_power_kw:.2f} kW | motor {p.total_motor_kw:g} kW"
        f" | PV {p.total_pv_kwp:g} kWp | CAPEX ${p.total_capex_usd:,.0f} | motor load {p.load_factor:.0%}"
    )
    lines = (
        ("E_PV", f"{p.total_pv_kwp:g} kWp x {e.psh_avg_kwh_m2_day:.2f} h/day x {e.pv_derate:g}", e.e_pv_kwh_day),
        ("E_pump", f"{p.total_motor_kw:g} kW x {e.pumping_hours:g} h/day", e.e_pump_kwh_day),
        ("E_excess", "max(0, E_PV - E_pump)", e.e_excess_kwh_day),
    )
    for name, expr, value in lines:
        print(f"{PAD}{name:<8} = {expr:<30} = {value:7.2f} kWh/day")
    if e.e_excess_kwh_day == 0:
        shortfall = f"deficit {e.e_deficit_kwh_day:.2f} kWh/day; needs PSH > {e.breakeven_psh:.2f}"
        print(f"{PAD}no surplus at PSH_avg: {shortfall}")
    profile = a.monthly_profile()
    surplus = [month for month, value in profile["e_excess_kwh_day"].items() if value > 0]
    print(f"{PAD}by month (same fleet), surplus in: {', '.join(surplus) or 'none'}")
    print(_monthly_table(profile))


def _site(
    parcels: gpd.GeoDataFrame,
    label: str,
    lat: float,
    lon: float,
    params: dss.DesignParameters,
    client: dss.SolarClient,
) -> int:
    """Run `evaluate_site` for one labelled point and print the outcome; returns an exit code.

    EmptyClusterError -> message plus nearest-parcel hint (EXIT_SITE).
    SolarDataError    -> NASA failure message (EXIT_SOLAR).
    Success           -> full report and timing (EXIT_OK).
    """
    print(f"\n{label}  ({lat:.5f}, {lon:.5f})")
    start = time.perf_counter()
    try:
        assessment = dss.evaluate_site(parcels, lat, lon, params, solar_client=client)
    except dss.EmptyClusterError as exc:
        print(f"  EmptyClusterError: {exc}")
        if dss.in_domain(lat, lon):
            hint = dss.nearest_parcel(parcels, lat, lon)
            where = f"({hint.lat:.5f}, {hint.lon:.5f})"
            print(f"  nearest parcel {hint.parcel_id} is {hint.distance_m:,.0f} m away at {where}")
        return EXIT_SITE
    except dss.SolarDataError as exc:
        print(f"  SolarDataError: {exc}")
        return EXIT_SOLAR
    _report(assessment)
    print(f"  evaluated in {(time.perf_counter() - start) * 1e3:.0f} ms, NASA POWER round trip included unless cached")
    return EXIT_OK


def main(argv: list[str] | None = None, solar_client: dss.SolarClient | None = None) -> int:
    """CLI entry point: load the parcel layer, then evaluate one custom site or the verification suite.

    `solar_client` overrides NASA POWER (tests, offline stubs). The suite treats empty clusters as expected
    ([1] spec Alentejo point, [3] Lisbon), so only a NASA failure makes it exit non-zero.
    Returns 0 ok | 1 dataset error | 2 invalid input or empty custom site | 3 NASA POWER unavailable.
    """
    args = _parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING, format="%(levelname)s %(name)s: %(message)s"
    )
    try:
        client = dss.NasaPowerClient(timeout_s=args.nasa_timeout) if solar_client is None else solar_client
        path = dss.ensure_dataset(args.data) if args.download else args.data
        start = time.perf_counter()
        parcels = dss.load_parcels(path)
    except dss.DatasetError as exc:
        print(f"Dataset error: {exc}", file=sys.stderr)
        return EXIT_DATASET
    except (TypeError, ValueError) as exc:
        print(f"Invalid input: {exc}", file=sys.stderr)
        return EXIT_SITE
    load_s = time.perf_counter() - start
    params = dss.DesignParameters(area_basis=args.area_basis)
    print(f"EuroCrops PT | {len(parcels):,} parcels | loaded in {load_s:.1f} s | CRS {dss.PT_TM06}")
    print(
        f"Design | radius {params.radius_m:g} m | head {params.head_m:g} m | eta {params.pump_efficiency:g}"
        f" | t_pump {params.pumping_hours:g} h/day | PV derate {params.pv_derate:g} | area basis {params.area_basis}"
    )
    if isinstance(client, dss.NasaPowerClient):
        print(
            f"Solar | NASA POWER climatology, community={client.community}, at each cluster centroid"
            f" | timeout {client.timeout_s:g} s x {client.attempts} attempts"
        )
    else:
        print(f"Solar | {type(client).__name__} (injected)")

    codes: list[int] = []
    try:
        if args.lat is not None:
            return _site(parcels, "Custom site", args.lat, args.lon, params, client)
        codes.append(_site(parcels, "[1] Spec Alentejo point", *SPEC_ALENTEJO, params, client))
        if codes[-1] == EXIT_SITE:
            hint = dss.nearest_parcel(parcels, *SPEC_ALENTEJO)
            label = "[1b] Alentejo, re-sited to nearest parcel"
            codes.append(_site(parcels, label, hint.lat, hint.lon, params, client))
        codes.append(_site(parcels, "[2] Alentejo olive/fallow cluster", *ALENTEJO_MIXED, params, client))
        codes.append(_site(parcels, "[3] Central Lisbon (urban)", *SPEC_LISBON, params, client))
    except (TypeError, ValueError) as exc:
        print(f"Invalid input: {exc}", file=sys.stderr)
        return EXIT_SITE
    return EXIT_SOLAR if EXIT_SOLAR in codes else EXIT_OK  # empty clusters in the suite are expected


if __name__ == "__main__":
    sys.exit(main())
