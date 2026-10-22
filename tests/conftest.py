"""Shared fixtures: a small made-up parcel layer with known geometry, recorded NASA replies, the real layer."""

from __future__ import annotations

import json
import math
import urllib.error
import urllib.request
import zipfile
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace

import geopandas as gpd
import pytest
from shapely.geometry import Point, box

import dss_pipeline as dss

SITE = (38.015, -7.865)  # the spec's Alentejo point; the made-up parcels are drawn around it
FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
def clear_solar_cache():
    """The NASA cache is global; wipe it so tests cannot leak results into each other."""
    dss._solar_cache.clear()


@pytest.fixture(scope="session")
def site() -> tuple[float, float]:
    return SITE


@pytest.fixture(scope="session")
def origin() -> tuple[float, float]:
    """SITE in EPSG:3763 metres."""
    point = gpd.GeoSeries([Point(SITE[1], SITE[0])], crs=dss.WGS84).to_crs(dss.PT_TM06).iloc[0]

    return point.x, point.y


@pytest.fixture(scope="session")
def site_offset(origin: tuple[float, float]) -> Callable[[float, float], tuple[float, float]]:
    """Function (dx, dy) in metres east/north of SITE -> (lat, lon)."""
    x0, y0 = origin

    def to_lat_lon(dx: float, dy: float) -> tuple[float, float]:
        point = gpd.GeoSeries([Point(x0 + dx, y0 + dy)], crs=dss.PT_TM06).to_crs(dss.WGS84).iloc[0]
        return point.y, point.x

    return to_lat_lon


@pytest.fixture
def synthetic_parcels(origin: tuple[float, float]) -> gpd.GeoDataFrame:
    """Seven boxes around SITE (offsets in metres, zone radius 150 m).

    id  crop code    crop     x range; y range              whole ha   inside circle (ha)
    1   3301010600   maize    -50..50;   -50..50            1.0        1.0
    2   3303050000   olives   100..400;  -50..50            3.0        0.4717 (clipped by the circle)
    3   3301280000   tomato   -50..50;   60..100            0.4        0.4
    4   3302000000   pasture  -110..-60; -40..40            0.4        0.4
    5   (missing)    unknown  -40..40;   -110..-60          0.4        0.4
    6   3301010100   wheat    151..160;  -10..10            0.0018     0, just outside the circle
    7   3301010100   wheat    1000..1100; 1000..1100        1.0        0, far away
    """
    x, y = origin
    rows = [
        (1, "3301010600", box(x - 50, y - 50, x + 50, y + 50)),
        (2, "3303050000", box(x + 100, y - 50, x + 400, y + 50)),
        (3, "3301280000", box(x - 50, y + 60, x + 50, y + 100)),
        (4, "3302000000", box(x - 110, y - 40, x - 60, y + 40)),
        (5, None, box(x - 40, y - 110, x + 40, y - 60)),
        (6, "3301010100", box(x + 151, y - 10, x + 160, y + 10)),
        (7, "3301010100", box(x + 1000, y + 1000, x + 1100, y + 1100)),
    ]
    ids, codes, shapes = zip(*rows, strict=True)

    return gpd.GeoDataFrame({"OSA_ID": ids, "EC_hcat_c": codes}, geometry=list(shapes), crs=dss.PT_TM06)


@pytest.fixture(scope="session")
def expected() -> SimpleNamespace:
    """Hand-calculated answers for `synthetic_parcels` at SITE."""
    # Parcel 2 inside the circle: integral over |y| <= 50 of (sqrt(150^2 - y^2) - 100) dy, in m2, as ha.
    p2_in_zone = (50 * math.sqrt(150**2 - 50**2) + 150**2 * math.asin(50 / 150) - 100 * 100) / 1e4

    return SimpleNamespace(
        categories={1: "10", 2: "20", 3: "50", 4: "30", 5: dss.FALLBACK},
        area_ha={1: 1.0, 2: 3.0, 3: 0.4, 4: 0.4, 5: 0.4},
        p2_in_zone_ha=p2_in_zone,
        area_in_zone_ha=2.2 + p2_in_zone,
        q_zone_m3=1.0 * 35 + p2_in_zone * 45 + 0.4 * 55 + 0.4 * 15 + 0.4 * 30,
    )


@pytest.fixture
def zipped_shapefile(tmp_path: Path) -> Callable[..., Path]:
    """Writes a GeoDataFrame as <stem>.zip holding one shapefile, like PT.zip."""

    def write(gdf: gpd.GeoDataFrame, stem: str = "PT_test") -> Path:
        folder = tmp_path / stem
        folder.mkdir()
        gdf.to_file(folder / f"{stem}.shp", engine="pyogrio")

        archive = tmp_path / f"{stem}.zip"
        with zipfile.ZipFile(archive, "w") as zf:
            for member in folder.iterdir():
                zf.write(member, member.name)

        return archive

    return write


@pytest.fixture(scope="session")
def real_parcels() -> gpd.GeoDataFrame:
    if not dss.DEFAULT_DATA_PATH.is_file():
        pytest.skip(f"real parcel file absent at {dss.DEFAULT_DATA_PATH}; run: python run_pipeline.py --download")

    return dss.load_parcels(dss.DEFAULT_DATA_PATH)


# --- NASA POWER -----------------------------------------------------------------------------------

@pytest.fixture(scope="session")
def nasa_bytes() -> Callable[[str], bytes]:
    """Recorded NASA replies at SITE (2026-10-22). 'AG' reports MJ/m2/day, 'RE' reports kWh/m2/day."""
    return lambda community: (FIXTURES / f"nasa_power_alentejo_{community}.json").read_bytes()


@pytest.fixture
def nasa_payload(nasa_bytes: Callable[[str], bytes]) -> Callable[[str], dict]:
    """A fresh, editable copy of a recorded reply."""
    return lambda community: json.loads(nasa_bytes(community))


class StubSolar:
    """Stands in for fetch_solar_resource: returns one fixed result and remembers where it was asked."""

    def __init__(self, resource: dss.SolarResource) -> None:
        self.resource = resource
        self.calls: list[tuple[float, float]] = []

    def __call__(self, lat: float, lon: float) -> dss.SolarResource:
        self.calls.append((lat, lon))
        return self.resource


@pytest.fixture
def stub_solar(nasa_bytes: Callable[[str], bytes]) -> StubSolar:
    """The recorded Alentejo climatology: PSH_avg 4.90, dullest month DEC at 2.11 kWh/m2/day."""
    return StubSolar(dss.parse_power_climatology(json.loads(nasa_bytes("AG")), *SITE))


@pytest.fixture(scope="session")
def nasa_online() -> None:
    """Skip the live-API test when NASA POWER cannot be reached."""
    try:
        with urllib.request.urlopen(dss.NASA_POWER_URL, timeout=5):
            pass
    except urllib.error.HTTPError as exc:  # server answered (the bare endpoint says 4xx), so it is reachable
        exc.close()
    except OSError as exc:
        pytest.skip(f"NASA POWER unreachable: {exc}")
