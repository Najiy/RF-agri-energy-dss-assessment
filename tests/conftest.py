"""Shared fixtures: a synthetic EPSG:3763 layer with analytically known geometry, and the real layer."""

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

SITE = (38.015, -7.865)  # spec Alentejo point; synthetic parcels surround it
FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="session")
def site() -> tuple[float, float]:
    return SITE


@pytest.fixture(scope="session")
def origin() -> tuple[float, float]:
    """SITE in EPSG:3763 metres."""
    pt = gpd.GeoSeries([Point(SITE[1], SITE[0])], crs=dss.WGS84).to_crs(dss.PT_TM06).iloc[0]
    return pt.x, pt.y


@pytest.fixture(scope="session")
def site_offset(origin: tuple[float, float]) -> Callable[[float, float], tuple[float, float]]:
    """(dx, dy) metres east/north of SITE -> (lat, lon)."""
    x0, y0 = origin

    def to_lat_lon(dx: float, dy: float) -> tuple[float, float]:
        pt = gpd.GeoSeries([Point(x0 + dx, y0 + dy)], crs=dss.PT_TM06).to_crs(dss.WGS84).iloc[0]
        return pt.y, pt.x

    return to_lat_lon


@pytest.fixture
def synthetic_parcels(origin: tuple[float, float]) -> gpd.GeoDataFrame:
    """Seven parcels around SITE (offsets in metres; zone radius 150 m).

    id  HCAT        crop     extent (x; y)                 whole ha  in-zone ha
    1   3301010600  maize    -50..50;   -50..50            1.0       1.0
    2   3303050000  olives   100..400;  -50..50            3.0       0.4717 (circle-band integral)
    3   3301280000  tomato   -50..50;   60..100            0.4       0.4
    4   3302000000  pasture  -110..-60; -40..40            0.4       0.4
    5   None        missing  -40..40;   -110..-60          0.4       0.4
    6   3301010100  wheat    149.9..160; -0.5..0.5         0.001     ~0.1 m2 sliver -> not a member (A3)
    7   3301010100  wheat    1000..1100; 1000..1100        1.0       0, no contact
    """
    x, y = origin
    rows = [
        (1, "3301010600", box(x - 50, y - 50, x + 50, y + 50)),
        (2, "3303050000", box(x + 100, y - 50, x + 400, y + 50)),
        (3, "3301280000", box(x - 50, y + 60, x + 50, y + 100)),
        (4, "3302000000", box(x - 110, y - 40, x - 60, y + 40)),
        (5, None, box(x - 40, y - 110, x + 40, y - 60)),
        (6, "3301010100", box(x + 149.9, y - 0.5, x + 160, y + 0.5)),
        (7, "3301010100", box(x + 1000, y + 1000, x + 1100, y + 1100)),
    ]
    ids, codes, geoms = zip(*rows, strict=True)
    return gpd.GeoDataFrame({"OSA_ID": ids, "EC_hcat_c": codes}, geometry=list(geoms), crs=dss.PT_TM06)


@pytest.fixture(scope="session")
def expected() -> SimpleNamespace:
    """Analytic expectations for `synthetic_parcels` at SITE."""
    # Parcel 2 in zone: integral over |y| <= 50 of (sqrt(150^2 - y^2) - 100) dy, in m2 -> ha.
    p2_in_zone = (50 * math.sqrt(150**2 - 50**2) + 150**2 * math.asin(50 / 150) - 100 * 100) / 1e4
    return SimpleNamespace(
        categories={1: "10", 2: "20", 3: "50", 4: "30", 5: dss.FALLBACK},
        area_ha={1: 1.0, 2: 3.0, 3: 0.4, 4: 0.4, 5: 0.4},
        p2_in_zone_ha=p2_in_zone,
        area_in_zone_ha=2.2 + p2_in_zone,
        q_zone_m3=1.0 * 35 + p2_in_zone * 45 + 0.4 * 55 + 0.4 * 15 + 0.4 * 30,
        q_parcel_m3=1.0 * 35 + 3.0 * 45 + 0.4 * 55 + 0.4 * 15 + 0.4 * 30,
    )


@pytest.fixture
def zipped_shapefile(tmp_path: Path) -> Callable[..., Path]:
    """Write a GeoDataFrame as <stem>.zip holding one shapefile, like PT.zip."""

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
        pytest.skip(f"real layer absent at {dss.DEFAULT_DATA_PATH}; run: python run_pipeline.py --download")
    return dss.load_parcels(dss.DEFAULT_DATA_PATH)


# --- NASA POWER -----------------------------------------------------------------------------------
@pytest.fixture(scope="session")
def nasa_bytes() -> Callable[[str], bytes]:
    """Recorded NASA POWER responses at SITE (2026-10-22): 'AG' reports MJ/(m2.day), 'RE' kWh/(m2.day)."""
    return lambda community: (FIXTURES / f"nasa_power_alentejo_{community}.json").read_bytes()


@pytest.fixture
def nasa_payload(nasa_bytes: Callable[[str], bytes]) -> Callable[[str], dict]:
    """Fresh, mutable parse of a recorded response."""
    return lambda community: json.loads(nasa_bytes(community))


class StubSolar:
    """SolarClient double: serves one recorded SolarResource and logs the query points."""

    def __init__(self, resource: dss.SolarResource) -> None:
        self.resource = resource
        self.calls: list[tuple[float, float]] = []

    def solar_resource(self, lat: float, lon: float) -> dss.SolarResource:
        self.calls.append((lat, lon))
        return self.resource


@pytest.fixture
def stub_solar(nasa_bytes: Callable[[str], bytes]) -> StubSolar:
    """Recorded AG climatology at SITE: PSH_avg 4.90, design month DEC 2.11 kWh/(m2.day)."""
    return StubSolar(dss.parse_power_climatology(json.loads(nasa_bytes("AG")), *SITE))


@pytest.fixture(scope="session")
def nasa_online() -> None:
    """Skip live-API tests when NASA POWER cannot be reached."""
    try:
        with urllib.request.urlopen(dss.NASA_POWER_URL, timeout=5):
            pass
    except urllib.error.HTTPError as exc:  # reachable: the bare endpoint answers 4xx
        exc.close()
    except OSError as exc:
        pytest.skip(f"NASA POWER unreachable: {exc}")
