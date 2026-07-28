import pytest
import httpx
from types import SimpleNamespace

from avalon_q_scheduler.config import NestConfig, load_config
from avalon_q_scheduler.nest_client import (
    NestAuthError,
    NestCredentials,
    NestSdmClient,
    NestTokenCache,
    NestTokenPersistenceError,
    authorization_code_from_input,
    build_authorization_url,
    exchange_authorization_code,
    load_refresh_token,
    load_monthly_backfill,
    reading_from_device,
    refresh_token_source_version,
    save_refresh_token,
)


def test_reading_from_device_maps_sdm_thermostat_traits():
    device = {
        "name": "enterprises/project/devices/thermostat-1",
        "type": "sdm.devices.types.THERMOSTAT",
        "parentRelations": [{"displayName": "Hallway"}],
        "traits": {
            "sdm.devices.traits.Info": {"customName": "Hallway Thermostat"},
            "sdm.devices.traits.Humidity": {"ambientHumidityPercent": 55},
            "sdm.devices.traits.Connectivity": {"status": "ONLINE"},
            "sdm.devices.traits.Fan": {
                "timerMode": "ON",
                "timerTimeout": "2026-06-29T21:07:42Z",
            },
            "sdm.devices.traits.ThermostatMode": {"mode": "HEATCOOL"},
            "sdm.devices.traits.ThermostatEco": {"mode": "OFF"},
            "sdm.devices.traits.ThermostatHvac": {"status": "COOLING"},
            "sdm.devices.traits.ThermostatTemperatureSetpoint": {
                "heatCelsius": 21.89,
                "coolCelsius": 23.888885,
            },
            "sdm.devices.traits.Temperature": {"ambientTemperatureCelsius": 24.1},
        },
    }

    reading = reading_from_device(device)

    assert reading.device_id == "enterprises/project/devices/thermostat-1"
    assert reading.custom_name == "Hallway Thermostat"
    assert reading.room == "Hallway"
    assert reading.hvac_status == "COOLING"
    assert reading.thermostat_mode == "HEATCOOL"
    assert reading.ambient_temp_c == 24.1
    assert reading.ambient_temp_f == pytest.approx(75.38)
    assert reading.ambient_humidity_pct == 55
    assert reading.cool_setpoint_c == 23.888885
    assert reading.fan_timer_mode == "ON"
    assert reading.connectivity_status == "ONLINE"


def test_load_monthly_backfill_reads_screenshot_seed_csv(tmp_path):
    path = tmp_path / "nest_energy_dashboard_monthly_backfill.csv"
    path.write_text(
        "\n".join(
            [
                "month,total_runtime_minutes,heat_minutes,cool_minutes,daily_average_minutes,comparison_text,leaves_earned,source,source_confidence,notes",
                '2025-08,19067,0,19067,635,+10% vs last month,6,google_home_screenshot,exact_monthly_total,"Highest screenshot-backed cooling month."',
            ]
        ),
        encoding="utf-8",
    )

    rows = load_monthly_backfill(path)

    assert len(rows) == 1
    assert rows[0].month == "2025-08"
    assert rows[0].cool_minutes == 19067
    assert rows[0].leaves_earned == 6
    assert rows[0].source_confidence == "exact_monthly_total"


def test_load_refresh_token_prefers_dpapi_file_over_stale_environment(
    tmp_path,
    monkeypatch,
):
    token_path = tmp_path / "nest-token.txt"
    token_path.write_text("encrypted", encoding="utf-8")
    config = NestConfig(refresh_token_dpapi_path=token_path)
    monkeypatch.setenv(config.refresh_token_env, "stale-environment-token")
    monkeypatch.setattr(
        "avalon_q_scheduler.nest_client._load_dpapi_refresh_token",
        lambda path: "fresh-dpapi-token" if path == token_path else "",
    )

    assert load_refresh_token(config) == "fresh-dpapi-token"


def test_enabled_nest_requires_durable_refresh_token_storage(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "miner:\n  host: 192.0.2.1\nnest:\n  enabled: true\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="refresh_token_dpapi_path is required"):
        load_config(config_path)


def test_refresh_token_source_version_changes_with_same_size_dpapi_file(tmp_path):
    token_path = tmp_path / "nest-token.txt"
    config = NestConfig(refresh_token_dpapi_path=token_path)
    token_path.write_text("encrypted-a", encoding="utf-8")
    first = refresh_token_source_version(config)

    token_path.write_text("encrypted-b", encoding="utf-8")
    second = refresh_token_source_version(config)

    assert first != second
    assert "encrypted-a" not in first
    assert "encrypted-b" not in second


def test_build_authorization_url_uses_partner_connections_manager():
    url = build_authorization_url(
        NestConfig(project_id="project-id", redirect_uri="https://www.google.com"),
        NestCredentials(client_id="client-id", client_secret="client-secret"),
    )

    assert url.startswith("https://nestservices.google.com/partnerconnections/project-id/auth?")
    assert "access_type=offline" in url
    assert "prompt=consent" in url
    assert "client_id=client-id" in url
    assert "scope=https%3A%2F%2Fwww.googleapis.com%2Fauth%2Fsdm.service" in url


def test_authorization_code_from_input_accepts_redirect_url():
    code = authorization_code_from_input(
        "https://www.google.com/?code=auth-code&scope=https://www.googleapis.com/auth/sdm.service"
    )

    assert code == "auth-code"


def test_exchange_authorization_code_returns_refresh_token(monkeypatch):
    captured = {}

    class FakeHttpClient:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def post(self, url, **kwargs):
            captured["url"] = url
            captured["data"] = kwargs["data"]
            return httpx.Response(
                200,
                json={
                    "access_token": "access-token",
                    "expires_in": 3599,
                    "refresh_token": "refresh-token",
                },
                request=httpx.Request("POST", url),
            )

    monkeypatch.setattr("avalon_q_scheduler.nest_client.httpx.Client", FakeHttpClient)

    tokens = exchange_authorization_code(
        NestConfig(project_id="project-id", redirect_uri="https://www.google.com"),
        NestCredentials(client_id="client-id", client_secret="client-secret"),
        "auth-code",
    )

    assert tokens.access_token == "access-token"
    assert tokens.refresh_token == "refresh-token"
    assert captured["url"] == "https://oauth2.googleapis.com/token"
    assert captured["data"]["grant_type"] == "authorization_code"
    assert captured["data"]["code"] == "auth-code"
    assert captured["data"]["redirect_uri"] == "https://www.google.com"


def test_refresh_access_token_includes_google_oauth_error(monkeypatch):
    class FakeHttpClient:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def post(self, *_args, **_kwargs):
            return httpx.Response(
                400,
                json={
                    "error": "invalid_grant",
                    "error_description": "Token has been expired or revoked.",
                },
                request=httpx.Request("POST", "https://oauth2.googleapis.com/token"),
            )

    monkeypatch.setattr("avalon_q_scheduler.nest_client.httpx.Client", FakeHttpClient)
    client = NestSdmClient(
        NestConfig(project_id="project-id"),
        NestCredentials(client_id="client-id", client_secret="client-secret"),
        "refresh-token",
    )

    with pytest.raises(NestAuthError, match="invalid_grant.*expired or revoked") as exc_info:
        client.refresh_access_token()

    assert exc_info.value.refresh_grant_invalid is True
    assert exc_info.value.requires_reauthorization is True
    assert exc_info.value.status_code == 400


def test_refresh_access_token_persists_rotated_refresh_token(monkeypatch):
    saved = []

    class FakeHttpClient:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def post(self, *_args, **_kwargs):
            return httpx.Response(
                200,
                json={
                    "access_token": "new-access-token",
                    "expires_in": 3599,
                    "refresh_token": "rotated-refresh-token",
                },
                request=httpx.Request("POST", "https://oauth2.googleapis.com/token"),
            )

    monkeypatch.setattr("avalon_q_scheduler.nest_client.httpx.Client", FakeHttpClient)
    monkeypatch.setattr(
        "avalon_q_scheduler.nest_client.save_refresh_token",
        lambda _config, token: saved.append(token) or "token-path",
    )
    client = NestSdmClient(
        NestConfig(project_id="project-id"),
        NestCredentials(client_id="client-id", client_secret="client-secret"),
        "old-refresh-token",
    )

    token_cache = client.refresh_access_token()

    assert token_cache.access_token == "new-access-token"
    assert client.refresh_token == "rotated-refresh-token"
    assert client.persisted_refresh_token_source_version is not None
    assert saved == ["rotated-refresh-token"]


def test_rotated_refresh_token_save_failure_keeps_tokens_and_retries(monkeypatch):
    calls = {"post": 0, "save": 0, "get": 0}

    class FakeHttpClient:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def post(self, url, **_kwargs):
            calls["post"] += 1
            return httpx.Response(
                200,
                json={
                    "access_token": "new-access-token",
                    "expires_in": 3599,
                    "refresh_token": "rotated-refresh-token",
                },
                request=httpx.Request("POST", url),
            )

        def get(self, url, **_kwargs):
            calls["get"] += 1
            return httpx.Response(
                200,
                json={"devices": []},
                request=httpx.Request("GET", url),
            )

    def flaky_save(_config, _token):
        calls["save"] += 1
        if calls["save"] == 1:
            raise RuntimeError("simulated secure storage failure")

    monkeypatch.setattr("avalon_q_scheduler.nest_client.httpx.Client", FakeHttpClient)
    monkeypatch.setattr("avalon_q_scheduler.nest_client.save_refresh_token", flaky_save)
    monkeypatch.setattr(
        "avalon_q_scheduler.nest_client.refresh_token_source_version",
        lambda _config: "dpapi:rotated",
    )
    client = NestSdmClient(
        NestConfig(project_id="project-id"),
        NestCredentials(client_id="client-id", client_secret="client-secret"),
        "old-refresh-token",
    )

    with pytest.raises(NestTokenPersistenceError) as exc_info:
        client.refresh_access_token()

    assert "rotated-refresh-token" not in str(exc_info.value)
    assert client.refresh_token == "rotated-refresh-token"
    assert client.token_cache.access_token == "new-access-token"
    assert client.refresh_token_persistence_pending is True

    assert client.get_devices() == []
    assert calls == {"post": 1, "save": 2, "get": 1}
    assert client.refresh_token_persistence_pending is False
    assert client.persisted_refresh_token_source_version == "dpapi:rotated"


def test_sdm_401_refreshes_access_token_and_retries_once(monkeypatch):
    calls = {"get": 0, "post": 0}
    authorization_headers = []

    class FakeHttpClient:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def get(self, url, **kwargs):
            calls["get"] += 1
            authorization_headers.append(kwargs["headers"]["Authorization"])
            status_code = 401 if calls["get"] == 1 else 200
            return httpx.Response(
                status_code,
                json={"devices": []},
                request=httpx.Request("GET", url),
            )

        def post(self, url, **_kwargs):
            calls["post"] += 1
            return httpx.Response(
                200,
                json={"access_token": "fresh-access-token", "expires_in": 3599},
                request=httpx.Request("POST", url),
            )

    monkeypatch.setattr("avalon_q_scheduler.nest_client.httpx.Client", FakeHttpClient)
    client = NestSdmClient(
        NestConfig(project_id="project-id"),
        NestCredentials(client_id="client-id", client_secret="client-secret"),
        "refresh-token",
        token_cache=NestTokenCache(
            access_token="rejected-access-token",
            expires_at=float("inf"),
        ),
    )

    assert client.get_devices() == []
    assert calls == {"get": 2, "post": 1}
    assert authorization_headers == [
        "Bearer rejected-access-token",
        "Bearer fresh-access-token",
    ]


def test_sdm_repeated_401_stops_after_one_refresh_and_retry(monkeypatch):
    calls = {"get": 0, "post": 0}

    class FakeHttpClient:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def get(self, url, **_kwargs):
            calls["get"] += 1
            return httpx.Response(
                401,
                json={"error": "UNAUTHENTICATED"},
                request=httpx.Request("GET", url),
            )

        def post(self, url, **_kwargs):
            calls["post"] += 1
            return httpx.Response(
                200,
                json={"access_token": "fresh-access-token", "expires_in": 3599},
                request=httpx.Request("POST", url),
            )

    monkeypatch.setattr("avalon_q_scheduler.nest_client.httpx.Client", FakeHttpClient)
    client = NestSdmClient(
        NestConfig(project_id="project-id"),
        NestCredentials(client_id="client-id", client_secret="client-secret"),
        "refresh-token",
        token_cache=NestTokenCache(
            access_token="rejected-access-token",
            expires_at=float("inf"),
        ),
    )

    with pytest.raises(NestAuthError) as exc_info:
        client.get_devices()

    assert calls == {"get": 2, "post": 1}
    assert exc_info.value.status_code == 401
    assert exc_info.value.requires_reauthorization is True


def test_sdm_401_after_initial_refresh_does_not_refresh_twice(monkeypatch):
    calls = {"get": 0, "post": 0}

    class FakeHttpClient:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def get(self, url, **_kwargs):
            calls["get"] += 1
            return httpx.Response(
                401,
                json={"error": "UNAUTHENTICATED"},
                request=httpx.Request("GET", url),
            )

        def post(self, url, **_kwargs):
            calls["post"] += 1
            return httpx.Response(
                200,
                json={"access_token": "fresh-access-token", "expires_in": 3599},
                request=httpx.Request("POST", url),
            )

    monkeypatch.setattr("avalon_q_scheduler.nest_client.httpx.Client", FakeHttpClient)
    client = NestSdmClient(
        NestConfig(project_id="project-id"),
        NestCredentials(client_id="client-id", client_secret="client-secret"),
        "refresh-token",
    )

    with pytest.raises(NestAuthError) as exc_info:
        client.get_devices()

    assert calls == {"get": 1, "post": 1}
    assert exc_info.value.requires_reauthorization is True


def test_dpapi_refresh_token_save_atomically_replaces_target(tmp_path, monkeypatch):
    token_path = tmp_path / "nest-token.dpapi.txt"
    token_path.write_text("original-encrypted-token", encoding="utf-8")

    def fake_run(_command, **_kwargs):
        temp_paths = list(tmp_path.glob(f".{token_path.name}.*.tmp"))
        assert len(temp_paths) == 1
        assert token_path.read_text(encoding="utf-8") == "original-encrypted-token"
        if "input" in _kwargs:
            temp_paths[0].write_text("rotated-encrypted-token", encoding="utf-8")
            return SimpleNamespace(stdout="")
        return SimpleNamespace(stdout="unprinted-rotated-token\n")

    monkeypatch.setattr("avalon_q_scheduler.nest_client.subprocess.run", fake_run)

    save_refresh_token(
        NestConfig(refresh_token_dpapi_path=token_path),
        "unprinted-rotated-token",
    )

    assert token_path.read_text(encoding="utf-8") == "rotated-encrypted-token"
    assert list(tmp_path.glob(f".{token_path.name}.*.tmp")) == []


def test_dpapi_refresh_token_verification_failure_preserves_target(
    tmp_path,
    monkeypatch,
):
    token_path = tmp_path / "nest-token.dpapi.txt"
    token_path.write_text("original-encrypted-token", encoding="utf-8")

    def corrupt_run(_command, **_kwargs):
        temp_paths = list(tmp_path.glob(f".{token_path.name}.*.tmp"))
        assert len(temp_paths) == 1
        if "input" in _kwargs:
            temp_paths[0].write_text("corrupt-encrypted-token", encoding="utf-8")
            return SimpleNamespace(stdout="")
        return SimpleNamespace(stdout="wrong-decrypted-token\n")

    monkeypatch.setattr("avalon_q_scheduler.nest_client.subprocess.run", corrupt_run)

    with pytest.raises(RuntimeError, match="Failed to verify encrypted Nest refresh token"):
        save_refresh_token(
            NestConfig(refresh_token_dpapi_path=token_path),
            "unprinted-rotated-token",
        )

    assert token_path.read_text(encoding="utf-8") == "original-encrypted-token"
    assert list(tmp_path.glob(f".{token_path.name}.*.tmp")) == []


def test_dpapi_refresh_token_save_failure_preserves_target(tmp_path, monkeypatch):
    token_path = tmp_path / "nest-token.dpapi.txt"
    token_path.write_text("original-encrypted-token", encoding="utf-8")

    def failing_run(_command, **_kwargs):
        temp_paths = list(tmp_path.glob(f".{token_path.name}.*.tmp"))
        assert len(temp_paths) == 1
        temp_paths[0].write_text("partial-encrypted-token", encoding="utf-8")
        raise RuntimeError("simulated encryption failure")

    monkeypatch.setattr("avalon_q_scheduler.nest_client.subprocess.run", failing_run)

    with pytest.raises(RuntimeError, match="simulated encryption failure"):
        save_refresh_token(
            NestConfig(refresh_token_dpapi_path=token_path),
            "unprinted-rotated-token",
        )

    assert token_path.read_text(encoding="utf-8") == "original-encrypted-token"
    assert list(tmp_path.glob(f".{token_path.name}.*.tmp")) == []
