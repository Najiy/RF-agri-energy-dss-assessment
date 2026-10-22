"""Task 1: metric command zone, indexed query, areas, membership, HCAT classification, defensive errors."""

from __future__ import annotations

import math
import pickle

import geopandas as gpd
import numpy as np
import pandas as pd
import pytest
from shapely.geometry import Polygon

import dss_pipeline as dss

SPEC_MESSAGE = "Location is out of agricultural domain or contains no parcels within the 150m radius."


# --- Command zone ---------------------------------------------------------------------------------
def test_command_zone_is_a_150_m_circle_in_metres(site, origin):
    zone = dss.command_zone(*site)
    assert zone.area == pytest.approx(math.pi * 150**2, rel=2e-4)  # 256-gon vs true circle
    assert zone.area / 1e4 == pytest.approx(7.07, abs=0.01)  # spec: ~7 ha
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
        (True, -8, TypeError),
    ],
)
def test_malformed_coordinates_raise(synthetic_parcels, lat, lon, error):
    with pytest.raises(error):
        dss.extract_cluster(synthetic_parcels, lat, lon)


@pytest.mark.parametrize("radius", [0, -150, math.nan])
def test_invalid_radius_raises(site, radius):
    with pytest.raises(ValueError):
        dss.command_zone(*site, radius_m=radius)


# --- EmptyClusterError ----------------------------------------------------------------------------
@pytest.mark.parametrize(
    "lat, lon",
    [
        (51.5074, -0.1278),  # London
        (0.0, 0.0),  # Gulf of Guinea
        (-33.87, 151.21),  # Sydney: TM06 yields inf this far from its meridian
        (-39.67, 171.87),  # antipode of Portugal
        (40.4168, -3.7038),  # Madrid: near, yet outside the TM06 domain
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


def test_message_tracks_custom_radius(synthetic_parcels, site_offset):
    with pytest.raises(dss.EmptyClusterError, match=r"within the 75m radius\.$"):
        dss.extract_cluster(synthetic_parcels, *site_offset(5000, -5000), radius_m=75)


def test_empty_cluster_error_pickles():
    """Monte Carlo workers ship exceptions across processes."""
    clone = pickle.loads(pickle.dumps(dss.EmptyClusterError.for_radius(150)))
    assert type(clone) is dss.EmptyClusterError and str(clone) == SPEC_MESSAGE
    assert issubclass(dss.EmptyClusterError, Exception)


# --- Cluster content ------------------------------------------------------------------------------
def test_members_and_categories(synthetic_parcels, site, expected):
    parcels = dss.extract_cluster(synthetic_parcels, *site).parcels
    assert dict(zip(parcels["OSA_ID"], parcels["crop_category"], strict=True)) == expected.categories


def test_whole_parcel_vs_in_zone_areas(synthetic_parcels, site, expected):
    parcels = dss.extract_cluster(synthetic_parcels, *site).parcels.set_index("OSA_ID")
    assert parcels["area_ha"].to_dict() == pytest.approx(expected.area_ha)
    assert parcels.loc[2, "area_in_zone_ha"] == pytest.approx(expected.p2_in_zone_ha, rel=1e-3)
    assert parcels.loc[[1, 3, 4, 5], "area_in_zone_ha"].tolist() == pytest.approx([1.0, 0.4, 0.4, 0.4])


def test_boundary_sliver_is_candidate_not_member(synthetic_parcels, site):
    zone = dss.command_zone(*site)
    candidates = synthetic_parcels.iloc[synthetic_parcels.sindex.query(zone, predicate="intersects")]
    assert 6 in set(candidates["OSA_ID"])
    assert 6 not in set(dss.extract_cluster(synthetic_parcels, *site).parcels["OSA_ID"])


def test_cluster_aggregates(synthetic_parcels, site, expected):
    cluster = dss.extract_cluster(synthetic_parcels, *site)
    assert cluster.n_parcels == 5
    assert cluster.parcel_area_ha == pytest.approx(5.2)
    assert cluster.area_in_zone_ha == pytest.approx(expected.area_in_zone_ha, rel=1e-4)
    assert cluster.zone_ha == pytest.approx(math.pi * 150**2 / 1e4, rel=2e-4)
    assert cluster.centroid_lat_lon() == pytest.approx(site, abs=1e-3)  # footprint is near-centred


def test_invalid_geometry_is_repaired(origin, site):
    x, y = origin
    bowtie = Polygon([(x - 10, y - 10), (x + 10, y + 10), (x + 10, y - 10), (x - 10, y + 10)])
    layer = gpd.GeoDataFrame({"OSA_ID": [1], "EC_hcat_c": ["3301010600"]}, geometry=[bowtie], crs=dss.PT_TM06)
    assert not layer.is_valid.iloc[0]
    parcels = dss.extract_cluster(layer, *site).parcels
    assert parcels["area_ha"].iloc[0] == pytest.approx(200 / 1e4)  # two 100 m2 lobes, not the signed 0


@pytest.mark.parametrize(
    "mutate, error",
    [
        (lambda g: g.to_crs(dss.WGS84), ValueError),  # degrees: areas would be meaningless
        (lambda g: g.drop(columns="EC_hcat_c"), ValueError),
        (lambda g: pd.concat([g, g]), ValueError),  # duplicate row labels
        (lambda g: pd.DataFrame(g), TypeError),
    ],
)
def test_layer_contract(synthetic_parcels, site, mutate, error):
    with pytest.raises(error):
        dss.extract_cluster(mutate(synthetic_parcels), *site)


# --- Nearest parcel -------------------------------------------------------------------------------
def test_nearest_parcel_matches_brute_force(synthetic_parcels, site_offset):
    lat, lon = site_offset(2500, 300)
    hint = dss.nearest_parcel(synthetic_parcels, lat, lon)
    point = gpd.GeoSeries.from_xy([lon], [lat], crs=dss.WGS84).to_crs(dss.PT_TM06).iloc[0]
    distances = synthetic_parcels.distance(point).to_numpy()
    assert hint.row == int(distances.argmin())
    assert hint.distance_m == pytest.approx(distances.min())
    # The suggested re-site point lies inside that parcel, so a cluster exists there.
    assert hint.parcel_id in set(dss.extract_cluster(synthetic_parcels, hint.lat, hint.lon).parcels["OSA_ID"])


def test_nearest_parcel_rejects_points_outside_domain(synthetic_parcels):
    with pytest.raises(ValueError, match="domain"):
        dss.nearest_parcel(synthetic_parcels, 51.5, -0.12)


# --- HCAT classification (A1) ---------------------------------------------------------------------
@pytest.mark.parametrize(
    "code, expected",
    [
        # spec's literal two-digit scheme
        ("1000000000", "10"),
        ("2000000000", "20"),
        ("3000000000", "30"),
        ("5000000000", "50"),
        # EuroCrops HCAT3 codes as found in PT.zip (root '33')
        ("3301010600", "10"),  # grain maize
        ("3301010100", "10"),  # common soft wheat
        ("3303050000", "20"),  # olive plantations
        ("3303040000", "20"),  # citrus
        ("3303060000", "20"),  # vineyards
        ("3302000000", "30"),  # pasture, meadow
        ("3301090100", "30"),  # temporary grass
        ("3301280000", "50"),  # tomato
        ("3301070000", "50"),  # fresh vegetables
        ("3301030000", "50"),  # potatoes
        ("3303080700", dss.FALLBACK),  # PT shrubland: longer prefix overrides '3303'
        ("3301020100", dss.FALLBACK),  # beans: pulses are the fallback archetype
        ("3301110000", dss.FALLBACK),  # fallow
        ("3306060000", dss.FALLBACK),  # oak / montado
        ("3399000000", dss.FALLBACK),  # not known
        # input hygiene
        (3301010600, "10"),
        (3301010600.0, "10"),
        (np.int64(3301010600), "10"),
        (" 3302000000 ", "30"),
        (None, dss.FALLBACK),
        (math.nan, dss.FALLBACK),
        (pd.NA, dss.FALLBACK),
        ("", dss.FALLBACK),
        ("33abc", dss.FALLBACK),
        (True, dss.FALLBACK),
        (3301010600.5, dss.FALLBACK),
    ],
)
def test_hcat_category(code, expected):
    assert dss.hcat_category(code) == expected


def test_classify_parcels_resolves_unique_codes_only(monkeypatch):
    calls = []
    original = dss.hcat_category
    monkeypatch.setattr(dss, "hcat_category", lambda c: calls.append(c) or original(c))
    frame = pd.DataFrame({"EC_hcat_c": ["3301010600"] * 1000 + ["3303050000"] * 1000 + [None] * 10})
    out = dss.classify_parcels(frame)
    assert len(calls) == 2
    assert out["crop_category"].value_counts().to_dict() == {"10": 1000, "20": 1000, dss.FALLBACK: 10}
    assert out["water_demand_m3_ha_day"].iloc[[0, 1000, 2000]].tolist() == [35.0, 45.0, 30.0]
