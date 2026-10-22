"""Task 1 ETL: archive handling, schema checks, CRS normalisation, resilient download."""

from __future__ import annotations

import logging
import urllib.error
import zipfile

import geopandas as gpd
import pandas as pd
import pytest

import dss_pipeline as dss


def _strip_crs(gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    return gpd.GeoDataFrame(gdf.drop(columns="geometry"), geometry=list(gdf.geometry))


def test_wgs84_archive_is_reprojected_to_tm06(synthetic_parcels, zipped_shapefile):
    loaded = dss.load_parcels(zipped_shapefile(synthetic_parcels.to_crs(dss.WGS84)))
    assert loaded.crs.to_epsg() == 3763
    assert len(loaded) == len(synthetic_parcels)
    assert loaded.area.to_numpy() == pytest.approx(synthetic_parcels.area.to_numpy(), rel=1e-6)
    assert pd.api.types.is_integer_dtype(loaded["OSA_ID"])
    assert pd.isna(loaded.loc[4, "EC_hcat_c"])


@pytest.mark.filterwarnings("ignore:'crs' was not provided")  # the CRS-less write is the point
@pytest.mark.parametrize("crs", [dss.WGS84, dss.PT_TM06])
def test_missing_crs_is_inferred(synthetic_parcels, zipped_shapefile, caplog, crs):
    archive = zipped_shapefile(_strip_crs(synthetic_parcels.to_crs(crs)))
    with caplog.at_level(logging.WARNING, logger="dss_pipeline"):
        loaded = dss.load_parcels(archive)
    assert "no CRS" in caplog.text and crs in caplog.text
    assert loaded.area.to_numpy() == pytest.approx(synthetic_parcels.area.to_numpy(), rel=1e-6)


def test_null_geometries_are_dropped(synthetic_parcels, zipped_shapefile, caplog):
    with_null = synthetic_parcels.copy()
    with_null.loc[0, "geometry"] = None
    with caplog.at_level(logging.WARNING, logger="dss_pipeline"):
        loaded = dss.load_parcels(zipped_shapefile(with_null))
    assert len(loaded) == len(synthetic_parcels) - 1
    assert "Dropped 1" in caplog.text


def test_missing_file(tmp_path):
    with pytest.raises(dss.DatasetError, match="not found"):
        dss.load_parcels(tmp_path / "PT.zip")


def test_corrupt_archive(tmp_path):
    bogus = tmp_path / "PT.zip"
    bogus.write_text("not a zip")
    with pytest.raises(dss.DatasetError, match="not a valid ZIP"):
        dss.load_parcels(bogus)


def test_archive_without_shapefile(tmp_path):
    archive = tmp_path / "PT.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("readme.txt", "no layer here")
    with pytest.raises(dss.DatasetError, match=r"exactly one \.shp"):
        dss.load_parcels(archive)


def test_missing_hcat_column(synthetic_parcels, zipped_shapefile):
    with pytest.raises(dss.DatasetError, match="EC_hcat_c"):
        dss.load_parcels(zipped_shapefile(synthetic_parcels.drop(columns="EC_hcat_c")))


def test_download_once_then_reuse(tmp_path, synthetic_parcels, zipped_shapefile, monkeypatch):
    source = zipped_shapefile(synthetic_parcels)
    target = tmp_path / "data" / "PT.zip"
    assert dss.ensure_dataset(target, url=source.as_uri()) == target
    assert zipfile.is_zipfile(target) and not target.with_name("PT.zip.part").exists()
    monkeypatch.setattr(dss.urllib.request, "urlopen", lambda *a, **k: pytest.fail("unexpected download"))
    assert dss.ensure_dataset(target, url="https://example.invalid/PT.zip") == target


def test_download_rejects_non_zip_payload(tmp_path):
    page = tmp_path / "rate_limited.html"
    page.write_text("<html>429</html>")
    target = tmp_path / "data" / "PT.zip"
    with pytest.raises(dss.DatasetError, match="ZIP"):
        dss.ensure_dataset(target, url=page.as_uri())
    assert not target.exists() and not target.with_name("PT.zip.part").exists()


def test_download_wraps_network_errors(tmp_path, monkeypatch):
    def offline(*_args, **_kwargs):
        raise urllib.error.URLError("network unreachable")

    monkeypatch.setattr(dss.urllib.request, "urlopen", offline)
    with pytest.raises(dss.DatasetError, match="network unreachable"):
        dss.ensure_dataset(tmp_path / "PT.zip")
