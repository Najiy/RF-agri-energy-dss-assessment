"""Runner wiring on a small made-up archive with a stand-in for NASA: exit codes and what gets printed."""

from __future__ import annotations

import pytest

import dss_pipeline as dss
import run_pipeline

SPEC_MESSAGE = "Location is out of agricultural domain or contains no parcels within the 150m radius."


@pytest.fixture
def archive(synthetic_parcels, zipped_shapefile):
    return str(zipped_shapefile(synthetic_parcels))


def _site_args(archive: str, lat: float, lon: float) -> list[str]:
    return ["--data", archive, "--lat", str(lat), "--lon", str(lon)]


def test_custom_site_prints_all_three_tasks(archive, site, stub_solar, capsys):
    assert run_pipeline.main(_site_args(archive, *site), get_solar=stub_solar) == 0

    out = capsys.readouterr().out
    assert "Task 1 | 5 parcel(s)" in out
    assert "Task 2 | Q_day" in out and "PSH_avg 4.90" in out and "dullest month DEC 2.11" in out
    assert "Tier 1 x 1" in out and "E_excess" in out and "surplus in: MAY, JUN, JUL, AUG" in out


def test_site_without_parcels_exits_1_with_a_hint(archive, stub_solar, capsys):
    assert run_pipeline.main(_site_args(archive, 38.722, -9.139), get_solar=stub_solar) == 1

    out = capsys.readouterr().out
    assert f"EmptyClusterError: {SPEC_MESSAGE}" in out and "nearest parcel" in out
    assert stub_solar.calls == []  # no cluster, so NASA is never asked


def test_nasa_failure_exits_1(archive, site, capsys):
    def offline(lat, lon):
        raise dss.SolarDataError("NASA POWER request failed: URLError('offline')")

    assert run_pipeline.main(_site_args(archive, *site), get_solar=offline) == 1
    assert "NASA POWER error" in capsys.readouterr().err


def test_bad_nasa_timeout_exits_1(archive, site, capsys):
    assert run_pipeline.main([*_site_args(archive, *site), "--nasa-timeout", "0"]) == 1
    assert "timeout_s" in capsys.readouterr().err


def test_missing_dataset_exits_1(tmp_path, stub_solar, capsys):
    assert run_pipeline.main(["--data", str(tmp_path / "PT.zip")], get_solar=stub_solar) == 1
    assert "Dataset error" in capsys.readouterr().err


def test_lat_requires_lon():
    with pytest.raises(SystemExit) as excinfo:
        run_pipeline.main(["--lat", "38.0"])

    assert excinfo.value.code == 2
