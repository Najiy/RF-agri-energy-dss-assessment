"""The real EuroCrops PT file: the spec's check locations and the data findings behind A1, A2 and A9.

Skipped when data/PT.zip is absent (fetch it with: python run_pipeline.py --download).
"""

from __future__ import annotations

import time

import geopandas as gpd
import pytest

import dss_pipeline as dss
import run_pipeline

pytestmark = pytest.mark.integration

SPEC_MESSAGE = "Location is out of agricultural domain or contains no parcels within the 150m radius."


def test_file_loads_in_metres(real_parcels):
    assert len(real_parcels) == 100_000
    assert real_parcels.crs.to_epsg() == 3763
    assert real_parcels["OSA_ID"].is_unique
    assert str(real_parcels["OSA_ID"].dtype) == "Int64"


def test_metre_areas_match_the_areas_declared_in_the_file(real_parcels):
    """Sanity check on the reprojection: our areas should reproduce the file's own OSA_AREA (m2) column."""
    declared = gpd.read_file(f"zip://{dss.DEFAULT_DATA_PATH.as_posix()}", columns=["OSA_AREA"]).to_crs(dss.PT_TM06)

    assert (declared.area / declared["OSA_AREA"] - 1).abs().median() < 1e-4


def test_all_crop_codes_start_with_33(real_parcels):
    """A1: the spec's two-digit rule would put every parcel in the fallback."""
    assert real_parcels["EC_hcat_c"].str[:2].eq("33").all()

    shares = dss.classify_parcels(real_parcels)["crop_category"].value_counts(normalize=True)

    assert set(shares.index) == {"10", "20", "30", "50", dss.FALLBACK}
    assert shares[dss.FALLBACK] < 0.5


def test_central_lisbon_raises_empty_cluster(real_parcels):
    with pytest.raises(dss.EmptyClusterError) as excinfo:
        dss.extract_cluster(real_parcels, 38.722, -9.139)

    assert str(excinfo.value) == SPEC_MESSAGE


def test_spec_alentejo_point_has_no_parcels(real_parcels):
    """A9: nothing within 150 m of (38.015, -7.865) in this file; the nearest parcel is about 1.33 km away."""
    with pytest.raises(dss.EmptyClusterError):
        dss.extract_cluster(real_parcels, 38.015, -7.865)

    assert dss.nearest_parcel(real_parcels, 38.015, -7.865).distance_m == pytest.approx(1328, abs=1)


def test_resited_alentejo_cluster(real_parcels):
    hint = dss.nearest_parcel(real_parcels, 38.015, -7.865)

    cluster = dss.extract_cluster(real_parcels, hint.lat, hint.lon)

    assert cluster.parcels["OSA_ID"].tolist() == [506762]
    assert cluster.parcels["crop_category"].tolist() == ["10"]  # common soft wheat
    assert cluster.area_in_zone_ha == pytest.approx(1.937, abs=1e-3)


def test_mixed_cluster_uses_area_inside_the_circle(real_parcels):
    """A2: two 20 ha olive blocks only clip the circle; whole-parcel area would be about 8x too large."""
    cluster = dss.extract_cluster(real_parcels, *run_pipeline.ALENTEJO_MIXED)

    assert set(cluster.parcels["OSA_ID"]) == {514985, 515000, 515002, 515003}
    assert set(cluster.parcels["crop_category"]) == {"20", dss.FALLBACK}
    assert cluster.area_in_zone_ha == pytest.approx(5.655, abs=1e-3)
    assert cluster.parcel_area_ha == pytest.approx(46.886, abs=1e-3)
    assert cluster.area_in_zone_ha <= cluster.zone_ha


def test_cluster_lookup_is_fast(real_parcels):
    _ = real_parcels.sindex  # the index is built once per layer, outside the timed loop

    start = time.perf_counter()
    for _ in range(20):
        dss.extract_cluster(real_parcels, *run_pipeline.ALENTEJO_MIXED)

    assert (time.perf_counter() - start) / 20 < 0.25  # the spec asks for "under a few seconds"


def test_runner_verification_suite(real_parcels, stub_solar, capsys):
    assert run_pipeline.main([], get_solar=stub_solar) == 0

    out = capsys.readouterr().out
    assert out.count(f"EmptyClusterError: {SPEC_MESSAGE}") == 2  # [1] spec Alentejo point and [3] Lisbon
    assert "[1b] Alentejo, re-sited to nearest parcel" in out
    assert "Tier 1 x 1" in out and "Tier 3 x 1" in out
    assert len(stub_solar.calls) == 2  # one NASA question per non-empty cluster: [1b] and [2]
