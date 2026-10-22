"""Task 2.2: NASA POWER parsing, unit normalisation, resilient client, cache; Task 2 -> Task 3 wiring."""

from __future__ import annotations

import http.client
import io
import json
import urllib.error
import urllib.parse

import pytest

import dss_pipeline as dss

ALENTEJO = (38.015, -7.865)


class ScriptedTransport:
    """Transport double: replays outcomes in order (bytes returned, exceptions raised) and logs URLs."""

    def __init__(self, *outcomes: bytes | BaseException) -> None:
        self.outcomes = list(outcomes)
        self.urls: list[str] = []

    def __call__(self, url: str, timeout_s: float) -> bytes:
        self.urls.append(url)
        outcome = self.outcomes.pop(0)  # an unscripted extra call fails the test loudly
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def http_error(code: int, body: bytes = b"") -> urllib.error.HTTPError:
    return urllib.error.HTTPError(dss.NASA_POWER_URL, code, "error", None, io.BytesIO(body))  # type: ignore[arg-type]


def make_client(transport: ScriptedTransport, **kwargs) -> tuple[dss.NasaPowerClient, list[float]]:
    sleeps: list[float] = []
    return dss.NasaPowerClient(transport=transport, sleep=sleeps.append, **kwargs), sleeps


# --- Payload parsing and units (A7) ---------------------------------------------------------------
def test_ag_payload_is_converted_from_megajoules(nasa_payload):
    solar = dss.parse_power_climatology(nasa_payload("AG"), *ALENTEJO)
    assert solar.psh_avg == pytest.approx(17.65 / 3.6)
    assert solar.monthly_psh[6] == pytest.approx(28.44 / 3.6)  # JUL
    assert (solar.design_month, solar.design_psh) == ("DEC", pytest.approx(7.58 / 3.6))
    assert "MJ/m^2/day" in solar.source


def test_ag_and_re_communities_agree(nasa_payload):
    ag = dss.parse_power_climatology(nasa_payload("AG"), *ALENTEJO)
    re_ = dss.parse_power_climatology(nasa_payload("RE"), *ALENTEJO)
    assert re_.psh_avg == pytest.approx(4.9039)  # RE already reports kWh/(m2.day)
    assert ag.psh_avg == pytest.approx(re_.psh_avg, rel=2e-3)  # AG rounds MJ to 2 dp
    assert ag.monthly_psh == pytest.approx(re_.monthly_psh, rel=2e-3)


@pytest.mark.parametrize(
    "mutate, match",
    [
        (lambda p: p.pop("properties"), "lacks"),
        (lambda p: p["parameters"]["ALLSKY_SFC_SW_DWN"].pop("units"), "lacks"),
        (lambda p: p["parameters"]["ALLSKY_SFC_SW_DWN"].update(units="W/m^2"), "unexpected units"),
        (lambda p: p["properties"]["parameter"]["ALLSKY_SFC_SW_DWN"].update(DEC=-999.0), "DEC"),  # fill value
        (lambda p: p["properties"]["parameter"]["ALLSKY_SFC_SW_DWN"].pop("JUL"), "JUL"),
        (lambda p: p["properties"]["parameter"]["ALLSKY_SFC_SW_DWN"].update(ANN="n/a"), "ANN"),
        (lambda p: p["properties"]["parameter"].update(ALLSKY_SFC_SW_DWN=[1, 2, 3]), "mapping"),
    ],
)
def test_unusable_payloads_raise(nasa_payload, mutate, match):
    payload = nasa_payload("AG")
    mutate(payload)
    with pytest.raises(dss.SolarDataError, match=match):
        dss.parse_power_climatology(payload, *ALENTEJO)


@pytest.mark.parametrize("payload", [None, [], "oops", {"header": "failed", "messages": ["bad request"]}])
def test_non_payloads_raise(payload):
    with pytest.raises(dss.SolarDataError):
        dss.parse_power_climatology(payload, *ALENTEJO)


def test_kwh_label_on_megajoule_values_is_caught(nasa_payload):
    """Units metadata saying kWh while values are MJ-sized trips the plausibility guard."""
    payload = nasa_payload("AG")
    payload["parameters"]["ALLSKY_SFC_SW_DWN"]["units"] = "kW-hr/m^2/day"
    with pytest.raises(dss.SolarDataError, match="implausible"):
        dss.parse_power_climatology(payload, *ALENTEJO)


def test_solar_resource_validates_and_freezes():
    solar = dss.SolarResource(38.0, -7.9, [5.0] * 11 + [2.0], 4.9)
    assert isinstance(solar.monthly_psh, tuple) and solar.design_month == "DEC" and solar.design_psh == 2.0
    with pytest.raises(ValueError, match="12 values"):
        dss.SolarResource(38.0, -7.9, (5.0,) * 11, 4.9)
    with pytest.raises(ValueError):
        dss.SolarResource(38.0, -7.9, (5.0,) * 12, -1.0)
    with pytest.raises(ValueError, match="MJ"):
        dss.SolarResource(38.0, -7.9, (5.0,) * 12, 17.65)
    with pytest.raises(ValueError):
        dss.SolarResource(95.0, -7.9, (5.0,) * 12, 4.9)


# --- Client: request, cache, resilience (A9) ------------------------------------------------------
def test_request_follows_spec_url(nasa_bytes):
    transport = ScriptedTransport(nasa_bytes("AG"))
    client, _ = make_client(transport)
    solar = client.solar_resource(*ALENTEJO)
    url = transport.urls[0]
    assert url.startswith(dss.NASA_POWER_URL + "?")
    assert urllib.parse.parse_qs(urllib.parse.urlsplit(url).query) == {
        "parameters": ["ALLSKY_SFC_SW_DWN"],
        "community": ["AG"],
        "longitude": [str(solar.lon)],
        "latitude": [str(solar.lat)],
        "format": ["JSON"],
    }
    assert (solar.lat, solar.lon) == (round(ALENTEJO[0], 2), round(ALENTEJO[1], 2))


def test_cache_serves_repeat_queries_without_network(nasa_bytes):
    transport = ScriptedTransport(nasa_bytes("AG"))
    client, _ = make_client(transport)
    first = client.solar_resource(38.0112, -7.8611)
    again = client.solar_resource(38.0138, -7.8641)  # same 0.01-degree key
    assert again is first and len(transport.urls) == 1


def test_distinct_points_are_fetched_separately(nasa_bytes):
    transport = ScriptedTransport(nasa_bytes("AG"), nasa_bytes("AG"))
    client, _ = make_client(transport)
    client.solar_resource(38.01, -7.86)
    client.solar_resource(38.50, -7.86)
    assert len(transport.urls) == 2


def test_transient_failures_are_retried_with_backoff(nasa_bytes):
    transport = ScriptedTransport(http_error(503), TimeoutError("read timed out"), nasa_bytes("AG"))
    client, sleeps = make_client(transport)
    assert client.solar_resource(*ALENTEJO).design_month == "DEC"
    assert len(transport.urls) == 3 and sleeps == [1.0, 2.0]


def test_rate_limit_is_retried(nasa_bytes):
    transport = ScriptedTransport(http_error(429), nasa_bytes("AG"))
    client, sleeps = make_client(transport)
    client.solar_resource(*ALENTEJO)
    assert sleeps == [1.0]


@pytest.mark.parametrize(
    "failure",
    [
        urllib.error.URLError("getaddrinfo failed"),
        ConnectionResetError("reset by peer"),
        http.client.IncompleteRead(b""),
    ],
)
def test_gives_up_after_bounded_attempts(failure):
    transport = ScriptedTransport(failure, failure, failure)
    client, sleeps = make_client(transport, attempts=3)
    with pytest.raises(dss.SolarDataError, match="unreachable after 3 attempt"):
        client.solar_resource(*ALENTEJO)
    assert len(transport.urls) == 3 and sleeps == [1.0, 2.0]


def test_persistent_server_error_reports_status():
    transport = ScriptedTransport(http_error(500), http_error(500))
    client, _ = make_client(transport, attempts=2)
    with pytest.raises(dss.SolarDataError, match="HTTP 500 after 2 attempt"):
        client.solar_resource(*ALENTEJO)


def test_client_errors_fail_fast_with_nasa_message():
    body = json.dumps({"header": "failed", "messages": ["Please provide a correct latitude value."]}).encode()
    transport = ScriptedTransport(http_error(422, body))
    client, sleeps = make_client(transport)
    with pytest.raises(dss.SolarDataError, match=r"HTTP 422 after 1 attempt.*correct latitude"):
        client.solar_resource(*ALENTEJO)
    assert len(transport.urls) == 1 and sleeps == []


def test_non_json_body_fails_fast():
    transport = ScriptedTransport(b"<html>scheduled maintenance</html>")
    client, _ = make_client(transport)
    with pytest.raises(dss.SolarDataError, match="not valid JSON"):
        client.solar_resource(*ALENTEJO)
    assert len(transport.urls) == 1


def test_failures_are_not_cached(nasa_bytes):
    transport = ScriptedTransport(urllib.error.URLError("offline"), nasa_bytes("AG"))
    client, _ = make_client(transport, attempts=1)
    with pytest.raises(dss.SolarDataError):
        client.solar_resource(*ALENTEJO)
    assert client.solar_resource(*ALENTEJO).psh_avg == pytest.approx(17.65 / 3.6)


def test_invalid_coordinates_never_reach_the_network():
    transport = ScriptedTransport()
    client, _ = make_client(transport)
    with pytest.raises(ValueError):
        client.solar_resource(95, 0)
    with pytest.raises(TypeError):
        client.solar_resource("38", -7.8)
    assert transport.urls == []


@pytest.mark.parametrize("kwargs", [{"attempts": 0}, {"attempts": 1.5}, {"timeout_s": 0}, {"backoff_s": -1}])
def test_client_configuration_validated(kwargs):
    with pytest.raises(ValueError):
        dss.NasaPowerClient(**kwargs)


# --- Task 2 -> Task 3 -----------------------------------------------------------------------------
def test_evaluate_site_queries_the_cluster_centroid(synthetic_parcels, site, stub_solar):
    a = dss.evaluate_site(synthetic_parcels, *site, solar_client=stub_solar)
    assert stub_solar.calls == [pytest.approx(a.cluster.centroid_lat_lon())]
    assert a.solar is stub_solar.resource


def test_task3_consumes_task2_outputs(synthetic_parcels, site, stub_solar):
    a = dss.evaluate_site(synthetic_parcels, *site, solar_client=stub_solar)
    e, p = a.energy, a.energy.pump
    assert a.hydraulic_power_kw == pytest.approx(dss.hydraulic_power_kw(a.q_day_m3))  # 2.3 from 2.1
    assert (p.required_power_kw, p.q_day_m3) == (a.hydraulic_power_kw, a.q_day_m3)  # tier matched on Task 2
    assert e.psh_avg_kwh_m2_day == a.solar.psh_avg  # E_PV on Task 2's PSH_avg
    assert e.e_pv_kwh_day == pytest.approx(p.total_pv_kwp * a.solar.psh_avg * 0.75)


def test_solar_failure_propagates(synthetic_parcels, site):
    class Offline:
        def solar_resource(self, lat: float, lon: float) -> dss.SolarResource:
            raise dss.SolarDataError("NASA POWER unreachable after 3 attempt(s)")

    with pytest.raises(dss.SolarDataError):
        dss.evaluate_site(synthetic_parcels, *site, solar_client=Offline())


def test_monthly_profile_applies_task3_per_month(synthetic_parcels, site, stub_solar):
    a = dss.evaluate_site(synthetic_parcels, *site, solar_client=stub_solar)
    profile = a.monthly_profile()
    assert list(profile.index) == list(dss.MONTHS)
    kwp, e_pump = a.energy.pump.total_pv_kwp, a.energy.e_pump_kwh_day
    expected = [max(0.0, kwp * psh * 0.75 - e_pump) for psh in a.solar.monthly_psh]
    assert profile["e_excess_kwh_day"].tolist() == pytest.approx(expected)
    assert profile["e_pv_kwh_day"].idxmin() == a.solar.design_month


def test_seasonal_surplus_hidden_by_annual_mean(stub_solar):
    """Tier 1 at Alentejo: E_excess = 0 at PSH_avg, yet May-Aug run a surplus."""
    solar = stub_solar.resource
    balance = dss.model_energy_balance(2.24, 67.8, solar.psh_avg)
    profile = dss.monthly_energy_profile(balance, solar)
    assert balance.e_excess_kwh_day == 0.0
    assert [m for m, v in profile["e_excess_kwh_day"].items() if v > 0] == ["MAY", "JUN", "JUL", "AUG"]
    assert profile.loc["JUL", "e_excess_kwh_day"] == pytest.approx(5 * 28.44 / 3.6 * 0.75 - 22.2)


# --- Live API -------------------------------------------------------------------------------------
@pytest.mark.network
def test_live_nasa_power_alentejo(nasa_online):
    solar = dss.NasaPowerClient().solar_resource(*ALENTEJO)
    assert solar.psh_avg == pytest.approx(4.90, abs=0.15)
    assert solar.design_month in {"DEC", "JAN"}
