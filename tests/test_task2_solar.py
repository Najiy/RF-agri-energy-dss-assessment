"""Task 2.2: NASA POWER parsing, unit conversion, request handling, cache; and Task 2 -> Task 3 wiring."""

from __future__ import annotations

import io
import urllib.error
import urllib.parse

import pytest

import dss_pipeline as dss

ALENTEJO = (38.015, -7.865)


class ScriptedGet:
    """Stands in for the HTTP call: plays back the given outcomes in order (bytes are returned, errors raised)."""

    def __init__(self, *outcomes: bytes | BaseException) -> None:
        self.outcomes = list(outcomes)
        self.urls: list[str] = []

    def __call__(self, url: str, timeout_s: float) -> bytes:
        self.urls.append(url)
        outcome = self.outcomes.pop(0)  # asking more often than scripted fails the test loudly

        if isinstance(outcome, BaseException):
            raise outcome

        return outcome


# --- Reading NASA's reply, and units (A6) ---------------------------------------------------------

def test_ag_reply_is_converted_from_megajoules(nasa_payload):
    solar = dss.parse_power_climatology(nasa_payload("AG"), *ALENTEJO)

    assert solar.psh_avg == pytest.approx(17.65 / 3.6)
    assert solar.monthly_psh["JUL"] == pytest.approx(28.44 / 3.6)
    assert (solar.design_month, solar.design_psh) == ("DEC", pytest.approx(7.58 / 3.6))


def test_ag_and_re_communities_agree(nasa_payload):
    ag = dss.parse_power_climatology(nasa_payload("AG"), *ALENTEJO)
    re_ = dss.parse_power_climatology(nasa_payload("RE"), *ALENTEJO)

    assert re_.psh_avg == pytest.approx(4.9039)  # RE already reports kWh/m2/day
    assert ag.psh_avg == pytest.approx(re_.psh_avg, rel=2e-3)  # AG rounds to 2 decimals in MJ


@pytest.mark.parametrize(
    "damage, match",
    [
        (lambda p: p.pop("properties"), "lacks"),
        (lambda p: p["parameters"]["ALLSKY_SFC_SW_DWN"].pop("units"), "lacks"),
        (lambda p: p["parameters"]["ALLSKY_SFC_SW_DWN"].update(units="W/m^2"), "unexpected units"),
        (lambda p: p["properties"]["parameter"]["ALLSKY_SFC_SW_DWN"].update(DEC=-999.0), "missing values"),
        (lambda p: p["properties"]["parameter"]["ALLSKY_SFC_SW_DWN"].pop("JUL"), "lacks"),
        (lambda p: p["properties"]["parameter"]["ALLSKY_SFC_SW_DWN"].update(ANN="n/a"), "lacks"),
        (lambda p: p["properties"]["parameter"].update(ALLSKY_SFC_SW_DWN=[1, 2, 3]), "lacks"),
    ],
)
def test_unusable_replies_raise(nasa_payload, damage, match):
    payload = nasa_payload("AG")
    damage(payload)

    with pytest.raises(dss.SolarDataError, match=match):
        dss.parse_power_climatology(payload, *ALENTEJO)


@pytest.mark.parametrize("payload", [None, [], "oops", {"header": "failed", "messages": ["bad request"]}])
def test_replies_without_data_raise(payload):
    with pytest.raises(dss.SolarDataError):
        dss.parse_power_climatology(payload, *ALENTEJO)


# --- Making the request ---------------------------------------------------------------------------

def test_request_follows_the_spec_url(nasa_bytes):
    http_get = ScriptedGet(nasa_bytes("AG"))

    solar = dss.fetch_solar_resource(*ALENTEJO, http_get=http_get)

    url = http_get.urls[0]
    assert url.startswith(dss.NASA_POWER_URL + "?")
    assert urllib.parse.parse_qs(urllib.parse.urlsplit(url).query) == {
        "parameters": ["ALLSKY_SFC_SW_DWN"],
        "community": ["AG"],
        "longitude": ["-7.87"],
        "latitude": ["38.02"],  # 38.015 rounded to 0.01 degree (A8)
        "format": ["JSON"],
    }
    assert (solar.lat, solar.lon) == (38.02, -7.87)


def test_cache_serves_repeat_queries_without_network(nasa_bytes):
    http_get = ScriptedGet(nasa_bytes("AG"))

    first = dss.fetch_solar_resource(38.0112, -7.8611, http_get=http_get)
    again = dss.fetch_solar_resource(38.0138, -7.8641, http_get=http_get)  # same 0.01-degree cell

    assert again is first
    assert len(http_get.urls) == 1


def test_different_cells_are_fetched_separately(nasa_bytes):
    http_get = ScriptedGet(nasa_bytes("AG"), nasa_bytes("AG"))

    dss.fetch_solar_resource(38.01, -7.86, http_get=http_get)
    dss.fetch_solar_resource(38.50, -7.86, http_get=http_get)

    assert len(http_get.urls) == 2


@pytest.mark.parametrize(
    "failure",
    [
        urllib.error.URLError("getaddrinfo failed"),  # no network
        TimeoutError("timed out"),
        ConnectionResetError("reset by peer"),
        urllib.error.HTTPError(dss.NASA_POWER_URL, 503, "unavailable", None, io.BytesIO(b"")),
    ],
)
def test_network_trouble_becomes_solar_data_error(failure):
    with pytest.raises(dss.SolarDataError, match="request failed"):
        dss.fetch_solar_resource(*ALENTEJO, http_get=ScriptedGet(failure))


def test_reply_that_is_not_json_raises():
    http_get = ScriptedGet(b"<html>scheduled maintenance</html>")

    with pytest.raises(dss.SolarDataError, match="not valid JSON"):
        dss.fetch_solar_resource(*ALENTEJO, http_get=http_get)


def test_failures_are_not_cached(nasa_bytes):
    http_get = ScriptedGet(urllib.error.URLError("offline"), nasa_bytes("AG"))

    with pytest.raises(dss.SolarDataError):
        dss.fetch_solar_resource(*ALENTEJO, http_get=http_get)

    assert dss.fetch_solar_resource(*ALENTEJO, http_get=http_get).psh_avg == pytest.approx(17.65 / 3.6)


@pytest.mark.parametrize("lat, lon, timeout", [(95, 0, 10), (38, 200, 10), (38, -7.8, 0), (38, -7.8, -1)])
def test_bad_arguments_never_reach_the_network(lat, lon, timeout):
    http_get = ScriptedGet()

    with pytest.raises(ValueError):
        dss.fetch_solar_resource(lat, lon, timeout, http_get=http_get)

    assert http_get.urls == []


# --- Task 2 feeds Task 3 --------------------------------------------------------------------------

def test_evaluate_site_asks_nasa_about_the_cluster_centre(synthetic_parcels, site, stub_solar):
    a = dss.evaluate_site(synthetic_parcels, *site, get_solar=stub_solar)

    assert stub_solar.calls == [pytest.approx(a.cluster.centroid_lat_lon())]
    assert a.solar is stub_solar.resource


def test_task3_uses_task2_outputs(synthetic_parcels, site, stub_solar):
    a = dss.evaluate_site(synthetic_parcels, *site, get_solar=stub_solar)
    energy, pump = a.energy, a.energy.pump

    assert a.hydraulic_power_kw == pytest.approx(dss.hydraulic_power_kw(a.q_day_m3))  # 2.3 from 2.1
    assert (pump.required_power_kw, pump.q_day_m3) == (a.hydraulic_power_kw, a.q_day_m3)  # tier from Task 2
    assert energy.psh_avg_kwh_m2_day == a.solar.psh_avg  # sunshine from Task 2
    assert energy.e_pv_kwh_day == pytest.approx(pump.total_pv_kwp * a.solar.psh_avg * 0.75)


def test_nasa_failure_propagates(synthetic_parcels, site):
    def offline(lat, lon):
        raise dss.SolarDataError("NASA POWER request failed")

    with pytest.raises(dss.SolarDataError):
        dss.evaluate_site(synthetic_parcels, *site, get_solar=offline)


def test_monthly_profile_applies_task3_per_month(synthetic_parcels, site, stub_solar):
    a = dss.evaluate_site(synthetic_parcels, *site, get_solar=stub_solar)

    profile = a.monthly_profile()

    kwp, e_pump = a.energy.pump.total_pv_kwp, a.energy.e_pump_kwh_day
    expected = [max(0.0, kwp * psh * 0.75 - e_pump) for psh in a.solar.monthly_psh.values()]
    assert list(profile.index) == list(dss.MONTHS)
    assert profile["e_excess_kwh_day"].tolist() == pytest.approx(expected)
    assert profile["e_pv_kwh_day"].idxmin() == a.solar.design_month


def test_yearly_average_hides_the_summer_surplus(stub_solar):
    """Tier 1 at Alentejo: E_excess is 0 at the yearly average, yet May to August run a surplus."""
    solar = stub_solar.resource

    balance = dss.model_energy_balance(2.24, 67.8, solar.psh_avg)
    profile = dss.monthly_energy_profile(balance, solar)

    assert balance.e_excess_kwh_day == 0.0
    assert [m for m, v in profile["e_excess_kwh_day"].items() if v > 0] == ["MAY", "JUN", "JUL", "AUG"]
    assert profile.loc["JUL", "e_excess_kwh_day"] == pytest.approx(5 * 28.44 / 3.6 * 0.75 - 22.2)


# --- Live API -------------------------------------------------------------------------------------

@pytest.mark.network
def test_live_nasa_power_alentejo(nasa_online):
    solar = dss.fetch_solar_resource(*ALENTEJO)

    assert solar.psh_avg == pytest.approx(4.90, abs=0.15)
    assert solar.design_month in {"DEC", "JAN"}
