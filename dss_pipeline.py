"""Agri-Energy DSS prototype: Tasks 1 to 3 for one farming cluster.

Flow (evaluate_site runs all of it for one point)
    Task 1  extract_cluster        parcels inside a 150 m circle around the user's point
    Task 2  daily_water_demand_m3  water the cluster needs per day
            fetch_solar_resource   sunshine data from NASA POWER at the cluster centre
            hydraulic_power_kw     pump power needed to lift that water
    Task 3  model_energy_balance   pick a pump, then compare solar energy made vs energy the pump uses

Coordinates
    The parcel file is in degrees (EPSG:4326). Areas and distances only make sense in metres, so the file is
    converted once to EPSG:3763 (Portugal TM06, metres). The user's point is converted the same way BEFORE the
    150 m buffer is drawn; a buffer of 150 in degrees would cover most of the planet.

Spatial index
    A spatial index (GeoPandas `sindex`) finds the parcels near a point without testing all 100,000 polygons.

Units
    area ha | water demand m3/ha/day | Q_day m3/day | pump and PV power kW (kWp = PV rating) | energy kWh/day
    PSH (peak sun hours) hours/day, numerically the same as kWh per m2 per day.

Assumptions
    A1  Every crop code in PT.zip starts with "33", so the spec's "first two digits" rule would put every
        parcel in the fallback. Codes are matched by their longest known prefix instead (HCAT_PREFIX_RULES).
    A2  Water demand uses the part of each parcel inside the circle, not the whole parcel. A 20 ha field that
        only clips the circle would otherwise size the pump for 20 ha instead of ~7 ha.
    A3  Non-crop land (fallow, shrub, forest, unknown) gets the fallback demand, as Table 1 says.
    A4  Demand beyond Tier 4 -> repeat Tier 4 as many times as needed.
    A5  E_pump = rated motor kW x pumping hours (worst case: motor runs at full rating all window).
    A6  The spec's NASA URL (community=AG) returns MJ/m2/day, not kWh. We convert (divide by 3.6).
    A7  Design month = month with the lowest sunshine.
    A8  NASA is asked about the cluster centre rounded to 0.01 degree (~1 km, finer than NASA's 1 degree grid);
        the rounded point is also the cache key.
    A9  The spec's Alentejo point (38.015, -7.865) has no parcel within 1.3 km in this file (it holds only
        100,000 parcels), so it raises EmptyClusterError. The runner then re-sites to the nearest parcel.

Monte Carlo (regional policy)
    evaluate_site is deterministic and takes its inputs explicitly (point, DesignParameters, solar source), so
    a regional study is an outer loop with a seeded random generator: draw sites from parcel points, draw
    parameters with dataclasses.replace (e.g. water depth -> head_m), draw sunshine years through a custom
    `get_solar`, and count EmptyClusterError as "not farmland". Policy levers (CAPEX subsidy, surplus-energy
    tariff) act on the outputs (total_capex_usd, e_excess_kwh_day). Runs are independent, so they parallelise.
"""

from __future__ import annotations

import http.client
import json
import math
import shutil
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import geopandas as gpd
import pandas as pd
import pyogrio
from shapely.geometry import Point, Polygon

# --- Constants ------------------------------------------------------------------------------------

WGS84 = "EPSG:4326"  # latitude / longitude in degrees (GPS, NASA)
PT_TM06 = "EPSG:3763"  # Portugal TM06: x / y in metres
COMMAND_ZONE_RADIUS_M = 150.0  # tube-well reach; area = pi * 150^2 = 7.07 ha
M2_PER_HA = 10_000

EUROCROPS_PT_URL = "https://zenodo.org/records/10118572/files/PT.zip"
DEFAULT_DATA_PATH = Path(__file__).resolve().parent / "data" / "PT.zip"
ID_COL = "OSA_ID"  # unique id of each parcel record
HCAT_COL = "EC_hcat_c"  # 10-digit crop code
LABEL_COL = "EC_hcat_n"  # crop name, for display

RHO_WATER_KG_M3 = 1000.0
G_M_S2 = 9.81
J_PER_KWH = 3.6e6  # 1 kWh = 3,600 s x 1,000 W

NASA_POWER_URL = "https://power.larc.nasa.gov/api/temporal/climatology/point"
NASA_PARAMETER = "ALLSKY_SFC_SW_DWN"  # sunlight energy reaching the ground (all-sky, shortwave)
NASA_COMMUNITY = "AG"  # the community named in the spec URL
NASA_COORD_DECIMALS = 2
MONTHS = ("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC")
NASA_UNITS_TO_KWH = {"mj/m^2/day": 1 / 3.6, "kw-hr/m^2/day": 1.0}  # factor to kWh/m2/day, by unit label


# --- Errors ---------------------------------------------------------------------------------------

class EmptyClusterError(Exception):
    """No parcel inside the command zone: urban area, water body or outside Portugal."""


class DatasetError(Exception):
    """Parcel file missing, unreadable or lacking a required column."""


class SolarDataError(Exception):
    """NASA POWER unreachable, or its reply unusable."""


# --- Input checks ---------------------------------------------------------------------------------

def _check_range(name: str, value: float, low: float, high: float, *, low_open: bool = False) -> float:
    """Return value as a float if low <= value <= high (low excluded when low_open). NaN and inf fail."""
    ok = math.isfinite(value) and (low < value if low_open else low <= value) and value <= high

    if not ok:
        left = "(" if low_open else "["
        right = "]" if math.isfinite(high) else ")"
        raise ValueError(f"{name} must be a finite number in {left}{low:g}, {high:g}{right}, got {value!r}")

    return float(value)


def _positive(name: str, value: float) -> float:
    return _check_range(name, value, 0, math.inf, low_open=True)


def _non_negative(name: str, value: float) -> float:
    return _check_range(name, value, 0, math.inf)


def _fraction(name: str, value: float) -> float:
    return _check_range(name, value, 0, 1, low_open=True)  # efficiencies and derate factors


def _hours(name: str, value: float) -> float:
    return _check_range(name, value, 0, 24, low_open=True)  # hours per day


def _lat_lon(lat: float, lon: float) -> tuple[float, float]:
    return _check_range("lat", lat, -90, 90), _check_range("lon", lon, -180, 180)


# --- Design parameters ----------------------------------------------------------------------------

@dataclass(frozen=True)
class DesignParameters:
    """The spec's design values. Frozen: share one instance freely, use dataclasses.replace to vary one."""

    radius_m: float = COMMAND_ZONE_RADIUS_M
    head_m: float = 40.0  # total dynamic head: how high the pump lifts the water, incl. pipe friction
    pump_efficiency: float = 0.55  # share of electric power that ends up as useful water lifting
    pumping_hours: float = 6.0  # pump runs this many hours per day
    pv_derate: float = 0.75  # real-world losses (heat, dust, wiring) on the panel rating


DEFAULT_PARAMS = DesignParameters()


# --- Reference Table 1: crop categories -----------------------------------------------------------

FALLBACK = "other"


@dataclass(frozen=True)
class CropArchetype:
    name: str
    water_demand_m3_ha_day: float


CROP_ARCHETYPES = {
    "10": CropArchetype("Cereals & Grains", 35.0),
    "20": CropArchetype("Orchards & Fruits", 45.0),
    "30": CropArchetype("Pastures & Meadows", 15.0),
    "50": CropArchetype("Vegetables / High-Value", 55.0),
    FALLBACK: CropArchetype("Fallback Field Crop", 30.0),
}

# Crop-code prefix -> Table 1 category (A1). The longest matching prefix wins, so "330308" (shrubland)
# beats "3303" (permanent crops). Codes matching nothing fall back to "other".
HCAT_PREFIX_RULES = {
    "10": "10",  # the spec's literal two-digit rule, kept for files that follow it
    "20": "20",
    "30": "30",
    "50": "50",
    "330101": "10",  # cereals: wheat, barley, oats, rye, maize, rice
    "3302": "30",  # pasture, meadow, grassland
    "330109": "30",  # temporary grass, ryegrass, alfalfa, clover
    "3303": "20",  # permanent crops: orchards, nuts, citrus, olives, vineyards
    "330308": FALLBACK,  # shrubland and hedges, filed under permanent crops but not irrigated crops
    "330103": "50",  # potatoes
    "330104": "50",  # sweet potatoes
    "330107": "50",  # fresh vegetables
    "330108": "50",  # flowers and ornamentals
    "330113": "50",  # strawberries
    "330114": "50",  # melon, pumpkin, courgette
    "330121": "50",  # cabbage family
    "330122": "50",  # garlic, onion
    "330126": "50",  # aubergine
    "330128": "50",  # tomato
    "330129": "50",  # carrot, beet, turnip
    "330131": "50",  # lettuce, spinach
}
_PREFIXES_LONGEST_FIRST = sorted(HCAT_PREFIX_RULES, key=len, reverse=True)


def hcat_category(code: str) -> str:
    """Table 1 category key for one crop code. Missing or unknown codes give FALLBACK."""
    text = str(code).strip()

    for prefix in _PREFIXES_LONGEST_FIRST:
        if text.startswith(prefix):
            return HCAT_PREFIX_RULES[prefix]

    return FALLBACK


def classify_parcels(parcels: pd.DataFrame) -> pd.DataFrame:
    """Copy of `parcels` with the crop category, its name and its water demand [m3/ha/day] added."""
    keys = parcels[HCAT_COL].map(hcat_category)

    return parcels.assign(
        crop_category=keys,
        crop_category_name=keys.map({k: a.name for k, a in CROP_ARCHETYPES.items()}),
        water_demand_m3_ha_day=keys.map({k: a.water_demand_m3_ha_day for k, a in CROP_ARCHETYPES.items()}),
    )


# --- Task 1: load the parcel file -----------------------------------------------------------------

def ensure_dataset(path: str | Path = DEFAULT_DATA_PATH, url: str = EUROCROPS_PT_URL, timeout_s: float = 60.0) -> Path:
    """Return the path to PT.zip, downloading it first (~37 MB) if it is not there yet."""
    path = Path(path)

    if path.is_file():
        return path

    path.parent.mkdir(parents=True, exist_ok=True)
    part = path.with_name(path.name + ".part")  # download here first, so a failed run leaves no half file

    try:
        with urllib.request.urlopen(url, timeout=timeout_s) as response, part.open("wb") as out:
            shutil.copyfileobj(response, out)
        part.replace(path)
    except OSError as exc:
        part.unlink(missing_ok=True)
        raise DatasetError(f"Could not download {url}: {exc}") from exc

    return path


def load_parcels(path: str | Path = DEFAULT_DATA_PATH) -> gpd.GeoDataFrame:
    """Read the parcel file into a GeoDataFrame in EPSG:3763 (metres).

    Raises:
        DatasetError: file missing, unreadable, or without the id / crop-code columns.
    """
    path = Path(path)

    if not path.is_file():
        raise DatasetError(f"Parcel file not found: {path} (download it with --download)")

    # A .zip is read in place through GDAL's zip:// prefix, no unpacking needed.
    source = f"zip://{path.resolve().as_posix()}" if path.suffix.lower() == ".zip" else str(path)

    try:
        # Only three columns: the other text columns in this file have broken accents and are not needed.
        parcels = gpd.read_file(source, columns=[ID_COL, HCAT_COL, LABEL_COL])
    except pyogrio.errors.DataSourceError as exc:
        raise DatasetError(f"Cannot read {path.name}: {exc}") from exc

    missing = [c for c in (ID_COL, HCAT_COL) if c not in parcels.columns]  # GDAL silently skips absent columns
    if missing or parcels.empty:
        raise DatasetError(f"{path.name} has no rows or lacks columns {missing}")

    parcels = parcels.to_crs(PT_TM06)  # degrees -> metres, once, so every later area and distance is in metres
    parcels[ID_COL] = parcels[ID_COL].astype("Int64")  # the .dbf stores whole numbers as floats (506762.0)

    return parcels


# --- Task 1: find the cluster ---------------------------------------------------------------------

def _to_tm06(lat: float, lon: float) -> Point:
    """Degrees -> a Point in EPSG:3763 metres. Shapely points are (x, y) = (lon, lat)."""
    return gpd.GeoSeries([Point(lon, lat)], crs=WGS84).to_crs(PT_TM06).iloc[0]


def command_zone(lat: float, lon: float, radius_m: float = COMMAND_ZONE_RADIUS_M) -> Polygon:
    """Circular command zone around (lat, lon): project to metres FIRST, then buffer."""
    lat, lon = _lat_lon(lat, lon)
    radius_m = _positive("radius_m", radius_m)

    # quad_segs=64 draws the circle with 256 straight edges: area within 0.01 % of a true circle.
    return _to_tm06(lat, lon).buffer(radius_m, quad_segs=64)


def _check_layer(parcels: gpd.GeoDataFrame) -> None:
    """Refuse layers not in metres: buffering or measuring in degrees would give nonsense."""
    if parcels.crs is None or parcels.crs.to_epsg() != 3763:
        raise ValueError(f"parcel layer must be in {PT_TM06}, got {parcels.crs}; load it with load_parcels()")


@dataclass(frozen=True)
class Cluster:
    """Task 1 result. `parcels` holds one row per parcel touching the zone, in EPSG:3763, with extra columns:
    area_ha (whole parcel), area_in_zone_ha (part inside the circle), crop_category, crop_category_name and
    water_demand_m3_ha_day.
    """

    lat: float
    lon: float
    zone: Polygon
    parcels: gpd.GeoDataFrame

    @property
    def n_parcels(self) -> int:
        return len(self.parcels)

    @property
    def zone_ha(self) -> float:
        return self.zone.area / M2_PER_HA

    @property
    def area_in_zone_ha(self) -> float:
        return float(self.parcels["area_in_zone_ha"].sum())

    @property
    def parcel_area_ha(self) -> float:
        return float(self.parcels["area_ha"].sum())

    def centroid_lat_lon(self) -> tuple[float, float]:
        """Middle of the farmland inside the zone as (lat, lon): the point we ask NASA about."""
        pieces = self.parcels.geometry.intersection(self.zone)  # only the parts inside the circle
        middle = pieces.union_all().centroid  # merge the pieces into one shape and take its centre

        point = gpd.GeoSeries([middle], crs=PT_TM06).to_crs(WGS84).iloc[0]  # back to degrees for the API
        return point.y, point.x


def extract_cluster(
    parcels: gpd.GeoDataFrame, lat: float, lon: float, radius_m: float = COMMAND_ZONE_RADIUS_M
) -> Cluster:
    """Task 1: parcels touching the command zone at (lat, lon), with hectare areas and crop categories.

    Raises:
        ValueError / TypeError: bad coordinates, or a layer not in EPSG:3763.
        EmptyClusterError: no parcel inside the zone (urban area, water, outside Portugal).
    """
    _check_layer(parcels)
    zone = command_zone(lat, lon, radius_m)

    # The index narrows 100,000 parcels to the few near the circle, then tests those exactly.
    hits = parcels.sindex.query(zone, predicate="intersects")
    members = parcels.iloc[sorted(hits)]

    # Real files contain self-crossing ("bow-tie") polygons whose area and clipping are unreliable.
    # make_valid repairs them and leaves healthy polygons unchanged.
    shapes = members.geometry.make_valid()
    in_zone_m2 = shapes.intersection(zone).area

    keep = in_zone_m2 > 0  # a parcel that only touches the circle's edge gets no water

    if not keep.any():
        raise EmptyClusterError(
            f"Location is out of agricultural domain or contains no parcels within the {radius_m:g}m radius."
        )

    members = members.set_geometry(shapes)[keep].assign(
        area_ha=shapes[keep].area / M2_PER_HA,
        area_in_zone_ha=in_zone_m2[keep] / M2_PER_HA,
    )

    return Cluster(lat, lon, zone, classify_parcels(members))


@dataclass(frozen=True)
class NearestParcel:
    """Closest parcel to a point; used to suggest a valid location after EmptyClusterError."""

    parcel_id: int
    distance_m: float
    lat: float
    lon: float


def nearest_parcel(parcels: gpd.GeoDataFrame, lat: float, lon: float) -> NearestParcel | None:
    """Closest parcel to (lat, lon). None if the point cannot be projected onto the map at all."""
    _check_layer(parcels)
    lat, lon = _lat_lon(lat, lon)

    (_, rows), distances = parcels.sindex.nearest(_to_tm06(lat, lon), return_distance=True)

    if len(rows) == 0:  # a few far-away points project to infinity and match nothing
        return None

    row = int(rows[0])

    # representative_point is guaranteed to lie inside the parcel; a centroid of a curved shape may not.
    inside = parcels.geometry.iloc[row].representative_point()
    anchor = gpd.GeoSeries([inside], crs=PT_TM06).to_crs(WGS84).iloc[0]

    return NearestParcel(int(parcels[ID_COL].iloc[row]), float(distances[0]), anchor.y, anchor.x)


# --- Task 2: water demand and pump power ----------------------------------------------------------

def water_demand_table(cluster: Cluster) -> pd.DataFrame:
    """Per crop category: parcel count, areas and water demand Q [m3/day]."""
    table = pd.DataFrame(cluster.parcels.drop(columns=cluster.parcels.geometry.name))
    table["q_day_m3"] = table["area_in_zone_ha"] * table["water_demand_m3_ha_day"]  # ha x m3/ha/day = m3/day

    return table.groupby(["crop_category", "crop_category_name", "water_demand_m3_ha_day"], as_index=False).agg(
        n_parcels=("q_day_m3", "size"),
        area_in_zone_ha=("area_in_zone_ha", "sum"),
        area_ha=("area_ha", "sum"),
        q_day_m3=("q_day_m3", "sum"),
    )


def daily_water_demand_m3(cluster: Cluster) -> float:
    """Task 2.1: Q_day = sum(area_i x water_demand_i) [m3/day], using area inside the zone (A2)."""
    parcels = cluster.parcels

    return float((parcels["area_in_zone_ha"] * parcels["water_demand_m3_ha_day"]).sum())


def hydraulic_power_kw(
    q_day_m3: float,
    *,
    head_m: float = DEFAULT_PARAMS.head_m,
    efficiency: float = DEFAULT_PARAMS.pump_efficiency,
    pumping_hours: float = DEFAULT_PARAMS.pumping_hours,
) -> float:
    """Task 2.3: pump power [kW] = rho x g x Q x H / (3.6e6 x t x eta).

    Unit walk: kg/m3 x m/s2 x m3/day x m = joules per day. / 3.6e6 -> kWh/day. / hours per day -> kW.
    / efficiency -> the electric kW the motor draws, which is what the pump catalog's motor rating means.
    """
    q_day_m3 = _non_negative("q_day_m3", q_day_m3)
    head_m = _positive("head_m", head_m)
    efficiency = _fraction("efficiency", efficiency)
    pumping_hours = _hours("pumping_hours", pumping_hours)

    return RHO_WATER_KG_M3 * G_M_S2 * q_day_m3 * head_m / (J_PER_KWH * pumping_hours * efficiency)


# --- Task 2: NASA POWER sunshine data -------------------------------------------------------------

@dataclass(frozen=True)
class SolarResource:
    """Sunshine at one point, in kWh/m2/day (= peak sun hours per day)."""

    lat: float
    lon: float
    monthly_psh: dict[str, float]  # "JAN" .. "DEC" -> PSH
    psh_avg: float  # NASA's annual average

    @property
    def design_month(self) -> str:
        """Dullest month: the worst case the panels must cope with (A7)."""
        return min(self.monthly_psh, key=self.monthly_psh.get)

    @property
    def design_psh(self) -> float:
        return min(self.monthly_psh.values())


def parse_power_climatology(payload: Any, lat: float, lon: float) -> SolarResource:
    """Turn a NASA POWER reply (already JSON-decoded) into a SolarResource in kWh/m2/day.

    Raises:
        SolarDataError: expected fields missing, unknown units, or missing (-999) values.
    """
    try:
        values = payload["properties"]["parameter"][NASA_PARAMETER]
        units = payload["parameters"][NASA_PARAMETER]["units"]
    except (KeyError, TypeError) as exc:
        raise SolarDataError(f"NASA POWER reply lacks {NASA_PARAMETER} values or units") from exc

    # The reply states its own units; the spec URL gives MJ/m2/day, other communities give kWh (A6).
    to_kwh = NASA_UNITS_TO_KWH.get(str(units).strip().lower())
    if to_kwh is None:
        raise SolarDataError(f"NASA POWER reported {NASA_PARAMETER} in unexpected units {units!r}")

    try:
        monthly = {month: values[month] * to_kwh for month in MONTHS}
        annual = values["ANN"] * to_kwh
    except (KeyError, TypeError) as exc:
        raise SolarDataError("NASA POWER reply lacks monthly or annual values") from exc

    # NASA marks missing data as -999, so a negative value means a gap. Better to stop than to guess.
    if not all(math.isfinite(v) and v >= 0 for v in (*monthly.values(), annual)):
        raise SolarDataError("NASA POWER reply has missing values (gaps are filled with -999)")

    return SolarResource(lat, lon, monthly, annual)


def _http_get(url: str, timeout_s: float) -> bytes:
    with urllib.request.urlopen(url, timeout=timeout_s) as response:
        return response.read()


_solar_cache: dict[tuple[float, float], SolarResource] = {}  # in-memory, so re-runs skip the network


def fetch_solar_resource(
    lat: float,
    lon: float,
    timeout_s: float = 10.0,
    http_get: Callable[[str, float], bytes] = _http_get,
) -> SolarResource:
    """Task 2.2: NASA POWER climatology at (lat, lon), cached per rounded point (A8).

    `http_get` can be swapped out in tests.

    Raises:
        SolarDataError: no connection, timeout, HTTP error, invalid JSON, or unusable data.
    """
    lat, lon = _lat_lon(lat, lon)
    timeout_s = _positive("timeout_s", timeout_s)

    key = (round(lat, NASA_COORD_DECIMALS), round(lon, NASA_COORD_DECIMALS))
    if key in _solar_cache:
        return _solar_cache[key]

    query = {
        "parameters": NASA_PARAMETER,
        "community": NASA_COMMUNITY,
        "longitude": key[1],
        "latitude": key[0],
        "format": "JSON",
    }
    url = f"{NASA_POWER_URL}?{urllib.parse.urlencode(query)}"

    try:
        payload = json.loads(http_get(url, timeout_s))
    except (OSError, http.client.HTTPException) as exc:  # timeouts, no network, HTTP errors, cut-off reply
        raise SolarDataError(f"NASA POWER request failed: {exc!r}") from exc
    except ValueError as exc:  # reply was not JSON
        raise SolarDataError("NASA POWER reply is not valid JSON") from exc

    resource = parse_power_climatology(payload, *key)
    _solar_cache[key] = resource  # stored only after success, so failures are retried next time

    return resource


# --- Task 3: pump matching ------------------------------------------------------------------------

# Reference Table 2 (IDCOL pump catalog), smallest tier first.
PUMP_CATALOG = pd.DataFrame(
    [
        (1, 3.7, 5.0, 7_500, 250),
        (2, 5.5, 7.5, 10_500, 400),
        (3, 7.5, 10.0, 14_000, 580),
        (4, 11.0, 15.0, 19_500, 850),
    ],
    columns=["tier", "motor_kw", "pv_kwp", "capex_usd", "max_delivery_m3_day"],
)


@dataclass(frozen=True)
class PumpSelection:
    """Chosen pump. Ratings are per unit; the total_* values count all `n_units`."""

    tier: int
    n_units: int
    motor_kw: float
    pv_kwp: float
    capex_usd: float
    required_power_kw: float  # Task 2 result that drove the choice
    q_day_m3: float  # Task 2 result that drove the choice

    @property
    def multiple_pumps_required(self) -> bool:
        return self.n_units > 1

    @property
    def total_motor_kw(self) -> float:
        return self.n_units * self.motor_kw

    @property
    def total_pv_kwp(self) -> float:
        return self.n_units * self.pv_kwp

    @property
    def total_capex_usd(self) -> float:
        return self.n_units * self.capex_usd


def select_pump(power_kw: float, q_day_m3: float, catalog: pd.DataFrame = PUMP_CATALOG) -> PumpSelection:
    """Task 3.1: smallest tier whose motor power AND daily delivery both cover the need.

    Beyond Tier 4 (A4) the largest tier is repeated, as many units as the bigger of the two needs.
    """
    power_kw = _positive("power_kw", power_kw)
    q_day_m3 = _positive("q_day_m3", q_day_m3)
    catalog = catalog.sort_values("motor_kw")

    fits = catalog[(catalog["motor_kw"] >= power_kw) & (catalog["max_delivery_m3_day"] >= q_day_m3)]

    if not fits.empty:
        tier, n_units = fits.iloc[0], 1
    else:
        tier = catalog.iloc[-1]
        n_units = math.ceil(max(power_kw / tier["motor_kw"], q_day_m3 / tier["max_delivery_m3_day"]))

    return PumpSelection(
        tier=int(tier["tier"]),
        n_units=n_units,
        motor_kw=float(tier["motor_kw"]),
        pv_kwp=float(tier["pv_kwp"]),
        capex_usd=float(tier["capex_usd"]),
        required_power_kw=power_kw,
        q_day_m3=q_day_m3,
    )


# --- Task 3: energy balance -----------------------------------------------------------------------

def pv_energy_kwh_day(
    pv_kwp: float, psh_kwh_m2_day: float, derate: float = DEFAULT_PARAMS.pv_derate
) -> float:
    """Task 3.2: E_PV = panel kWp x peak sun hours x derate [kWh/day]."""
    pv_kwp = _non_negative("pv_kwp", pv_kwp)
    psh = _non_negative("psh_kwh_m2_day", psh_kwh_m2_day)
    derate = _fraction("derate", derate)

    return pv_kwp * psh * derate


def pump_energy_kwh_day(motor_kw: float, pumping_hours: float = DEFAULT_PARAMS.pumping_hours) -> float:
    """Task 3.3: E_pump = motor kW x pumping hours [kWh/day], at full rating (A5)."""
    return _non_negative("motor_kw", motor_kw) * _hours("pumping_hours", pumping_hours)


def surplus_energy_kwh_day(e_pv_kwh_day: float, e_pump_kwh_day: float) -> float:
    """Task 3.4: E_excess = max(0, E_PV - E_pump) [kWh/day]."""
    e_pv = _non_negative("e_pv_kwh_day", e_pv_kwh_day)
    e_pump = _non_negative("e_pump_kwh_day", e_pump_kwh_day)

    return max(0.0, e_pv - e_pump)


@dataclass(frozen=True)
class EnergyBalance:
    """Task 3 result for an average day."""

    pump: PumpSelection
    psh_avg_kwh_m2_day: float
    pv_derate: float
    pumping_hours: float
    e_pv_kwh_day: float
    e_pump_kwh_day: float
    e_excess_kwh_day: float

    @property
    def e_deficit_kwh_day(self) -> float:
        """Shortfall when panels make less than the pump uses; E_excess hides it by clamping at zero."""
        return max(0.0, self.e_pump_kwh_day - self.e_pv_kwh_day)

    @property
    def breakeven_psh(self) -> float:
        """Peak sun hours at which panels exactly cover the pump. A surplus needs more sun than this."""
        return self.e_pump_kwh_day / (self.pump.total_pv_kwp * self.pv_derate)


def model_energy_balance(
    power_kw: float,
    q_day_m3: float,
    psh_avg_kwh_m2_day: float,
    *,
    pumping_hours: float = DEFAULT_PARAMS.pumping_hours,
    pv_derate: float = DEFAULT_PARAMS.pv_derate,
    catalog: pd.DataFrame = PUMP_CATALOG,
) -> EnergyBalance:
    """Task 3: choose the pump from Task 2's power and demand, then E_PV, E_pump and E_excess."""
    pump = select_pump(power_kw, q_day_m3, catalog)

    e_pv = pv_energy_kwh_day(pump.total_pv_kwp, psh_avg_kwh_m2_day, pv_derate)
    e_pump = pump_energy_kwh_day(pump.total_motor_kw, pumping_hours)

    return EnergyBalance(
        pump=pump,
        psh_avg_kwh_m2_day=float(psh_avg_kwh_m2_day),
        pv_derate=float(pv_derate),
        pumping_hours=float(pumping_hours),
        e_pv_kwh_day=e_pv,
        e_pump_kwh_day=e_pump,
        e_excess_kwh_day=surplus_energy_kwh_day(e_pv, e_pump),
    )


def monthly_energy_profile(balance: EnergyBalance, solar: SolarResource) -> pd.DataFrame:
    """The same balance for each month's sunshine. The yearly average can hide a summer surplus."""
    pump = balance.pump
    e_pv = [pv_energy_kwh_day(pump.total_pv_kwp, psh, balance.pv_derate) for psh in solar.monthly_psh.values()]

    return pd.DataFrame(
        {
            "psh_kwh_m2_day": list(solar.monthly_psh.values()),
            "e_pv_kwh_day": e_pv,
            "e_excess_kwh_day": [surplus_energy_kwh_day(e, balance.e_pump_kwh_day) for e in e_pv],
        },
        index=pd.Index(list(solar.monthly_psh), name="month"),
    )


# --- One site, all tasks --------------------------------------------------------------------------

@dataclass(frozen=True)
class SiteAssessment:
    cluster: Cluster  # Task 1
    params: DesignParameters
    q_day_m3: float  # Task 2.1
    solar: SolarResource  # Task 2.2
    hydraulic_power_kw: float  # Task 2.3
    energy: EnergyBalance  # Task 3

    def monthly_profile(self) -> pd.DataFrame:
        return monthly_energy_profile(self.energy, self.solar)


def evaluate_site(
    parcels: gpd.GeoDataFrame,
    lat: float,
    lon: float,
    params: DesignParameters = DEFAULT_PARAMS,
    *,
    get_solar: Callable[[float, float], SolarResource] = fetch_solar_resource,
) -> SiteAssessment:
    """Run Tasks 1, 2 and 3 for one point. Task 2's outputs feed Task 3.

    `get_solar(lat, lon)` supplies the sunshine data; replace it for offline runs or Monte Carlo draws.

    Raises:
        EmptyClusterError: no parcel in the command zone.
        SolarDataError: NASA POWER failed.
    """
    cluster = extract_cluster(parcels, lat, lon, params.radius_m)

    q_day = daily_water_demand_m3(cluster)
    solar = get_solar(*cluster.centroid_lat_lon())
    power = hydraulic_power_kw(
        q_day, head_m=params.head_m, efficiency=params.pump_efficiency, pumping_hours=params.pumping_hours
    )

    # Task 3 takes the tier from Task 2's power and demand, and E_PV from Task 2's annual-average sunshine.
    energy = model_energy_balance(
        power, q_day, solar.psh_avg, pumping_hours=params.pumping_hours, pv_derate=params.pv_derate
    )

    return SiteAssessment(cluster, params, q_day, solar, power, energy)
