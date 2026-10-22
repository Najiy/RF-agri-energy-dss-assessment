"""Loading and downloading the parcel file."""

from __future__ import annotations

import urllib.error

import pandas as pd
import pytest

import dss_pipeline as dss


def test_degrees_archive_is_reprojected_to_metres(synthetic_parcels, zipped_shapefile):
    archive = zipped_shapefile(synthetic_parcels.to_crs(dss.WGS84))  # same layout as the real PT.zip

    loaded = dss.load_parcels(archive)

    assert loaded.crs.to_epsg() == 3763
    assert len(loaded) == len(synthetic_parcels)
    assert loaded.area.to_numpy() == pytest.approx(synthetic_parcels.area.to_numpy(), rel=1e-6)
    assert pd.api.types.is_integer_dtype(loaded["OSA_ID"])
    assert pd.isna(loaded.loc[4, "EC_hcat_c"])


def test_missing_file(tmp_path):
    with pytest.raises(dss.DatasetError, match="not found"):
        dss.load_parcels(tmp_path / "PT.zip")


def test_corrupt_archive(tmp_path):
    bogus = tmp_path / "PT.zip"
    bogus.write_text("not a zip")

    with pytest.raises(dss.DatasetError, match="Cannot read"):
        dss.load_parcels(bogus)


def test_missing_crop_code_column(synthetic_parcels, zipped_shapefile):
    with pytest.raises(dss.DatasetError, match="EC_hcat_c"):
        dss.load_parcels(zipped_shapefile(synthetic_parcels.drop(columns="EC_hcat_c")))


def test_download_once_then_reuse(tmp_path, synthetic_parcels, zipped_shapefile, monkeypatch):
    source = zipped_shapefile(synthetic_parcels)
    target = tmp_path / "data" / "PT.zip"

    assert dss.ensure_dataset(target, url=source.as_uri()) == target
    assert target.is_file() and not target.with_name("PT.zip.part").exists()

    # Second call must not touch the network.
    monkeypatch.setattr(dss.urllib.request, "urlopen", lambda *a, **k: pytest.fail("unexpected download"))
    assert dss.ensure_dataset(target, url="https://example.invalid/PT.zip") == target


def test_failed_download_raises_and_leaves_no_file(tmp_path, monkeypatch):
    def offline(*_args, **_kwargs):
        raise urllib.error.URLError("network unreachable")

    monkeypatch.setattr(dss.urllib.request, "urlopen", offline)
    target = tmp_path / "PT.zip"

    with pytest.raises(dss.DatasetError, match="network unreachable"):
        dss.ensure_dataset(target)

    assert not target.exists() and not target.with_name("PT.zip.part").exists()
