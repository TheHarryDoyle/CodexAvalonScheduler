from datetime import date, datetime, time as datetime_time
from zoneinfo import ZoneInfo

import pytest

from avalon_q_scheduler.config import TeslaConfig
from avalon_q_scheduler.tesla_client import (
    TeslaFleetClient,
    TeslaTokenCache,
    _extract_grid_import_export_wh,
    _extract_vehicle_grid_import_wh,
    _extract_wall_connector_charge_wh,
)


def test_extract_monthly_grid_import_export_from_time_series():
    response = {
        "time_series": [
            {
                "grid_energy_imported": 1000,
                "grid_energy_exported_from_solar": 600,
                "grid_energy_exported_from_battery": 50,
            },
            {
                "grid_energy_imported": 2000,
                "grid_energy_exported_from_solar": 400,
                "grid_energy_exported_from_battery": 25,
            },
        ]
    }

    imported, exported = _extract_grid_import_export_wh(response)

    assert imported == 3000
    assert exported == 1075


def test_extract_vehicle_grid_import_uses_only_grid_energy_sent_to_car():
    response = {
        "time_series": [
            {
                "timestamp": "2026-07-14T01:00:00-05:00",
                "grid_energy_imported": 13000,
                "consumer_energy_imported_from_grid": 700,
                "battery_energy_imported_from_grid": 0,
            },
            {
                "timestamp": "2026-07-15T01:00:00-05:00",
                "grid_energy_imported": 11000,
                "consumer_energy_imported_from_grid": 400,
                "battery_energy_imported_from_grid": 0,
            },
        ]
    }

    grid_vehicle_wh, source = _extract_vehicle_grid_import_wh(
        response,
        start_date=date(2026, 7, 12),
    )

    assert grid_vehicle_wh == 22900
    assert source == "site_energy_flow_balance"


def test_extract_vehicle_grid_import_fails_closed_without_source_breakdown():
    grid_vehicle_wh, source = _extract_vehicle_grid_import_wh(
        {
            "time_series": [
                {
                    "timestamp": "2026-07-14T01:00:00-05:00",
                    "grid_energy_imported": 13000,
                }
            ]
        },
        start_date=date(2026, 7, 12),
    )

    assert grid_vehicle_wh is None
    assert source is None


def test_token_cache_round_trips(tmp_path):
    path = tmp_path / "tesla_token_cache.json"
    cache = TeslaTokenCache(
        access_token="access",
        refresh_token="refresh",
        expires_at=1234,
    )

    TeslaFleetClient.save_token_cache(path, cache)
    loaded = TeslaFleetClient.load_token_cache(path)

    assert loaded.access_token == "access"
    assert loaded.refresh_token == "refresh"
    assert loaded.expires_at == 1234


def test_monthly_energy_derives_vehicle_grid_import_after_plan_start(monkeypatch):
    raw = {
        "response": {
            "time_series": [
                {
                    "timestamp": "2026-07-11T01:00:00-05:00",
                    "grid_energy_imported": 5000,
                    "grid_energy_exported_from_solar": 0,
                    "grid_energy_exported_from_battery": 0,
                    "consumer_energy_imported_from_grid": 1000,
                    "battery_energy_imported_from_grid": 0,
                },
                {
                    "timestamp": "2026-07-14T01:00:00-05:00",
                    "grid_energy_imported": 13000,
                    "grid_energy_exported_from_solar": 0,
                    "grid_energy_exported_from_battery": 0,
                    "consumer_energy_imported_from_grid": 700,
                    "battery_energy_imported_from_grid": 0,
                },
                {
                    "timestamp": "2026-07-15T01:00:00-05:00",
                    "grid_energy_imported": 11000,
                    "grid_energy_exported_from_solar": 0,
                    "grid_energy_exported_from_battery": 0,
                    "consumer_energy_imported_from_grid": 400,
                    "battery_energy_imported_from_grid": 0,
                },
            ]
        }
    }
    client = TeslaFleetClient(TeslaConfig(), token_cache=TeslaTokenCache())
    requested_paths = []
    monkeypatch.setattr(
        client,
        "_get_json",
        lambda path: requested_paths.append(path) or raw,
    )

    result = client.get_monthly_energy(
        123,
        today=date(2026, 7, 15),
        time_zone="America/Chicago",
        vehicle_grid_start_date=date(2026, 7, 12),
    )

    assert result.imported_kwh == 29
    assert result.exported_kwh == 0
    assert result.vehicle_grid_imported_kwh == pytest.approx(22.9)
    assert result.vehicle_grid_source == "site_energy_flow_balance"
    assert result.vehicle_grid_start_date == "2026-07-12"
    assert "calendar_history?kind=energy" in requested_paths[0]
    assert "period=day" in requested_paths[0]


def test_extract_wall_connector_charge_history_and_prorate_boundary_session():
    zone = ZoneInfo("America/Chicago")
    fully_eligible = datetime(2026, 7, 12, 1, 0, tzinfo=zone)
    crosses_end = datetime(2026, 7, 12, 5, 0, tzinfo=zone)
    daytime = datetime(2026, 7, 12, 10, 0, tzinfo=zone)
    response = {
        "response": {
            "charge_history": [
                {
                    "charge_start_time": {"seconds": fully_eligible.timestamp()},
                    "charge_duration": {"seconds": 7200},
                    "energy_added_wh": 10000,
                },
                {
                    "charge_start_time": {"seconds": crosses_end.timestamp()},
                    "charge_duration": {"seconds": 7200},
                    "energy_added_wh": 8000,
                },
                {
                    "charge_start_time": {"seconds": daytime.timestamp()},
                    "charge_duration": {"seconds": 3600},
                    "energy_added_wh": 5000,
                },
            ]
        }
    }

    total, eligible, sessions, eligible_sessions = _extract_wall_connector_charge_wh(
        response,
        time_zone="America/Chicago",
        eligible_start_time=datetime_time(0, 0),
        eligible_end_time=datetime_time(6, 0),
    )

    assert total == 23000
    assert eligible == 14000
    assert sessions == 3
    assert eligible_sessions == 2


def test_extract_wall_connector_charge_history_rejects_unknown_shape():
    with pytest.raises(ValueError, match="charge_history"):
        _extract_wall_connector_charge_wh(
            {"response": {}},
            time_zone="America/Chicago",
            eligible_start_time=datetime_time(0, 0),
            eligible_end_time=datetime_time(6, 0),
        )


def test_monthly_wall_connector_charging_separates_grid_and_powerwall_energy(monkeypatch):
    zone = ZoneInfo("America/Chicago")
    first_session_start = datetime(2026, 7, 14, 1, 0, tzinfo=zone)
    second_session_start = datetime(2026, 7, 15, 1, 0, tzinfo=zone)
    raw = {
        "response": {
            "charge_history": [
                {
                    "charge_start_time": {"seconds": first_session_start.timestamp()},
                    "charge_duration": {"seconds": 3600},
                    "energy_added_wh": 14700,
                },
                {
                    "charge_start_time": {"seconds": second_session_start.timestamp()},
                    "charge_duration": {"seconds": 3600},
                    "energy_added_wh": 17600,
                },
            ]
        }
    }
    client = TeslaFleetClient(TeslaConfig(), token_cache=TeslaTokenCache())
    requested_paths = []
    monkeypatch.setattr(
        client,
        "_get_json",
        lambda path: requested_paths.append(path) or raw,
    )

    result = client.get_monthly_wall_connector_charging(
        123,
        today=date(2026, 7, 15),
        time_zone="America/Chicago",
        plan_start_date=date(2026, 7, 12),
        vehicle_grid_imported_kwh=22.9,
        vehicle_grid_source="site_energy_flow_balance",
    )

    assert result.start_date == "2026-07-12"
    assert result.end_date == "2026-07-16"
    assert result.total_charged_kwh == pytest.approx(32.3)
    assert result.eligible_charged_kwh == pytest.approx(32.3)
    assert result.grid_charged_kwh == pytest.approx(22.9)
    assert result.eligible_grid_charged_kwh == pytest.approx(22.9)
    assert result.eligible_charged_kwh - result.eligible_grid_charged_kwh == pytest.approx(
        9.4
    )
    assert result.grid_source == "site_energy_flow_balance"
    assert "telemetry_history?kind=charge" in requested_paths[0]
    assert "start_date=2026-07-12T00%3A00%3A00-05%3A00" in requested_paths[0]


def test_monthly_charging_caps_grid_energy_at_wall_connector_total(monkeypatch):
    zone = ZoneInfo("America/Chicago")
    session_start = datetime(2026, 7, 14, 1, 0, tzinfo=zone)
    raw = {
        "response": {
            "charge_history": [
                {
                    "charge_start_time": {"seconds": session_start.timestamp()},
                    "charge_duration": {"seconds": 3600},
                    "energy_added_wh": 14000,
                }
            ]
        }
    }
    client = TeslaFleetClient(TeslaConfig(), token_cache=TeslaTokenCache())
    monkeypatch.setattr(client, "_get_json", lambda _path: raw)

    result = client.get_monthly_wall_connector_charging(
        123,
        today=date(2026, 7, 14),
        time_zone="America/Chicago",
        plan_start_date=date(2026, 7, 12),
        vehicle_grid_imported_kwh=20,
        vehicle_grid_source="site_energy_flow_balance",
    )

    assert result.total_charged_kwh == 14
    assert result.grid_charged_kwh == 14
    assert result.eligible_grid_charged_kwh == 14


def test_monthly_charging_allocates_out_of_window_energy_to_grid_first(monkeypatch):
    zone = ZoneInfo("America/Chicago")
    eligible_start = datetime(2026, 7, 14, 1, 0, tzinfo=zone)
    ineligible_start = datetime(2026, 7, 14, 10, 0, tzinfo=zone)
    raw = {
        "response": {
            "charge_history": [
                {
                    "charge_start_time": {"seconds": eligible_start.timestamp()},
                    "charge_duration": {"seconds": 3600},
                    "energy_added_wh": 30000,
                },
                {
                    "charge_start_time": {"seconds": ineligible_start.timestamp()},
                    "charge_duration": {"seconds": 3600},
                    "energy_added_wh": 10000,
                },
            ]
        }
    }
    client = TeslaFleetClient(TeslaConfig(), token_cache=TeslaTokenCache())
    monkeypatch.setattr(client, "_get_json", lambda _path: raw)

    result = client.get_monthly_wall_connector_charging(
        123,
        today=date(2026, 7, 14),
        time_zone="America/Chicago",
        plan_start_date=date(2026, 7, 12),
        vehicle_grid_imported_kwh=20,
        vehicle_grid_source="site_energy_flow_balance",
    )

    assert result.total_charged_kwh == 40
    assert result.eligible_charged_kwh == 30
    assert result.grid_charged_kwh == 20
    assert result.eligible_grid_charged_kwh == 10
