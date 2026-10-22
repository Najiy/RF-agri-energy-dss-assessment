"""Task 1: metric command zone, spatial query, areas, crop categories, EmptyClusterError."""

from __future__ import annotations

import math

import geopandas as gpd
import pandas as pd
import pytest
from shapely.geometry import Polygon

import dss_pipeline as dss

SPEC_MESSAGE = "Location is out of agricultural domain or contains no parcels within the 150m radius."


# --- Command zone ---------------------------------------------------------------------------------

def test_command_zone_is_a_150_m_circle_in_metres(site, origin):
    zone = dss.command_zone(*site)

    assert zone.area == pytest.approx(math.pi * 150**2, rel=2e-4)
    assert zone.area / 1e4 == pytest.approx(7.07, abs=0.01)  # the spec's "about 7 hectares"

    minx, miny, maxx, maxy = zone.bounds
    assert (maxx - minx, maxy - miny) == pytest.approx((300, 300), abs=0.01)
    assert (zone.centroid.x, zone.centroid.y) == pytest.approx(origin, abs=1e-6)


@pytest.mark.parametrize(
    "lat, lon, error",
    [
        (91, -8, ValueError),
        (38, 181, ValueError),
        (math.nan, -8, ValueError),
        (38, math.inf, ValueError),
        ("38.0", -8, TypeError),
        (None, -8, TypeError),
    ],
)
def test_bad_coordinates_raise(synthetic_parcels, lat, lon, error):
    with pytest.raises(error):
        dss.extract_cluster(synthetic_parcels, lat, lon)


@pytest.mark.parametrize("radius", [0, -150, math.nan])
def test_bad_radius_raises(site, radius):
    with pytest.raises(ValueError):
        dss.command_zone(*site, radius_m=radius)


# --- EmptyClusterError ----------------------------------------------------------------------------

@pytest.mark.parametrize(
    "lat, lon",
    [
        (51.5074, -0.1278),  # London
        (0.0, 0.0),  # sea off West Africa
        (-33.87, 151.21),  # Sydney
        (40.4168, -3.7038),  # Madrid: close to Portugal, still outside
        (-7.5, -100.0),  # a spot the Portugal projection cannot place on its map (it returns infinity)
    ],
)
def test_outside_portugal_raises_empty_cluster(synthetic_parcels, lat, lon):
    with pytest.raises(dss.EmptyClusterError) as excinfo:
        dss.extract_cluster(synthetic_parcels, lat, lon)

    assert str(excinfo.value) == SPEC_MESSAGE


def test_empty_zone_inside_portugal_raises_spec_message(synthetic_parcels, site_offset):
    with pytest.raises(dss.EmptyClusterError) as excinfo:
        dss.extract_cluster(synthetic_parcels, *site_offset(5000, -5000))

    assert str(excinfo.value) == SPEC_MESSAGE


def test_message_follows_a_custom_radius(synthetic_parcels, site_offset):
    with pytest.raises(dss.EmptyClusterError, match=r"within the 75m radius\.$"):
        dss.extract_cluster(synthetic_parcels, *site_offset(5000, -5000), radius_m=75)


# --- Cluster content ------------------------------------------------------------------------------

def test_members_and_categories(synthetic_parcels, site, expected):
    parcels = dss.extract_cluster(synthetic_parcels, *site).parcels

    assert dict(zip(parcels["OSA_ID"], parcels["crop_category"], strict=True)) == expected.categories


def test_whole_parcel_vs_in_zone_areas(synthetic_parcels, site, expected):
    parcels = dss.extract_cluster(synthetic_parcels, *site).parcels.set_index("OSA_ID")

    assert parcels["area_ha"].to_dict() == pytest.approx(expected.area_ha)
    assert parcels.loc[2, "area_in_zone_ha"] == pytest.approx(expected.p2_in_zone_ha, rel=1e-3)  # clipped by circle
    assert parcels.loc[[1, 3, 4, 5], "area_in_zone_ha"].tolist() == pytest.approx([1.0, 0.4, 0.4, 0.4])


def test_parcels_outside_the_circle_are_not_members(synthetic_parcels, site):
    members = set(dss.extract_cluster(synthetic_parcels, *site).parcels["OSA_ID"])

    assert 6 not in members  # 1 m beyond the circle
    assert 7 not in members  # far away


def test_cluster_totals(synthetic_parcels, site, expected):
    cluster = dss.extract_cluster(synthetic_parcels, *site)

    assert cluster.n_parcels == 5
    assert cluster.parcel_area_ha == pytest.approx(5.2)
    assert cluster.area_in_zone_ha == pytest.approx(expected.area_in_zone_ha, rel=1e-4)
    assert cluster.zone_ha == pytest.approx(math.pi * 150**2 / 1e4, rel=2e-4)
    assert cluster.centroid_lat_lon() == pytest.approx(site, abs=1e-3)  # farmland is spread around the point


def test_self_crossing_polygon_is_repaired(origin, site):
    x, y = origin
    bowtie = Polygon([(x - 10, y - 10), (x + 10, y + 10), (x + 10, y - 10), (x - 10, y + 10)])
    layer = gpd.GeoDataFrame({"OSA_ID": [1], "EC_hcat_c": ["3301010600"]}, geometry=[bowtie], crs=dss.PT_TM06)
    assert not layer.is_valid.iloc[0]

    parcels = dss.extract_cluster(layer, *site).parcels

    assert parcels["area_ha"].iloc[0] == pytest.approx(200 / 1e4)  # two 100 m2 triangles


def test_layer_in_degrees_is_refused(synthetic_parcels, site):
    with pytest.raises(ValueError, match="EPSG:3763"):
        dss.extract_cluster(synthetic_parcels.to_crs(dss.WGS84), *site)


# --- Nearest parcel -------------------------------------------------------------------------------

def test_nearest_parcel_matches_brute_force(synthetic_parcels, site_offset):
    lat, lon = site_offset(2500, 300)

    hint = dss.nearest_parcel(synthetic_parcels, lat, lon)

    point = gpd.GeoSeries.from_xy([lon], [lat], crs=dss.WGS84).to_crs(dss.PT_TM06).iloc[0]
    distances = synthetic_parcels.distance(point)
    assert hint.parcel_id == synthetic_parcels["OSA_ID"][distances.idxmin()]
    assert hint.distance_m == pytest.approx(distances.min())

    # The suggested point lies inside that parcel, so a cluster exists there.
    assert hint.parcel_id in set(dss.extract_cluster(synthetic_parcels, hint.lat, hint.lon).parcels["OSA_ID"])


def test_nearest_parcel_gives_none_for_unprojectable_point(synthetic_parcels):
    assert dss.nearest_parcel(synthetic_parcels, -7.5, -100.0) is None


# --- Crop categories (A1) -------------------------------------------------------------------------

@pytest.mark.parametrize(
    "code, expected",
    [
        # the spec's literal two-digit scheme
        ("1000000000", "10"),
        ("2000000000", "20"),
        ("3000000000", "30"),
        ("5000000000", "50"),
        # real codes as found in PT.zip (all start with 33)
        ("3301010600", "10"),  # grain maize
        ("3301010100", "10"),  # wheat
        ("3303050000", "20"),  # olives
        ("3303040000", "20"),  # citrus
        ("3303060000", "20"),  # vineyards
        ("3302000000", "30"),  # pasture
        ("3301090100", "30"),  # temporary grass
        ("3301280000", "50"),  # tomato
        ("3301070000", "50"),  # fresh vegetables
        ("3301030000", "50"),  # potatoes
        ("3303080700", dss.FALLBACK),  # shrubland: the longer "330308" rule beats "3303"
        ("3301020100", dss.FALLBACK),  # beans
        ("3301110000", dss.FALLBACK),  # fallow
        ("3306060000", dss.FALLBACK),  # oak woodland
        ("3399000000", dss.FALLBACK),  # not known
        # odd input
        (" 3302000000 ", "30"),
        (None, dss.FALLBACK),
        (math.nan, dss.FALLBACK),
        ("", dss.FALLBACK),
    ],
)
def test_hcat_category(code, expected):
    assert dss.hcat_category(code) == expected


def test_classify_parcels_adds_name_and_water_demand():
    frame = pd.DataFrame({"EC_hcat_c": ["3301010600", "3303050000", None]})

    out = dss.classify_parcels(frame)

    assert out["crop_category"].tolist() == ["10", "20", dss.FALLBACK]
    assert out["crop_category_name"].tolist() == ["Cereals & Grains", "Orchards & Fruits", "Fallback Field Crop"]
    assert out["water_demand_m3_ha_day"].tolist() == [35.0, 45.0, 30.0]
