"""Agri-Energy DSS: single-cluster engine for Tasks 1-3 (clustering, demand + NASA climate, pump + surplus).

Flow
    Task 1  extract_cluster        EuroCrops PT parcels -> 150 m tube-well command zone -> classified cluster
    Task 2  daily_water_demand_m3  Q_day = sum(A_i x WD_i)
            NasaPowerClient        NASA POWER climatology at the cluster centroid -> PSH_avg, design month
            hydraulic_power_kw     P = rho.g.Q.H / (3.6e6 . t . eta)
    Task 3  model_energy_balance   IDCOL tier match from P and Q_day; E_PV from Task 2's PSH_avg; E_excess
            monthly_energy_profile the same balance under each month's PSH (seasonal surplus)
    all     evaluate_site          one call per site, Task 2 outputs wired into Task 3

CRS
    PT.zip ships in EPSG:4326. Every geometric operation (buffer, intersection, area) runs in EPSG:3763
    (ETRS89 / Portugal TM06, metres): the layer is reprojected once at load; the user point is built in
    4326 and reprojected before buffering (a 150-unit buffer in 4326 would span 150 degrees).
    Check: EPSG:3763 areas reproduce the declared OSA_AREA (m2) to a median relative error of 7e-6.
    Domain = EPSG:3763 area of use (mainland Portugal) + 0.1 deg. Far outside it TM06 distorts or returns
    inf, so such points raise EmptyClusterError before projection.

Spatial index
    Layer loaded once (~1 s, 100k polygons). GeoPandas builds an STRtree (`sindex`) lazily on first use;
    `sindex.query(zone, predicate="intersects")` = envelope prefilter + exact GEOS test -> sub-ms per site.

Units
    area ha (1 ha = 1e4 m2) | water demand m3/(ha.day) | Q_day m3/day | motor power kW | PV capacity kWp |
    energy kWh/day | PSH h/day == kWh/(m2.day) at the 1 kW/m2 STC reference irradiance.
    kW / kWp are capacities (P_motor, PV_kWp); kWh/day is daily work (E_PV, E_pump, E_excess).
    NASA POWER ALLSKY_SFC_SW_DWN arrives in the units its payload declares (AG: MJ/(m2.day), /3.6 -> kWh).

Assumptions (data blockers found -> resolution)
    A1  HCAT prefix. All 100,000 PT `EC_hcat_c` codes start with '33', the HCAT root, so the spec's literal
        two-digit rule sends 100 % of parcels to the fallback. Codes resolve by longest prefix against HCAT
        level-1/2 groups (`HCAT_PREFIX_RULES`); the spec's two-digit keys are kept and never collide.
    A2  Area basis. Demand uses parcel area *inside* the command zone. Whole-parcel area lets a 20 ha olive
        block that clips the zone size a ~7 ha well (10x over-sizing). `area_basis="parcel"` restores the
        literal reading; both areas are always reported.
    A3  Membership needs >= 1 m2 overlap: below LPIS positional accuracy a boundary touch is not service area.
    A4  Non-crop HCAT groups (fallow, shrubland, forest/montado, not-known) take the Table 1 fallback
        (30 m3/ha/day) as specified. Conservative: assumes the zone is cropped once water is available.
    A5  Tier = smallest with motor >= P and rated delivery >= Q_day. Past Tier 4: fewest identical units
        (each ~ one borehole), then the smallest tier carrying 1/N of the load.
    A6  E_pump = rated P_motor x t_pump (spec): nameplate draw over the whole window, conservative.
    A7  Units. The spec URL (community=AG) returns ALLSKY_SFC_SW_DWN in MJ/(m2.day), not the kWh the spec
        expects: Alentejo ANN 17.65 = 4.90 kWh. The parser converts by the payload's declared units and
        rejects unknown ones; any PSH > 10 kWh/(m2.day) is still refused as a likely MJ mix-up.
    A8  The spec's Alentejo point (38.015, -7.865) has no parcel within 1.3 km: PT.zip holds exactly 100,000
        parcels, evidently a sample, sparse in Alentejo. The runner shows the error, then re-sites to the
        nearest parcel (`nearest_parcel`).
    A9  NASA query. Point = in-zone farmland centroid rounded to 0.01 deg (~1 km; the solar grid is 1 deg
        CERES SYN1deg), which is also the cache key. Design month = lowest of the 12 climatological months
        (no irrigation calendar given). Timeouts, connection errors and HTTP 429/5xx get 3 attempts with
        1 s / 2 s backoff; HTTP 4xx and malformed payloads fail fast with NASA's own message. Fill values
        (-999) fail loudly: no design month from an incomplete year. Failures are never cached.

Monte Carlo (regional policy)
    `evaluate_site` is deterministic given (parcels, lat, lon, DesignParameters, solar_client) and costs ~ms
    once the layer and STRtree sit in memory and the solar cache is warm, so a regional study is an outer
    loop with a seeded RNG:
        site   ~ parcel representative points (or a grid) in the target district
        params ~ dataclasses.replace(DEFAULT_PARAMS, head_m=<groundwater-depth draw>, pump_efficiency=...)
        solar  ~ a SolarClient stub that samples interannual NASA POWER years, not only the climatology mean
        try: evaluate_site(...)  except EmptyClusterError: tally as non-agricultural
    Policy levers (CAPEX subsidy %, feed-in or PUE tariff on E_excess, borehole grants) transform the outputs
    (total_capex_usd, e_excess_kwh_day) into per-district P10/P50/P90 cost and viability shares per incentive
    scenario. Draws are independent and the exceptions picklable -> embarrassingly parallel.
"""

from __future__ import annotations

import http.client
import json
import logging
import math
import numbers
import shutil
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, Literal, Protocol

import geopandas as gpd
import numpy as np
import pandas as pd
import pyogrio
import pyproj
from shapely.geometry import Point, Polygon

__all__ = [
    "AREA_COLUMNS",
    "COMMAND_ZONE_RADIUS_M",
    "CROP_ARCHETYPES",
    "DEFAULT_DATA_PATH",
    "DEFAULT_PARAMS",
    "DEFAULT_SOLAR_CLIENT",
    "FALLBACK",
    "HCAT_COL",
    "HCAT_PREFIX_RULES",
    "ID_COL",
    "LABEL_COL",
    "MONTHS",
    "NASA_POWER_URL",
    "PT_TM06",
    "PUMP_CATALOG",
    "WGS84",
    "AreaBasis",
    "Cluster",
    "CropArchetype",
    "DSSError",
    "DatasetError",
    "DesignParameters",
    "EmptyClusterError",
    "EnergyBalance",
    "NasaPowerClient",
    "NearestParcel",
    "PumpSelection",
    "SiteAssessment",
    "SolarClient",
    "SolarDataError",
    "SolarResource",
    "classify_parcels",
    "command_zone",
    "daily_water_demand_m3",
    "ensure_dataset",
    "evaluate_site",
    "extract_cluster",
    "hcat_category",
    "hydraulic_power_kw",
    "in_domain",
    "load_parcels",
    "model_energy_balance",
    "monthly_energy_profile",
    "nearest_parcel",
    "parse_power_climatology",
    "pump_energy_kwh_day",
    "pv_energy_kwh_day",
    "select_pump",
    "surplus_energy_kwh_day",
    "water_demand_table",
]

logger = logging.getLogger(__name__)

# --- CRS & geometry -------------------------------------------------------------------------------
WGS84: Final = "EPSG:4326"
PT_TM06: Final = "EPSG:3763"  # ETRS89 / Portugal TM06, metres
TM06_AREA_OF_USE: Final = pyproj.CRS(PT_TM06).area_of_use.bounds  # (W, S, E, N), mainland Portugal
DOMAIN_MARGIN_DEG: Final = 0.1  # border tolerance; TM06 scale error there is still < 1e-4
COMMAND_ZONE_RADIUS_M: Final = 150.0  # pi * 150^2 ~ 7.07 ha
BUFFER_QUAD_SEGS: Final = 64  # 256-gon: area 0.01 % below the true circle
MIN_OVERLAP_M2: Final = 1.0  # A3
M2_PER_HA: Final = 1e4

# --- Dataset --------------------------------------------------------------------------------------
EUROCROPS_PT_URL: Final = "https://zenodo.org/records/10118572/files/PT.zip"
DEFAULT_DATA_PATH: Final = Path(__file__).resolve().parent / "data" / "PT.zip"
HCAT_COL: Final = "EC_hcat_c"  # 10-digit HCAT code, required
ID_COL: Final = "OSA_ID"  # land-occupation unit id, unique in PT (PAR_ID is not)
LABEL_COL: Final = "EC_hcat_n"  # HCAT class name

# --- Physics & spec design values -----------------------------------------------------------------
RHO_WATER_KG_M3: Final = 1000.0
G_M_S2: Final = 9.81
J_PER_KWH: Final = 3.6e6  # 3600 s x 1000 W
MJ_PER_KWH: Final = 3.6
HEAD_M: Final = 40.0  # total dynamic head: static depth + friction
PUMP_EFFICIENCY: Final = 0.55  # wire-to-water (pump x motor)
PUMPING_HOURS: Final = 6.0  # h/day
PV_DERATE: Final = 0.75  # temperature, soiling, wiring, controller
MAX_PLAUSIBLE_PSH: Final = 10.0  # kWh/(m2.day); sunniest deserts average ~8, so larger means MJ (A7)
_TIER_TOL: Final = 1e-9  # absorbs float noise when demand sits exactly on a tier rating

# --- NASA POWER (A9) ------------------------------------------------------------------------------
NASA_POWER_URL: Final = "https://power.larc.nasa.gov/api/temporal/climatology/point"
NASA_PARAMETER: Final = "ALLSKY_SFC_SW_DWN"  # all-sky surface shortwave downward irradiance (GHI)
NASA_COMMUNITY: Final = "AG"  # spec URL; reports MJ/(m2.day), RE/SB report kWh/(m2.day)
NASA_COORD_DECIMALS: Final = 2  # query point and cache key, ~1 km
MONTHS: Final = ("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC")
_KWH_PER_UNIT: Final = {"mj/m^2/day": 1 / MJ_PER_KWH, "kw-hr/m^2/day": 1.0}  # payload units, lower-cased
_RETRYABLE_HTTP: Final = frozenset({429, 500, 502, 503, 504})
_USER_AGENT: Final = "agri-energy-dss/1.0 (research prototype)"

AreaBasis = Literal["zone", "parcel"]
AREA_COLUMNS: Final[dict[str, str]] = {"zone": "area_in_zone_ha", "parcel": "area_ha"}


# --- Errors ---------------------------------------------------------------------------------------
class DSSError(Exception):
    """Base class for pipeline errors."""


class DatasetError(DSSError):
    """Parcel layer missing, unreadable or schema-incompatible."""


class EmptyClusterError(DSSError):
    """No agricultural parcel overlaps the command zone: urban core, water body or outside Portugal."""

    @classmethod
    def for_radius(cls, radius_m: float) -> EmptyClusterError:
        """Spec message; keeps Exception's single-arg signature so instances pickle across processes."""
        return cls(
            "Location is out of agricultural domain or contains no parcels "
            f"within the {radius_m:g}m radius."
        )


class SolarDataError(DSSError):
    """NASA POWER unreachable after retries, or its payload unusable."""


# --- Input validation -----------------------------------------------------------------------------
def _real(name: str, value: object) -> float:
    """Finite real as float. bool/str/None are rejected, not coerced."""
    if isinstance(value, bool) or not isinstance(value, numbers.Real):
        raise TypeError(f"{name} must be a real number, got {type(value).__name__}")
    out = float(value)
    if not math.isfinite(out):
        raise ValueError(f"{name} must be finite, got {out}")
    return out


def _positive(name: str, value: object) -> float:
    out = _real(name, value)
    if out <= 0:
        raise ValueError(f"{name} must be > 0, got {out:g}")
    return out


def _non_negative(name: str, value: object) -> float:
    out = _real(name, value)
    if out < 0:
        raise ValueError(f"{name} must be >= 0, got {out:g}")
    return out


def _fraction(name: str, value: object) -> float:
    out = _real(name, value)
    if not 0 < out <= 1:
        raise ValueError(f"{name} must be in (0, 1], got {out:g}")
    return out


def _hours_per_day(name: str, value: object) -> float:
    out = _real(name, value)
    if not 0 < out <= 24:
        raise ValueError(f"{name} must be in (0, 24] h/day, got {out:g}")
    return out


def _psh(name: str, value: object) -> float:
    """Peak sun hours [kWh/(m2.day)]. Above MAX_PLAUSIBLE_PSH reads as MJ/(m2.day) passed as kWh (A7)."""
    out = _non_negative(name, value)
    if out > MAX_PLAUSIBLE_PSH:
        raise ValueError(
            f"{name} = {out:g} kWh/(m2.day) is above any surface climatology (> {MAX_PLAUSIBLE_PSH:g}); "
            "NASA POWER community=AG reports MJ/(m2.day), so divide by 3.6"
        )
    return out


def _lat_lon(lat: object, lon: object) -> tuple[float, float]:
    lat_f, lon_f = _real("lat", lat), _real("lon", lon)
    if not (-90 <= lat_f <= 90 and -180 <= lon_f <= 180):
        raise ValueError(f"(lat, lon) = ({lat_f}, {lon_f}) is outside WGS84 decimal-degree ranges")
    return lat_f, lon_f


def _area_column(area_basis: str) -> str:
    try:
        return AREA_COLUMNS[area_basis]
    except KeyError:
        raise ValueError(f"area_basis must be one of {sorted(AREA_COLUMNS)}, got {area_basis!r}") from None


# --- Design parameters ----------------------------------------------------------------------------
@dataclass(frozen=True)
class DesignParameters:
    """Engineering assumptions for one evaluation; spec values by default.

    Immutable, so one instance is safely shared and `dataclasses.replace` derives Monte Carlo draws.
    """

    radius_m: float = COMMAND_ZONE_RADIUS_M
    head_m: float = HEAD_M
    pump_efficiency: float = PUMP_EFFICIENCY
    pumping_hours: float = PUMPING_HOURS
    pv_derate: float = PV_DERATE
    area_basis: AreaBasis = "zone"

    def __post_init__(self) -> None:
        _positive("radius_m", self.radius_m)
        _positive("head_m", self.head_m)
        _fraction("pump_efficiency", self.pump_efficiency)
        _hours_per_day("pumping_hours", self.pumping_hours)
        _fraction("pv_derate", self.pv_derate)
        _area_column(self.area_basis)


DEFAULT_PARAMS: Final = DesignParameters()


# --- Reference Table 1: agronomic archetypes ------------------------------------------------------
FALLBACK: Final = "other"


@dataclass(frozen=True)
class CropArchetype:
    """Reference Table 1 row. Yield and price are carried for Task 4 (out of scope)."""

    key: str
    name: str
    representative_crop: str
    water_demand_m3_ha_day: float
    yield_t_ha: float
    price_usd_kg: float


CROP_ARCHETYPES: Final[dict[str, CropArchetype]] = {
    a.key: a
    for a in (
        CropArchetype("10", "Cereals & Grains", "Maize / Wheat", 35.0, 7.0, 0.25),
        CropArchetype("20", "Orchards & Fruits", "Citrus / Fruit Tree", 45.0, 12.0, 0.80),
        CropArchetype("30", "Pastures & Meadows", "Fodder Grass", 15.0, 4.0, 0.10),
        CropArchetype("50", "Vegetables / High-Value", "Tomato / Pepper", 55.0, 25.0, 0.60),
        CropArchetype(FALLBACK, "Fallback Field Crop", "Mixed / Pulses", 30.0, 5.0, 0.30),
    )
}

# HCAT prefix -> Table 1 key, longest prefix wins (A1). Unmatched -> fallback, i.e. pulses 330102,
# oilseeds/industrial 330106, fallow 330111, other arable 330199, forest 3306, not-known 3399.
HCAT_PREFIX_RULES: Final[dict[str, str]] = {
    "10": "10",  # spec's literal two-digit scheme
    "20": "20",
    "30": "30",
    "50": "50",
    "330101": "10",  # cereals: wheat, barley, oats, rye, maize, rice, triticale, sorghum
    "3302": "30",  # permanent pasture, meadow, grassland
    "330109": "30",  # harvested green: temporary grass, ryegrass, alfalfa, clover
    "3303": "20",  # permanent crops: orchards, berries, nuts, citrus, olives, vineyards
    "330308": FALLBACK,  # under 3303 but, in PT, non-grazeable shrubland and hedges
    "330103": "50",  # potatoes
    "330104": "50",  # sweet potatoes
    "330107": "50",  # fresh vegetables
    "330108": "50",  # flowers, ornamentals (intensive horticulture)
    "330113": "50",  # strawberries
    "330114": "50",  # cucurbits: melon, pumpkin, watermelon, courgette
    "330121": "50",  # brassicas
    "330122": "50",  # alliums: garlic, onion
    "330126": "50",  # aubergine
    "330128": "50",  # tomato
    "330129": "50",  # roots: carrot, beet, turnip
    "330131": "50",  # leafy: lettuce, spinach
}
_RULE_LENGTHS: Final = sorted({len(k) for k in HCAT_PREFIX_RULES}, reverse=True)


def _normalise_hcat(code: object) -> str | None:
    """'3301010600', 3301010600 and 3301010600.0 -> '3301010600'; missing or non-numeric -> None."""
    integral = isinstance(code, numbers.Integral) and not isinstance(code, bool)
    whole_float = isinstance(code, float) and code.is_integer()  # numeric dbf field; NaN fails is_integer()
    if isinstance(code, str):
        text = code.strip()
    elif integral or whole_float:
        text = str(int(code))
    else:
        return None
    return text if text.isascii() and text.isdigit() else None


def hcat_category(code: object) -> str:
    """Table 1 key ('10', '20', '30', '50' or FALLBACK) for one HCAT code, by longest-prefix match."""
    text = _normalise_hcat(code)
    if text is None:
        return FALLBACK
    for n in _RULE_LENGTHS:
        key = HCAT_PREFIX_RULES.get(text[:n])
        if key is not None:
            return key
    return FALLBACK


def classify_parcels(parcels: pd.DataFrame, code_col: str = HCAT_COL) -> pd.DataFrame:
    """Copy with Table 1 category key, name and water demand [m3/(ha.day)].

    Resolves unique codes only (~80 in PT), then maps: O(unique) Python calls, not O(rows).
    """
    codes = parcels[code_col]
    keys = codes.map({c: hcat_category(c) for c in codes.dropna().unique()}).fillna(FALLBACK)
    return parcels.assign(
        crop_category=keys,
        crop_category_name=keys.map({k: a.name for k, a in CROP_ARCHETYPES.items()}),
        water_demand_m3_ha_day=keys.map({k: a.water_demand_m3_ha_day for k, a in CROP_ARCHETYPES.items()}),
    )


# --- Task 1: ETL ----------------------------------------------------------------------------------
def ensure_dataset(
    path: str | Path = DEFAULT_DATA_PATH, url: str = EUROCROPS_PT_URL, timeout_s: float = 60.0
) -> Path:
    """Return `path`, downloading the EuroCrops PT archive first if absent or corrupt.

    Streams to `<path>.part` and renames on success, so an interrupted run never leaves a truncated zip.
    """
    path = Path(path)
    if path.is_file() and zipfile.is_zipfile(path):
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    part = path.with_name(path.name + ".part")
    logger.info("Downloading %s -> %s", url, path)
    try:
        with urllib.request.urlopen(url, timeout=timeout_s) as response, part.open("wb") as sink:
            shutil.copyfileobj(response, sink, length=1 << 20)
        if not zipfile.is_zipfile(part):
            raise DatasetError(f"{url} did not return a ZIP archive")
        part.replace(path)
    except OSError as exc:  # URLError, HTTPError, timeouts, disk errors
        raise DatasetError(f"Could not download {url}: {exc}") from exc
    finally:
        part.unlink(missing_ok=True)
    return path


def _zip_source(zip_path: Path) -> str:
    """GDAL source string for the single shapefile inside `zip_path`."""
    try:
        with zipfile.ZipFile(zip_path) as archive:
            members = [
                n for n in archive.namelist() if n.lower().endswith(".shp") and not n.startswith("__MACOSX/")
            ]
    except zipfile.BadZipFile as exc:
        raise DatasetError(f"{zip_path.name} is not a valid ZIP archive") from exc
    if len(members) != 1:
        raise DatasetError(f"Expected exactly one .shp in {zip_path.name}, found {members or 'none'}")
    return f"zip://{zip_path.resolve().as_posix()}!{members[0]}"


def _infer_crs(bounds: np.ndarray) -> str:
    """Pick between the two CRSs the spec allows: degrees fit [-180, 180] x [-90, 90], TM06 metres do not."""
    minx, miny, maxx, maxy = bounds
    return WGS84 if -180 <= minx <= maxx <= 180 and -90 <= miny <= maxy <= 90 else PT_TM06


def load_parcels(path: str | Path = DEFAULT_DATA_PATH) -> gpd.GeoDataFrame:
    """Load EuroCrops parcels as a clean EPSG:3763 GeoDataFrame.

    `path` is a zipped shapefile (PT.zip) or any vector file GDAL reads. Only the HCAT code, its label and
    the parcel id are read: smaller frame, and it skips the PT dbf's mis-encoded Portuguese text fields.
    Null, empty and non-polygonal geometries are dropped; a missing CRS is inferred (with a warning).

    Raises:
        DatasetError: file missing/unreadable, no HCAT column, or no usable polygons.
    """
    path = Path(path)
    if not path.is_file():
        raise DatasetError(f"Parcel layer not found: {path} (fetch with ensure_dataset() or --download)")
    source = _zip_source(path) if path.suffix.lower() == ".zip" else str(path)
    try:
        fields = set(pyogrio.read_info(source)["fields"])
        if HCAT_COL not in fields:
            raise DatasetError(f"{path.name} lacks required column {HCAT_COL!r}; has {sorted(fields)}")
        columns = [c for c in (ID_COL, HCAT_COL, LABEL_COL) if c in fields]
        gdf = gpd.read_file(source, columns=columns, engine="pyogrio")
    except (pyogrio.errors.DataSourceError, pyogrio.errors.DataLayerError) as exc:
        raise DatasetError(f"Cannot read {path}: {exc}") from exc

    usable = gdf.geom_type.isin(("Polygon", "MultiPolygon")) & ~gdf.geometry.is_empty
    if not usable.all():
        logger.warning("Dropped %d null, empty or non-polygonal geometries", int((~usable).sum()))
        gdf = gdf.loc[usable]
    if gdf.empty:
        raise DatasetError(f"{path.name} contains no usable polygons")
    if gdf.crs is None:
        gdf = gdf.set_crs(_infer_crs(gdf.total_bounds))
        logger.warning("%s has no CRS; inferred %s from coordinate ranges", path.name, gdf.crs.to_string())
    source_crs = gdf.crs.to_string()
    gdf = gdf.to_crs(PT_TM06).reset_index(drop=True)

    ids = gdf.get(ID_COL)
    if ids is not None and pd.api.types.is_float_dtype(ids) and ids.dropna().mod(1).eq(0).all():
        gdf[ID_COL] = ids.astype("Int64")  # dbf stores integer ids as doubles
    logger.info("Loaded %d parcels from %s (%s -> %s)", len(gdf), path.name, source_crs, PT_TM06)
    return gdf


# --- Task 1: clustering ---------------------------------------------------------------------------
def in_domain(lat: float, lon: float) -> bool:
    """True if (lat, lon) lies in the EPSG:3763 area of use (mainland Portugal) plus the border margin."""
    lat, lon = _lat_lon(lat, lon)
    west, south, east, north = TM06_AREA_OF_USE
    m = DOMAIN_MARGIN_DEG
    return south - m <= lat <= north + m and west - m <= lon <= east + m


def _to_tm06(lat: float, lon: float) -> Point:
    """WGS84 decimal degrees -> EPSG:3763 metres. Point takes (x=lon, y=lat)."""
    return gpd.GeoSeries([Point(lon, lat)], crs=WGS84).to_crs(PT_TM06).iloc[0]


def command_zone(lat: float, lon: float, radius_m: float = COMMAND_ZONE_RADIUS_M) -> Polygon:
    """Circular tube-well command zone in EPSG:3763: project first, then buffer in metres.

    Raises:
        TypeError / ValueError: malformed coordinates or radius.
        EmptyClusterError: point outside the TM06 domain, where no PT parcel can exist.
    """
    radius_m = _positive("radius_m", radius_m)
    if not in_domain(lat, lon):
        raise EmptyClusterError.for_radius(radius_m)
    return _to_tm06(*_lat_lon(lat, lon)).buffer(radius_m, quad_segs=BUFFER_QUAD_SEGS)


def _check_layer(parcels: gpd.GeoDataFrame) -> None:
    if not isinstance(parcels, gpd.GeoDataFrame):
        raise TypeError(f"parcels must be a GeoDataFrame, got {type(parcels).__name__}")
    if parcels.crs is None or parcels.crs.to_epsg() != 3763:
        raise ValueError(f"parcel layer must be in {PT_TM06}, got {parcels.crs}; load it with load_parcels()")
    if HCAT_COL not in parcels.columns:
        raise ValueError(f"parcel layer lacks required column {HCAT_COL!r}")
    if not parcels.index.is_unique:
        raise ValueError("parcel layer index must be unique (row labels identify parcels)")


@dataclass(frozen=True)
class Cluster:
    """Task 1 output: parcels served by one command zone. Geometry in EPSG:3763.

    `parcels` keeps the layer's row labels and adds `area_ha` (whole parcel), `area_in_zone_ha` (clipped),
    `crop_category`, `crop_category_name` and `water_demand_m3_ha_day`.
    """

    lat: float
    lon: float
    radius_m: float
    zone: Polygon = field(repr=False)
    parcels: gpd.GeoDataFrame = field(repr=False)

    @property
    def n_parcels(self) -> int:
        return len(self.parcels)

    @property
    def zone_ha(self) -> float:
        """Command-zone area, ~7.07 ha at 150 m."""
        return self.zone.area / M2_PER_HA

    @property
    def area_in_zone_ha(self) -> float:
        """Declared farmland inside the zone."""
        return float(self.parcels["area_in_zone_ha"].sum())

    @property
    def parcel_area_ha(self) -> float:
        """Whole-parcel area of all members (literal spec reading)."""
        return float(self.parcels["area_ha"].sum())

    def centroid_lat_lon(self) -> tuple[float, float]:
        """Centroid of the in-zone farmland in WGS84: the NASA POWER query point for Task 2."""
        footprint = self.parcels.geometry.intersection(self.zone).union_all()
        centre = gpd.GeoSeries([footprint.centroid], crs=PT_TM06).to_crs(WGS84).iloc[0]
        return centre.y, centre.x


def extract_cluster(
    parcels: gpd.GeoDataFrame, lat: float, lon: float, radius_m: float = COMMAND_ZONE_RADIUS_M
) -> Cluster:
    """Task 1: parcels overlapping the command zone at (lat, lon), with hectare areas and Table 1 categories.

    Raises:
        TypeError / ValueError: malformed coordinates, or a layer not in EPSG:3763.
        EmptyClusterError: no parcel overlaps the zone by >= MIN_OVERLAP_M2 (urban, water, outside Portugal).
    """
    _check_layer(parcels)
    lat, lon = _lat_lon(lat, lon)
    zone = command_zone(lat, lon, radius_m)
    hits = np.sort(parcels.sindex.query(zone, predicate="intersects"))
    members = parcels.iloc[hits]
    geom = members.geometry.make_valid()  # PT has 78 self-intersecting polygons; valid ones pass unchanged
    in_zone_m2 = geom.intersection(zone).area
    keep = in_zone_m2 >= MIN_OVERLAP_M2
    if not keep.any():
        raise EmptyClusterError.for_radius(radius_m)
    members = members.set_geometry(geom)[keep].assign(
        area_ha=geom[keep].area / M2_PER_HA,
        area_in_zone_ha=in_zone_m2[keep] / M2_PER_HA,
    )
    logger.debug("Cluster (%.5f, %.5f): %d of %d candidates kept", lat, lon, int(keep.sum()), len(hits))
    return Cluster(lat=lat, lon=lon, radius_m=float(radius_m), zone=zone, parcels=classify_parcels(members))


@dataclass(frozen=True)
class NearestParcel:
    """Closest parcel to a query point: re-siting hint after an EmptyClusterError."""

    row: int  # positional index in the layer
    parcel_id: object  # ID_COL value, or the row label if the layer has no id column
    distance_m: float
    lat: float  # representative point, guaranteed inside the parcel
    lon: float


def nearest_parcel(parcels: gpd.GeoDataFrame, lat: float, lon: float) -> NearestParcel:
    """Nearest parcel to (lat, lon) via the STRtree.

    Raises:
        ValueError: point outside the TM06 domain, where planar distances are meaningless.
    """
    _check_layer(parcels)
    lat, lon = _lat_lon(lat, lon)
    if not in_domain(lat, lon):
        raise ValueError(f"({lat}, {lon}) is outside the {PT_TM06} domain")
    (_, rows), distances = parcels.sindex.nearest(_to_tm06(lat, lon), return_distance=True)
    row = int(rows[0])
    anchor = gpd.GeoSeries([parcels.geometry.iloc[row].representative_point()], crs=PT_TM06).to_crs(WGS84)
    parcel_id = parcels[ID_COL].iloc[row] if ID_COL in parcels.columns else parcels.index[row]
    return NearestParcel(row, parcel_id, float(distances[0]), float(anchor.y.iloc[0]), float(anchor.x.iloc[0]))


# --- Task 2.1 / 2.3: demand and hydraulic power ---------------------------------------------------
def water_demand_table(cluster: Cluster, area_basis: AreaBasis = "zone") -> pd.DataFrame:
    """Per-category peak demand Q_c = sum(A_i [ha]) x WD_c [m3/(ha.day)] -> m3/day, plus both area totals."""
    area_col = _area_column(area_basis)
    table = pd.DataFrame(cluster.parcels.drop(columns=cluster.parcels.geometry.name))
    table["q_day_m3"] = table[area_col] * table["water_demand_m3_ha_day"]
    return table.groupby(
        ["crop_category", "crop_category_name", "water_demand_m3_ha_day"], as_index=False, sort=True
    ).agg(
        n_parcels=("q_day_m3", "size"),
        area_in_zone_ha=("area_in_zone_ha", "sum"),
        area_ha=("area_ha", "sum"),
        q_day_m3=("q_day_m3", "sum"),
    )


def daily_water_demand_m3(cluster: Cluster, area_basis: AreaBasis = "zone") -> float:
    """Q_day = sum_i A_i x WD_i [m3/day] (Task 2.1)."""
    return float(water_demand_table(cluster, area_basis)["q_day_m3"].sum())


def hydraulic_power_kw(
    q_day_m3: float,
    *,
    head_m: float = HEAD_M,
    efficiency: float = PUMP_EFFICIENCY,
    pumping_hours: float = PUMPING_HOURS,
) -> float:
    """Required pump power P = rho.g.Q_day.H / (3.6e6 . t_pump . eta) [kW] (Task 2.3).

    rho[kg/m3] g[m/s2] Q[m3/day] H[m] = J/day; /3.6e6 -> kWh/day; /t[h/day] -> kW hydraulic;
    /eta -> kW drawn by the motor, which is what the catalog's rated motor power is compared against.
    """
    q_day_m3 = _non_negative("q_day_m3", q_day_m3)
    head_m = _positive("head_m", head_m)
    efficiency = _fraction("efficiency", efficiency)
    pumping_hours = _hours_per_day("pumping_hours", pumping_hours)
    return RHO_WATER_KG_M3 * G_M_S2 * q_day_m3 * head_m / (J_PER_KWH * pumping_hours * efficiency)


# --- Task 2.2: NASA POWER solar resource ----------------------------------------------------------
@dataclass(frozen=True)
class SolarResource:
    """Task 2.2 output: climatological daily insolation in kWh/(m2.day), i.e. PSH [h/day] at 1 kW/m2.

    `lat`, `lon` is the point actually queried (the rounded cluster centroid).
    """

    lat: float
    lon: float
    monthly_psh: tuple[float, ...]  # JAN..DEC
    psh_avg: float  # annual mean, NASA's ANN
    source: str = "NASA POWER"

    def __post_init__(self) -> None:
        lat, lon = _lat_lon(self.lat, self.lon)
        monthly = tuple(self.monthly_psh)
        if len(monthly) != len(MONTHS):
            raise ValueError(f"monthly_psh needs {len(MONTHS)} values (JAN..DEC), got {len(monthly)}")
        normalised = {  # frozen: store validated floats and an immutable tuple
            "lat": lat,
            "lon": lon,
            "monthly_psh": tuple(_psh(f"PSH {m}", v) for m, v in zip(MONTHS, monthly, strict=True)),
            "psh_avg": _psh("PSH ANN", self.psh_avg),
        }
        for name, value in normalised.items():
            object.__setattr__(self, name, value)

    @property
    def design_month(self) -> str:
        """Lowest-PSH month (Task 2.2): the worst case the PV array has to carry."""
        return MONTHS[int(np.argmin(self.monthly_psh))]

    @property
    def design_psh(self) -> float:
        """PSH of the design month [kWh/(m2.day)]."""
        return min(self.monthly_psh)


class SolarClient(Protocol):
    """Source of Task 2.2 data: NasaPowerClient, a test stub or a Monte Carlo sampler."""

    def solar_resource(self, lat: float, lon: float) -> SolarResource: ...


def _usable_value(value: object) -> bool:
    return isinstance(value, numbers.Real) and not isinstance(value, bool) and math.isfinite(value) and value >= 0


def parse_power_climatology(payload: Any, lat: float, lon: float) -> SolarResource:
    """Validate a NASA POWER climatology payload; convert ALLSKY_SFC_SW_DWN to kWh/(m2.day).

    Units come from the payload's own metadata, never assumed (A7). Negative values are NASA's -999 fill.

    Raises:
        SolarDataError: missing fields, unknown units, gaps or implausible values.
    """
    try:
        values = payload["properties"]["parameter"][NASA_PARAMETER]
        units = payload["parameters"][NASA_PARAMETER]["units"]
    except (KeyError, TypeError, IndexError) as exc:
        raise SolarDataError(f"NASA POWER payload lacks {NASA_PARAMETER} values or units") from exc
    scale = _KWH_PER_UNIT.get(str(units).strip().lower())
    if scale is None:
        raise SolarDataError(f"NASA POWER reported {NASA_PARAMETER} in unexpected units {units!r}")
    if not isinstance(values, dict):
        raise SolarDataError(f"NASA POWER {NASA_PARAMETER} is not a month -> value mapping")
    gaps = [key for key in (*MONTHS, "ANN") if not _usable_value(values.get(key))]
    if gaps:
        raise SolarDataError(f"NASA POWER has no usable data (fill value or gap) for {', '.join(gaps)}")
    header = payload.get("header")
    sources = "/".join(map(str, header.get("sources", []))) if isinstance(header, dict) else ""
    label = f"NASA POWER {sources} climatology" if sources else "NASA POWER climatology"
    try:
        return SolarResource(
            lat,
            lon,
            tuple(values[m] * scale for m in MONTHS),
            values["ANN"] * scale,
            source=f"{label} ({units})",
        )
    except ValueError as exc:
        raise SolarDataError(f"NASA POWER values implausible: {exc}") from exc


def _http_get(url: str, timeout_s: float) -> bytes:
    """Default transport: one GET; urllib / http.client errors propagate untouched."""
    request = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT, "Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout_s) as response:
        return response.read()


def _http_error_detail(exc: urllib.error.HTTPError) -> str:
    """NASA POWER explains 4xx errors in a JSON 'messages' list; else the HTTP reason phrase."""
    try:
        messages = json.loads(exc.read(4096)).get("messages")
    except (OSError, ValueError, AttributeError):
        messages = None
    return "; ".join(map(str, messages)) if isinstance(messages, list) and messages else str(exc.reason)


class NasaPowerClient:
    """Task 2.2: NASA POWER climatology for a WGS84 point, as a SolarResource (A9).

    Per-attempt timeout; bounded retries with exponential backoff on transient failures only; payload
    validation and unit normalisation; in-memory cache keyed on the rounded point, so re-runs and sites in
    the same ~1 km cell cost one request. `transport` and `sleep` are injectable for tests.
    """

    def __init__(
        self,
        *,
        timeout_s: float = 10.0,
        attempts: int = 3,
        backoff_s: float = 1.0,
        community: str = NASA_COMMUNITY,
        transport: Callable[[str, float], bytes] = _http_get,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if isinstance(attempts, bool) or not isinstance(attempts, int) or attempts < 1:
            raise ValueError(f"attempts must be an integer >= 1, got {attempts!r}")
        self.timeout_s = _positive("timeout_s", timeout_s)
        self.attempts = attempts
        self.backoff_s = _non_negative("backoff_s", backoff_s)
        self.community = community
        self._transport = transport
        self._sleep = sleep
        self._cache: dict[tuple[float, float], SolarResource] = {}

    def solar_resource(self, lat: float, lon: float) -> SolarResource:
        """Climatology at (lat, lon); cached per rounded point. Failures are never cached.

        Raises:
            SolarDataError: unreachable after retries, HTTP 4xx, or unusable payload.
        """
        lat, lon = _lat_lon(lat, lon)
        key = (round(lat, NASA_COORD_DECIMALS), round(lon, NASA_COORD_DECIMALS))
        if key not in self._cache:
            body = self._fetch(self._url(*key))
            try:
                payload = json.loads(body)
            except ValueError as exc:  # JSONDecodeError, UnicodeDecodeError
                raise SolarDataError("NASA POWER response is not valid JSON") from exc
            self._cache[key] = parse_power_climatology(payload, *key)
        return self._cache[key]

    def _url(self, lat: float, lon: float) -> str:
        query = {
            "parameters": NASA_PARAMETER,
            "community": self.community,
            "longitude": lon,
            "latitude": lat,
            "format": "JSON",
        }
        return f"{NASA_POWER_URL}?{urllib.parse.urlencode(query)}"

    def _fetch(self, url: str) -> bytes:
        """GET; retries timeouts, connection errors and HTTP 429/5xx. Everything else fails fast."""
        attempt = 1
        while True:
            try:
                return self._transport(url, self.timeout_s)
            except urllib.error.HTTPError as exc:  # before OSError, which it subclasses
                if exc.code not in _RETRYABLE_HTTP or attempt >= self.attempts:
                    detail = _http_error_detail(exc)
                    raise SolarDataError(f"NASA POWER HTTP {exc.code} after {attempt} attempt(s): {detail}") from exc
                reason = f"HTTP {exc.code}"
            except (OSError, http.client.HTTPException) as exc:  # DNS, refused, reset, timeout, truncated body
                if attempt >= self.attempts:
                    raise SolarDataError(f"NASA POWER unreachable after {attempt} attempt(s): {exc!r}") from exc
                reason = repr(exc)
            delay = self.backoff_s * 2 ** (attempt - 1)
            logger.warning("NASA POWER attempt %d/%d: %s; retry in %.1f s", attempt, self.attempts, reason, delay)
            self._sleep(delay)
            attempt += 1


DEFAULT_SOLAR_CLIENT: Final = NasaPowerClient()  # shared cache: notebook re-runs skip the network


# --- Task 3: equipment matching -------------------------------------------------------------------
# Reference Table 2 (IDCOL). Read-only; pass a modified copy to `select_pump` for what-if catalogs.
PUMP_CATALOG: Final = pd.DataFrame(
    [  # tier, motor kW, motor HP, dedicated PV kWp, installed CAPEX USD, max delivery m3/day at 40 m head
        (1, 3.7, 5.0, 5.0, 7_500, 250),
        (2, 5.5, 7.5, 7.5, 10_500, 400),
        (3, 7.5, 10.0, 10.0, 14_000, 580),
        (4, 11.0, 15.0, 15.0, 19_500, 850),
    ],
    columns=["tier", "motor_kw", "motor_hp", "pv_kwp", "capex_usd", "max_delivery_m3_day"],
)
_CATALOG_COLS: Final = ("tier", "motor_kw", "pv_kwp", "capex_usd", "max_delivery_m3_day")


@dataclass(frozen=True)
class PumpSelection:
    """IDCOL match. Ratings are per unit; `total_*` scale with `n_units`."""

    tier: int
    n_units: int
    motor_kw: float
    pv_kwp: float
    capex_usd: float
    max_delivery_m3_day: float  # at 40 m head
    required_power_kw: float
    q_day_m3: float

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

    @property
    def total_delivery_m3_day(self) -> float:
        return self.n_units * self.max_delivery_m3_day

    @property
    def load_factor(self) -> float:
        """Required / installed motor power: headroom indicator."""
        return self.required_power_kw / self.total_motor_kw


def _check_catalog(catalog: pd.DataFrame) -> None:
    missing = [c for c in _CATALOG_COLS if c not in catalog.columns]
    if missing:
        raise ValueError(f"pump catalog lacks columns {missing}")
    if catalog.empty:
        raise ValueError("pump catalog is empty")
    ratings = catalog[["motor_kw", "pv_kwp", "max_delivery_m3_day"]]
    if ratings.isna().any().any() or (ratings <= 0).any().any():
        raise ValueError("pump catalog ratings must be positive numbers")


def select_pump(power_kw: float, q_day_m3: float, catalog: pd.DataFrame = PUMP_CATALOG) -> PumpSelection:
    """Task 3.1: smallest tier with motor >= P and rated delivery >= Q_day.

    Past the largest tier (A5): N identical units share the load. N is minimised first (each unit ~ one
    borehole), then the smallest tier covering P/N and Q_day/N. The delivery rating is quoted at 40 m head.
    """
    power_kw = _positive("power_kw", power_kw)
    q_day_m3 = _positive("q_day_m3", q_day_m3)
    _check_catalog(catalog)
    # Units each tier needs to meet both constraints, vectorised over the catalog.
    units = (
        np.maximum(
            np.ceil(power_kw / catalog["motor_kw"] - _TIER_TOL),
            np.ceil(q_day_m3 / catalog["max_delivery_m3_day"] - _TIER_TOL),
        )
        .clip(lower=1)
        .astype(int)
    )
    best = catalog.assign(n_units=units).sort_values(["n_units", "motor_kw"]).iloc[0]
    return PumpSelection(
        tier=int(best["tier"]),
        n_units=int(best["n_units"]),
        motor_kw=float(best["motor_kw"]),
        pv_kwp=float(best["pv_kwp"]),
        capex_usd=float(best["capex_usd"]),
        max_delivery_m3_day=float(best["max_delivery_m3_day"]),
        required_power_kw=power_kw,
        q_day_m3=q_day_m3,
    )


# --- Task 3: surplus energy -----------------------------------------------------------------------
def pv_energy_kwh_day(pv_kwp: float, psh_kwh_m2_day: float, derate: float = PV_DERATE) -> float:
    """E_PV = PV_kWp x PSH x derate [kWh/day] (Task 3.2).

    PSH in kWh/(m2.day) equals h/day at the 1 kW/m2 rating irradiance, so kWp x h/day = kWh/day.

    Raises:
        ValueError: PSH above MAX_PLAUSIBLE_PSH, almost surely MJ/(m2.day) passed as kWh (A7).
    """
    return _non_negative("pv_kwp", pv_kwp) * _psh("psh_kwh_m2_day", psh_kwh_m2_day) * _fraction("derate", derate)


def pump_energy_kwh_day(motor_kw: float, pumping_hours: float = PUMPING_HOURS) -> float:
    """E_pump = P_motor x t_pump [kWh/day] (Task 3.3); rated draw over the whole window (A6)."""
    return _non_negative("motor_kw", motor_kw) * _hours_per_day("pumping_hours", pumping_hours)


def surplus_energy_kwh_day(e_pv_kwh_day: float, e_pump_kwh_day: float) -> float:
    """E_excess = max(0, E_PV - E_pump) [kWh/day] (Task 3.4)."""
    return max(0.0, _non_negative("e_pv_kwh_day", e_pv_kwh_day) - _non_negative("e_pump_kwh_day", e_pump_kwh_day))


@dataclass(frozen=True)
class EnergyBalance:
    """Task 3 daily energy balance on an average day (PSH_avg)."""

    pump: PumpSelection
    psh_avg_kwh_m2_day: float
    pv_derate: float
    pumping_hours: float
    e_pv_kwh_day: float
    e_pump_kwh_day: float
    e_excess_kwh_day: float

    @property
    def e_deficit_kwh_day(self) -> float:
        """Shortfall when PV cannot carry the full pumping window; the spec's max(0, .) hides it."""
        return max(0.0, self.e_pump_kwh_day - self.e_pv_kwh_day)

    @property
    def breakeven_psh(self) -> float:
        """PSH [kWh/(m2.day)] at which E_PV = E_pump; surplus exists only above it."""
        return self.e_pump_kwh_day / (self.pump.total_pv_kwp * self.pv_derate)


def model_energy_balance(
    power_kw: float,
    q_day_m3: float,
    psh_avg_kwh_m2_day: float,
    *,
    pumping_hours: float = PUMPING_HOURS,
    pv_derate: float = PV_DERATE,
    catalog: pd.DataFrame = PUMP_CATALOG,
) -> EnergyBalance:
    """Task 3: tier match, then E_PV, E_pump and E_excess for the installed fleet.

    Table 2 pairs ~1.35 kWp per motor kW, so with a 0.75 derate over 6 h/day E_excess > 0 needs PSH > ~5.9:
    zero at mainland Portugal's annual mean (~4.9), positive only in the summer months.
    """
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
    """Task 3 per month: the same fleet under each month's climatological PSH (Task 2.2).

    The spec's E_excess uses PSH_avg; the annual mean hides summer surplus and winter shortfall.
    """
    kwp, derate, e_pump = balance.pump.total_pv_kwp, balance.pv_derate, balance.e_pump_kwh_day
    e_pv = [pv_energy_kwh_day(kwp, psh, derate) for psh in solar.monthly_psh]
    return pd.DataFrame(
        {
            "psh_kwh_m2_day": solar.monthly_psh,
            "e_pv_kwh_day": e_pv,
            "e_excess_kwh_day": [surplus_energy_kwh_day(e, e_pump) for e in e_pv],
        },
        index=pd.Index(MONTHS, name="month"),
    )


# --- Orchestration --------------------------------------------------------------------------------
@dataclass(frozen=True)
class SiteAssessment:
    """One command zone, end to end."""

    cluster: Cluster  # Task 1
    params: DesignParameters
    q_day_m3: float  # Task 2.1
    solar: SolarResource  # Task 2.2
    hydraulic_power_kw: float  # Task 2.3
    energy: EnergyBalance  # Task 3

    def monthly_profile(self) -> pd.DataFrame:
        """Seasonal view of Task 3 (`monthly_energy_profile`)."""
        return monthly_energy_profile(self.energy, self.solar)


def evaluate_site(
    parcels: gpd.GeoDataFrame,
    lat: float,
    lon: float,
    params: DesignParameters = DEFAULT_PARAMS,
    *,
    solar_client: SolarClient | None = None,
) -> SiteAssessment:
    """Task 1 -> Task 2 -> Task 3 for one site.

    Task 2 queries `solar_client` (default: the shared NasaPowerClient) at the cluster centroid. Its PSH_avg
    drives E_PV, its P and Q_day drive the tier match, and one t_pump feeds both sizing and E_pump.

    Raises:
        EmptyClusterError: no parcel in the command zone.
        SolarDataError: NASA POWER unreachable after retries, or its payload unusable.
    """
    cluster = extract_cluster(parcels, lat, lon, params.radius_m)
    q_day = daily_water_demand_m3(cluster, params.area_basis)
    client = DEFAULT_SOLAR_CLIENT if solar_client is None else solar_client
    solar = client.solar_resource(*cluster.centroid_lat_lon())
    power = hydraulic_power_kw(
        q_day, head_m=params.head_m, efficiency=params.pump_efficiency, pumping_hours=params.pumping_hours
    )
    energy = model_energy_balance(
        power, q_day, solar.psh_avg, pumping_hours=params.pumping_hours, pv_derate=params.pv_derate
    )
    return SiteAssessment(cluster, params, q_day, solar, power, energy)
