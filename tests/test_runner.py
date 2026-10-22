"""Runner CLI wiring on a synthetic archive with a stubbed solar client: exit codes and report content."""

from __future__ import annotations

import pytest

import dss_pipeline as dss
import run_pipeline

SPEC_MESSAGE = "Location is out of agricultural domain or contains no parcels within the 150m radius."


class Offline:
    def solar_resource(self, lat: float, lon: float) -> dss.SolarResource:
        raise dss.SolarDataError("NASA POWER unreachable after 3 attempt(s): URLError('offline')")


@pytest.fixture
def archive(synthetic_parcels, zipped_shapefile):
    return str(zipped_shapefile(synthetic_parcels))


def _site_args(archive: str, lat: float, lon: float) -> list[str]:
    return ["--data", archive, "--lat", str(lat), "--lon", str(lon)]


def test_custom_site_reports_tasks_1_to_3(archive, site, stub_solar, capsys):
    assert run_pipeline.main(_site_args(archive, *site), solar_client=stub_solar) == 0
    out = capsys.readouterr().out
    assert "Task 1 | 5 parcel(s)" in out
    assert "Task 2 | Q_day" in out and "PSH_avg 4.90" in out and "design month DEC 2.11" in out
    assert "Tier 1 x 1" in out and "E_excess" in out and "surplus in: MAY, JUN, JUL, AUG" in out


def test_custom_site_without_parcels_exits_2(archive, stub_solar, capsys):
    assert run_pipeline.main(_site_args(archive, 38.722, -9.139), solar_client=stub_solar) == 2
    out = capsys.readouterr().out
    assert f"EmptyClusterError: {SPEC_MESSAGE}" in out and "nearest parcel" in out
    assert stub_solar.calls == []  # no cluster, no NASA request


def test_nasa_outage_exits_3(archive, site, capsys):
    assert run_pipeline.main(_site_args(archive, *site), solar_client=Offline()) == 3
    assert "SolarDataError: NASA POWER unreachable" in capsys.readouterr().out


def test_invalid_nasa_timeout_exits_2(archive, capsys):
    assert run_pipeline.main(["--data", archive, "--nasa-timeout", "0"]) == 2
    assert "timeout_s" in capsys.readouterr().err


def test_missing_dataset_exits_1(tmp_path, stub_solar, capsys):
    assert run_pipeline.main(["--data", str(tmp_path / "PT.zip")], solar_client=stub_solar) == 1
    assert "Dataset error" in capsys.readouterr().err


def test_lat_requires_lon():
    with pytest.raises(SystemExit) as excinfo:
        run_pipeline.main(["--lat", "38.0"])
    assert excinfo.value.code == 2
