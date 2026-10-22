"""Export parcels and the DSS result around a site as one JSON file for the 3D viewer.

    python ignore/visualiser/export_data.py                       # the verification sites plus one dense area
    python ignore/visualiser/export_data.py --lat 38.08 --lon -8.12 --name my-site

Output: ignore/visualiser/public/data/<name>.json plus an index.json listing every site.
Run it with the project venv (needs geopandas); NASA POWER is optional, the viewer works without it.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import geopandas as gpd
import shapely
from shapely.geometry import MultiPolygon, Polygon

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import dss_pipeline as dss  # noqa: E402

OUT_DIR = Path(__file__).resolve().parent / "public" / "data"
WINDOW_M = 1500  # parcels within this distance of the site are exported, so the zone sits in context
SIMPLIFY_M = 0.5  # drops vertices that move an outline by less than this; keeps files small
DECIMALS = 1e-6  # ~0.1 m of longitude/latitude precision


def _polygons_only(geom):
    """make_valid can return a mix of shapes; keep only the polygon parts."""
    parts = [g for g in shapely.get_parts(shapely.make_valid(geom)) if isinstance(g, Polygon) and not g.is_empty]
    if not parts:
        return None
    return parts[0] if len(parts) == 1 else MultiPolygon(parts)


def _window(parcels: gpd.GeoDataFrame, lat: float, lon: float) -> gpd.GeoDataFrame:
    """Parcels near the site, classified, with whole-parcel area and the share inside the 150 m zone."""
    zone = dss.command_zone(lat, lon, dss.COMMAND_ZONE_RADIUS_M)
    near = parcels.iloc[sorted(parcels.sindex.query(dss.command_zone(lat, lon, WINDOW_M), predicate="intersects"))]

    shapes = near.geometry.map(_polygons_only)
    near = dss.classify_parcels(near.set_geometry(shapes)[shapes.notna()])

    return near.assign(
        area_ha=near.area / dss.M2_PER_HA,
        area_in_zone_ha=near.geometry.intersection(zone).area / dss.M2_PER_HA,
    )


def _features(window: gpd.GeoDataFrame) -> dict:
    """GeoJSON in degrees (what web maps use), with outlines simplified and rounded."""
    shapes = window.geometry.simplify(SIMPLIFY_M).to_crs(dss.WGS84).map(lambda g: shapely.set_precision(g, DECIMALS))
    out = []

    for (_, row), shape in zip(window.iterrows(), shapes, strict=True):
        if shape.is_empty:
            continue

        out.append(
            {
                "type": "Feature",
                "geometry": shapely.geometry.mapping(shape),
                "properties": {
                    "id": int(row[dss.ID_COL]) if row[dss.ID_COL] == row[dss.ID_COL] else None,
                    "hcat": row[dss.HCAT_COL] if isinstance(row[dss.HCAT_COL], str) else None,
                    "label": row[dss.LABEL_COL] if isinstance(row[dss.LABEL_COL], str) else None,
                    "cat": row["crop_category"],
                    "wd": float(row["water_demand_m3_ha_day"]),
                    "ha": round(float(row["area_ha"]), 3),
                    "inZoneHa": round(float(row["area_in_zone_ha"]), 3),
                },
            }
        )

    return {"type": "FeatureCollection", "features": out}


def _assessment(parcels: gpd.GeoDataFrame, lat: float, lon: float) -> dict | None:
    """Tasks 1 to 3 for the site, or None if there is no farmland or NASA cannot be reached."""
    try:
        a = dss.evaluate_site(parcels, lat, lon)
    except (dss.EmptyClusterError, dss.SolarDataError) as exc:
        print(f"  no assessment: {exc}")
        return None

    pump, energy = a.energy.pump, a.energy
    profile = a.monthly_profile()

    return {
        "qDayM3": a.q_day_m3,
        "hydraulicKw": a.hydraulic_power_kw,
        "solar": {"lat": a.solar.lat, "lon": a.solar.lon, "pshAvg": a.solar.psh_avg},
        "pump": {
            "tier": pump.tier,
            "units": pump.n_units,
            "motorKw": pump.total_motor_kw,
            "pvKwp": pump.total_pv_kwp,
            "capexUsd": pump.total_capex_usd,
        },
        "energy": {"pv": energy.e_pv_kwh_day, "pump": energy.e_pump_kwh_day, "excess": energy.e_excess_kwh_day},
        "monthly": {
            "psh": profile["psh_kwh_m2_day"].tolist(),
            "excess": profile["e_excess_kwh_day"].tolist(),
            "months": list(profile.index),
        },
    }


def export_site(parcels: gpd.GeoDataFrame, name: str, label: str, lat: float, lon: float) -> dict:
    """Write public/data/<name>.json and return its index entry."""
    print(f"{name}: ({lat:.5f}, {lon:.5f})")
    window = _window(parcels, lat, lon)

    zone = gpd.GeoSeries([dss.command_zone(lat, lon)], crs=dss.PT_TM06).to_crs(dss.WGS84).iloc[0]
    doc = {
        "name": name,
        "label": label,
        "lat": lat,
        "lon": lon,
        "radiusM": dss.COMMAND_ZONE_RADIUS_M,
        "zone": [list(p) for p in zone.exterior.coords],
        "categories": {k: {"name": a.name, "wd": a.water_demand_m3_ha_day} for k, a in dss.CROP_ARCHETYPES.items()},
        "parcels": _features(window),
        "assessment": _assessment(parcels, lat, lon),
    }

    path = OUT_DIR / f"{name}.json"
    path.write_text(json.dumps(doc, separators=(",", ":")), encoding="utf-8")
    print(f"  {len(doc['parcels']['features'])} parcels, {path.stat().st_size / 1024:.0f} KB")

    return {"name": name, "label": label, "lat": lat, "lon": lon, "file": f"data/{name}.json"}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--lat", type=float)
    parser.add_argument("--lon", type=float)
    parser.add_argument("--name", default="custom")
    parser.add_argument("--data", type=Path, default=dss.DEFAULT_DATA_PATH)
    args = parser.parse_args()

    if (args.lat is None) != (args.lon is None):
        parser.error("--lat and --lon must be given together")

    parcels = dss.load_parcels(args.data)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    index_path = OUT_DIR / "index.json"

    if args.lat is not None:
        sites = [(args.name, f"Custom site {args.name}", args.lat, args.lon)]
        index = {s["name"]: s for s in json.loads(index_path.read_text())} if index_path.exists() else {}
    else:
        spec = dss.nearest_parcel(parcels, 38.015, -7.865)  # the spec point has no parcel; use the nearest
        sites = [
            ("alentejo-resited", "Spec Alentejo point, re-sited to nearest parcel", spec.lat, spec.lon),
            ("alentejo-mixed", "Alentejo mixed-crop cluster", 37.97883, -7.73698),
            ("tras-os-montes", "Dense mixed farmland, north-east Portugal", 41.85417, -6.76829),
            ("lisbon", "Central Lisbon (urban, no farmland)", 38.722, -9.139),
        ]
        index = {}

    for name, label, lat, lon in sites:
        index[name] = export_site(parcels, name, label, lat, lon)

    index_path.write_text(json.dumps(list(index.values()), indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
