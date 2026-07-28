from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

import avalon_q_scheduler.scheduler as scheduler_module

from avalon_q_scheduler.avalon_client import (
    AvalonClient,
    AvalonCommandRejected,
    AvalonCommands,
)
from avalon_q_scheduler.config import (
    AppConfig,
    AutomationConfig,
    EnergyPolicyConfig,
    MinerConfig,
    NestConfig,
    F2PoolConfig,
    ScheduleConfig,
    TeslaConfig,
    WeatherConfig,
)
from avalon_q_scheduler.decision_engine import Decision, TargetMode
from avalon_q_scheduler.nest_client import NestAuthError
from avalon_q_scheduler.scheduler import AvalonScheduler, ModeController
from avalon_q_scheduler.state_store import SchedulerState, StateStore
from avalon_q_scheduler.tesla_client import (
    TeslaLiveStatus,
    TeslaMonthlyCharging,
    TeslaMonthlyEnergy,
)
from avalon_q_scheduler.weather_client import DayWeather


def config(tmp_path):
    return AppConfig(
        timezone="America/Chicago",
        miner=MinerConfig(host="192.0.2.1"),
        automation=AutomationConfig(dry_run=True),
        schedule=ScheduleConfig(),
        weather=WeatherConfig(),
        tesla=TeslaConfig(),
        nest=NestConfig(),
        energy_policy=EnergyPolicyConfig(),
        state_path=tmp_path / "state.json",
        log_path=tmp_path / "test.log",
    )


def weather_snapshot(target_mode, reason, weather_date=None):
    return {
        "target_mode": target_mode,
        "reason": reason,
        "weather": {
            "date": weather_date
            or datetime.now(ZoneInfo("America/Chicago")).date().isoformat(),
        },
    }


def test_dry_run_records_decision_without_marking_mode_commanded(tmp_path):
    app_config = config(tmp_path)
    store = StateStore(app_config.state_path)
    controller = ModeController(app_config, AvalonClient("192.0.2.1"), store)

    result = controller.apply_decision(
        Decision(TargetMode.SUPER, "cheap power window started"),
        dry_run=True,
        update_weather_decision=True,
    )
    state = store.load()

    assert "DRY RUN" in result
    assert state.current_known_mode is None
    assert state.last_commanded_mode is None
    assert state.last_commanded_at is None
    assert state.last_weather_decision == {
        "target_mode": "super",
        "reason": "cheap power window started",
    }


class FakeClient:
    def __init__(self):
        self.commands = AvalonCommands()
        self.sent_commands = []

    def send_command(self, command):
        self.sent_commands.append(command)
        return "OK"


def scheduler_with_fake_client(tmp_path, initial_state):
    app_config = config(tmp_path)
    object.__setattr__(
        app_config,
        "automation",
        AutomationConfig(
            dry_run=False,
            min_minutes_between_mode_changes=0,
            softon_delay_seconds=0,
        ),
    )
    store = StateStore(app_config.state_path)
    store.save(initial_state)
    scheduler = AvalonScheduler(app_config)
    fake_client = FakeClient()
    scheduler.client = fake_client
    scheduler.state_store = store
    scheduler.mode_controller = ModeController(app_config, fake_client, store)
    return scheduler, store, fake_client


def test_evening_keeps_rainy_day_off(tmp_path):
    scheduler, store, fake_client = scheduler_with_fake_client(
        tmp_path,
        SchedulerState(
            current_known_mode="off",
            last_commanded_mode="off",
            softoff_active=True,
            last_weather_decision=weather_snapshot("off", "rainy forecast"),
        ),
    )

    result = scheduler.run_evening_low()

    assert result == "already in requested mode"
    assert fake_client.sent_commands == []
    assert store.load().current_known_mode == "off"


def test_evening_sets_eco_after_non_rainy_day(tmp_path):
    scheduler, store, fake_client = scheduler_with_fake_client(
        tmp_path,
        SchedulerState(
            current_known_mode="super",
            last_commanded_mode="super",
            last_weather_decision=weather_snapshot("super", "sunny forecast"),
        ),
    )

    scheduler.run_evening_low()

    assert fake_client.sent_commands == ["ascset|0,workmode,set,0"]
    assert store.load().current_known_mode == "eco"


class FakeWeatherClient:
    def __init__(self, forecast):
        self.forecast = forecast
        self.requested_windows = []

    def get_today_forecast(self, *, window_start=None, window_end=None):
        self.requested_windows.append((window_start, window_end))
        return self.forecast


def test_morning_weather_uses_eco_for_sunny_forecast_when_peak_window_enabled(tmp_path):
    scheduler, store, fake_client = scheduler_with_fake_client(
        tmp_path,
        SchedulerState(),
    )
    scheduler.weather_client = FakeWeatherClient(
        DayWeather(
            date=datetime.now(ZoneInfo("America/Chicago")).date().isoformat(),
            avg_cloud_cover_percent=10,
            max_precip_probability_percent=0,
            total_precip_mm=0,
            weather_codes=[0],
            raw={},
        )
    )

    scheduler.run_morning_weather_decision()

    assert fake_client.sent_commands == ["ascset|0,workmode,set,0"]
    assert store.load().last_weather_decision["target_mode"] == "eco"


def test_startup_catchup_runs_missing_morning_weather_decision_after_softoff(tmp_path, monkeypatch):
    class FakeDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 7, 7, 10, 0, tzinfo=tz)

    monkeypatch.setattr(scheduler_module, "datetime", FakeDateTime)
    scheduler, store, fake_client = scheduler_with_fake_client(
        tmp_path,
        SchedulerState(
            current_known_mode="off",
            last_commanded_mode="off",
            softoff_active=True,
            last_weather_decision=weather_snapshot(
                "super",
                "peak solar window started",
                weather_date="2026-07-06",
            ),
        ),
    )
    scheduler.weather_client = FakeWeatherClient(
        DayWeather(
            date="2026-07-07",
            avg_cloud_cover_percent=10,
            max_precip_probability_percent=0,
            total_precip_mm=0,
            weather_codes=[0],
            raw={},
            avg_shortwave_radiation_w_m2=700,
        )
    )

    scheduler._run_startup_catchup()

    assert fake_client.sent_commands[0].startswith("ascset|0,softon,1:")
    assert fake_client.sent_commands[-1] == "ascset|0,workmode,set,0"
    state = store.load()
    assert state.current_known_mode == "eco"
    assert state.softoff_active is False
    assert state.last_weather_decision["weather"]["date"] == "2026-07-07"


def test_startup_catchup_does_not_wake_after_evening_window(tmp_path, monkeypatch):
    class FakeDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 7, 7, 19, 0, tzinfo=tz)

    monkeypatch.setattr(scheduler_module, "datetime", FakeDateTime)
    scheduler, _store, fake_client = scheduler_with_fake_client(
        tmp_path,
        SchedulerState(
            current_known_mode="off",
            last_commanded_mode="off",
            softoff_active=True,
            last_weather_decision=weather_snapshot(
                "super",
                "peak solar window started",
                weather_date="2026-07-06",
            ),
        ),
    )

    scheduler._run_startup_catchup()

    assert fake_client.sent_commands == []


def test_nest_poll_backs_off_after_failure(tmp_path, monkeypatch):
    class FakeDateTime(datetime):
        current = datetime(2026, 7, 7, 8, 0, tzinfo=ZoneInfo("America/Chicago"))

        @classmethod
        def now(cls, tz=None):
            if tz is None:
                return cls.current
            return cls.current.astimezone(tz)

    class FailingNestClient:
        def __init__(self):
            self.calls = 0

        def get_thermostat_reading(self):
            self.calls += 1
            raise RuntimeError("bad nest token")

    monkeypatch.setattr(scheduler_module, "datetime", FakeDateTime)
    scheduler, _store, _fake_client = scheduler_with_fake_client(
        tmp_path,
        SchedulerState(),
    )
    failing_nest = FailingNestClient()
    scheduler.nest_client = failing_nest

    first = scheduler.run_nest_log_once()
    second = scheduler.run_nest_log_once()
    FakeDateTime.current = datetime(2026, 7, 7, 8, 1, 1, tzinfo=ZoneInfo("America/Chicago"))
    third = scheduler.run_nest_log_once()

    assert "retrying after 60 seconds" in first
    assert "skipped until" in second
    assert "retrying after 120 seconds" in third
    assert failing_nest.calls == 2


@pytest.mark.parametrize(
    "oauth_error",
    ["invalid_grant", "sdm_unauthorized_after_refresh"],
)
def test_nest_terminal_auth_failure_pauses_until_refresh_token_changes(
    tmp_path,
    monkeypatch,
    oauth_error,
):
    versions = ["dpapi:v1"]

    class InvalidGrantNestClient:
        def __init__(self):
            self.calls = 0

        def get_thermostat_reading(self):
            self.calls += 1
            raise NestAuthError(
                f"Nest authentication failed ({oauth_error})",
                status_code=400 if oauth_error == "invalid_grant" else 401,
                oauth_error=oauth_error,
            )

    class TransientNestClient:
        def __init__(self):
            self.calls = 0

        def get_thermostat_reading(self):
            self.calls += 1
            raise RuntimeError("temporary Nest outage")

    monkeypatch.setattr(
        scheduler_module,
        "refresh_token_source_version",
        lambda _config: versions[0],
    )
    scheduler, _store, _fake_client = scheduler_with_fake_client(
        tmp_path,
        SchedulerState(),
    )
    invalid_client = InvalidGrantNestClient()
    scheduler.nest_client = invalid_client

    first = scheduler.run_nest_log_once()
    second = scheduler.run_nest_log_once()

    assert "paused until the OAuth refresh grant changes" in first
    assert "paused until the OAuth refresh grant changes" in second
    assert invalid_client.calls == 1
    assert scheduler._load_nest_auth_block() == "dpapi:v1"

    object.__setattr__(scheduler.config, "nest", NestConfig(enabled=True))
    restarted_scheduler = AvalonScheduler(scheduler.config)
    restarted_result = restarted_scheduler.run_nest_log_once()

    assert "paused until the OAuth refresh grant changes" in restarted_result

    versions[0] = "dpapi:v2"
    transient_client = TransientNestClient()
    monkeypatch.setattr(scheduler, "_nest_client", lambda: transient_client)

    third = scheduler.run_nest_log_once()

    assert "retrying after 60 seconds" in third
    assert transient_client.calls == 1
    assert scheduler._load_nest_auth_block() is None


def test_nest_internal_refresh_token_rotation_keeps_access_token_cache(
    tmp_path,
    monkeypatch,
):
    versions = ["dpapi:v1"]

    class RotatingNestClient:
        def __init__(self):
            self.calls = 0
            self.persisted_refresh_token_source_version = None

        def get_thermostat_reading(self):
            self.calls += 1
            if self.calls == 1:
                versions[0] = "dpapi:v2"
                self.persisted_refresh_token_source_version = "dpapi:v2"
            return SimpleNamespace(
                custom_name="Hallway",
                room="Hallway",
                hvac_status="OFF",
                ambient_temp_c=22.0,
                ambient_humidity_pct=45.0,
            )

    monkeypatch.setattr(
        scheduler_module,
        "refresh_token_source_version",
        lambda _config: versions[0],
    )
    monkeypatch.setattr(scheduler_module, "append_reading", lambda *_args: None)
    scheduler, _store, _fake_client = scheduler_with_fake_client(
        tmp_path,
        SchedulerState(),
    )
    rotating_client = RotatingNestClient()
    scheduler.nest_client = rotating_client
    scheduler.nest_token_source_version = "dpapi:v1"

    first = scheduler.run_nest_log_once()
    second = scheduler.run_nest_log_once()

    assert "Nest reading logged" in first
    assert "Nest reading logged" in second
    assert rotating_client.calls == 2
    assert scheduler.nest_client is rotating_client
    assert scheduler.nest_token_source_version == "dpapi:v2"


def test_nest_terminal_failure_does_not_block_concurrent_replacement(
    tmp_path,
    monkeypatch,
):
    versions = ["dpapi:token-a"]

    class FailingOldTokenClient:
        persisted_refresh_token_source_version = None

        def get_thermostat_reading(self):
            versions[0] = "dpapi:token-b"
            raise NestAuthError(
                "old token was revoked",
                status_code=400,
                oauth_error="invalid_grant",
            )

    class TransientNewTokenClient:
        def get_thermostat_reading(self):
            raise RuntimeError("temporary Nest outage")

    monkeypatch.setattr(
        scheduler_module,
        "refresh_token_source_version",
        lambda _config: versions[0],
    )
    scheduler, _store, _fake_client = scheduler_with_fake_client(
        tmp_path,
        SchedulerState(),
    )
    scheduler.nest_client = FailingOldTokenClient()
    scheduler.nest_token_source_version = "dpapi:token-a"

    first = scheduler.run_nest_log_once()

    assert "paused until the OAuth refresh grant changes" in first
    assert scheduler._load_nest_auth_block() == "dpapi:token-a"

    new_client = TransientNewTokenClient()
    monkeypatch.setattr(scheduler, "_nest_client", lambda: new_client)

    second = scheduler.run_nest_log_once()

    assert "retrying after 60 seconds" in second
    assert scheduler._load_nest_auth_block() is None


def test_disabled_nest_ignores_corrupt_auth_block_sidecar(tmp_path):
    app_config = config(tmp_path)
    block_path = app_config.state_path.with_name(
        f"{app_config.state_path.name}.nest-auth-block"
    )
    block_path.write_bytes(b"\xff\xfe")

    scheduler = AvalonScheduler(app_config)

    assert scheduler.nest_blocked_token_source_version is None


def test_enabled_nest_stays_paused_when_auth_block_sidecar_is_corrupt(tmp_path):
    app_config = config(tmp_path)
    token_path = tmp_path / "nest-token.dpapi.txt"
    object.__setattr__(
        app_config,
        "nest",
        NestConfig(
            enabled=True,
            refresh_token_dpapi_path=token_path,
            poll_interval_seconds=60,
        ),
    )
    block_path = token_path.with_name(f"{token_path.name}.auth-block")
    block_path.write_bytes(b"\xff\xfe")

    scheduler = AvalonScheduler(app_config)
    result = scheduler.run_nest_log_once()

    assert "poll paused" in result
    assert scheduler.nest_retry_after is not None


def test_peak_solar_high_sets_super_for_strong_radiation(tmp_path):
    scheduler, store, fake_client = scheduler_with_fake_client(
        tmp_path,
        SchedulerState(current_known_mode="eco", last_commanded_mode="eco"),
    )
    scheduler.weather_client = FakeWeatherClient(
        DayWeather(
            date=datetime.now(ZoneInfo("America/Chicago")).date().isoformat(),
            avg_cloud_cover_percent=70,
            max_precip_probability_percent=0,
            total_precip_mm=0,
            weather_codes=[0],
            raw={},
            avg_shortwave_radiation_w_m2=700,
        )
    )

    scheduler.run_peak_solar_high()

    assert fake_client.sent_commands == ["ascset|0,workmode,set,2"]
    assert scheduler.weather_client.requested_windows == [("11:00", "15:00")]
    assert store.load().current_known_mode == "super"


def test_peak_solar_high_preserves_rainy_day_off_snapshot(tmp_path):
    today = datetime.now(ZoneInfo("America/Chicago")).date().isoformat()
    scheduler, store, fake_client = scheduler_with_fake_client(
        tmp_path,
        SchedulerState(
            current_known_mode="off",
            last_commanded_mode="off",
            softoff_active=True,
            last_weather_decision=weather_snapshot(
                "off",
                "rainy forecast",
                weather_date=today,
            ),
        ),
    )

    result = scheduler.run_peak_solar_high()

    assert result == "already in requested mode"
    assert fake_client.sent_commands == []
    saved = store.load().last_weather_decision
    assert saved["target_mode"] == "off"
    assert saved["weather"]["date"] == today


def test_late_afternoon_bridge_uses_evening_off_when_solar_is_weak(tmp_path):
    app_config = config(tmp_path)
    object.__setattr__(
        app_config,
        "automation",
        AutomationConfig(
            dry_run=False,
            min_minutes_between_mode_changes=0,
            evening_behavior="off",
            softon_delay_seconds=0,
        ),
    )
    store = StateStore(app_config.state_path)
    store.save(
        SchedulerState(
            current_known_mode="eco",
            last_commanded_mode="eco",
            last_weather_decision=weather_snapshot("eco", "morning solar window started"),
        )
    )
    scheduler = AvalonScheduler(app_config)
    fake_client = FakeClient()
    scheduler.client = fake_client
    scheduler.state_store = store
    scheduler.mode_controller = ModeController(app_config, fake_client, store)
    scheduler.weather_client = FakeWeatherClient(
        DayWeather(
            date=datetime.now(ZoneInfo("America/Chicago")).date().isoformat(),
            avg_cloud_cover_percent=80,
            max_precip_probability_percent=0,
            total_precip_mm=0,
            weather_codes=[3],
            raw={},
            avg_shortwave_radiation_w_m2=100,
        )
    )

    scheduler.run_late_afternoon_bridge()

    assert fake_client.sent_commands[-1].startswith("ascset|0,softoff,1:")
    assert scheduler.weather_client.requested_windows == [("16:00", "18:00")]
    assert store.load().current_known_mode == "off"


def test_late_afternoon_bridge_keeps_eco_when_solar_is_usable(tmp_path):
    scheduler, store, fake_client = scheduler_with_fake_client(
        tmp_path,
        SchedulerState(
            current_known_mode="eco",
            last_commanded_mode="eco",
            last_weather_decision=weather_snapshot("eco", "morning solar window started"),
        ),
    )
    scheduler.weather_client = FakeWeatherClient(
        DayWeather(
            date=datetime.now(ZoneInfo("America/Chicago")).date().isoformat(),
            avg_cloud_cover_percent=20,
            max_precip_probability_percent=0,
            total_precip_mm=0,
            weather_codes=[0],
            raw={},
            avg_shortwave_radiation_w_m2=300,
        )
    )

    result = scheduler.run_late_afternoon_bridge()

    assert result == "already in requested mode"
    assert fake_client.sent_commands == []
    assert scheduler.weather_client.requested_windows == [("16:00", "18:00")]
    assert store.load().current_known_mode == "eco"


def test_evening_behavior_can_turn_miner_off_overnight(tmp_path):
    app_config = config(tmp_path)
    object.__setattr__(
        app_config,
        "automation",
        AutomationConfig(
            dry_run=False,
            min_minutes_between_mode_changes=0,
            evening_behavior="off",
            softon_delay_seconds=0,
        ),
    )
    store = StateStore(app_config.state_path)
    store.save(
        SchedulerState(
            current_known_mode="eco",
            last_commanded_mode="eco",
            last_weather_decision=weather_snapshot("eco", "morning solar window started"),
        )
    )
    scheduler = AvalonScheduler(app_config)
    fake_client = FakeClient()
    scheduler.client = fake_client
    scheduler.state_store = store
    scheduler.mode_controller = ModeController(app_config, fake_client, store)

    scheduler.run_evening_low()

    assert fake_client.sent_commands[-1].startswith("ascset|0,softoff,1:")
    assert store.load().current_known_mode == "off"


def test_evening_ignores_stale_rainy_weather_decision(tmp_path):
    scheduler, store, fake_client = scheduler_with_fake_client(
        tmp_path,
        SchedulerState(
            current_known_mode="off",
            last_commanded_mode="off",
            softoff_active=True,
            last_weather_decision=weather_snapshot(
                "off",
                "rainy forecast",
                weather_date="2026-06-01",
            ),
        ),
    )

    scheduler.run_evening_low()

    assert fake_client.sent_commands[0].startswith("ascset|0,softon,1:")
    assert fake_client.sent_commands[-1] == "ascset|0,workmode,set,0"
    assert store.load().current_known_mode == "eco"


def test_duplicate_morning_mode_still_refreshes_weather_decision(tmp_path):
    app_config = config(tmp_path)
    store = StateStore(app_config.state_path)
    store.save(
        SchedulerState(
            current_known_mode="super",
            last_commanded_mode="super",
            softoff_active=False,
            last_weather_decision=weather_snapshot(
                "off",
                "old rainy forecast",
                weather_date="2026-06-01",
            ),
        )
    )
    controller = ModeController(app_config, AvalonClient("192.0.2.1"), store)
    forecast = DayWeather(
        date="2026-06-23",
        avg_cloud_cover_percent=20,
        max_precip_probability_percent=0,
        total_precip_mm=0,
        weather_codes=[0],
        raw={},
    )

    result = controller.apply_decision(
        Decision(TargetMode.SUPER, "sunny forecast", forecast),
        update_weather_decision=True,
    )

    assert result == "already in requested mode"
    saved = store.load().last_weather_decision
    assert saved is not None
    assert saved["target_mode"] == "super"
    assert saved["weather"]["date"] == "2026-06-23"


def test_cheap_power_low_returns_to_off_if_miner_was_off_before_window(tmp_path):
    scheduler, store, fake_client = scheduler_with_fake_client(
        tmp_path,
        SchedulerState(
            current_known_mode="off",
            last_commanded_mode="off",
        ),
    )

    scheduler.run_cheap_power_high()
    scheduler.run_cheap_power_low()

    assert fake_client.sent_commands[-1].startswith("ascset|0,softoff,1:")
    state = store.load()
    assert state.current_known_mode == "off"
    assert state.mode_before_cheap_power is None


def test_cheap_power_low_returns_to_eco_if_miner_was_on_before_window(tmp_path):
    scheduler, store, fake_client = scheduler_with_fake_client(
        tmp_path,
        SchedulerState(
            current_known_mode="eco",
            last_commanded_mode="eco",
        ),
    )

    scheduler.run_cheap_power_high()
    scheduler.run_cheap_power_low()

    assert fake_client.sent_commands[-1] == "ascset|0,workmode,set,0"
    assert store.load().current_known_mode == "eco"


def test_rejected_avalon_command_does_not_update_mode_state(tmp_path):
    app_config = config(tmp_path)
    object.__setattr__(
        app_config,
        "automation",
        AutomationConfig(dry_run=False, min_minutes_between_mode_changes=0),
    )
    store = StateStore(app_config.state_path)
    controller = ModeController(app_config, AvalonClient("192.0.2.1"), store)
    controller.client.send_command = lambda command: "STATUS=E,Code=120,Msg=failed|"

    with pytest.raises(AvalonCommandRejected):
        controller.apply_decision(Decision(TargetMode.OFF, "rainy forecast"))

    state = store.load()
    assert state.current_known_mode is None
    assert state.last_commanded_mode is None
    assert state.last_api_response == "STATUS=E,Code=120,Msg=failed|"


class SequenceClient(FakeClient):
    def __init__(self, responses):
        super().__init__()
        self.responses = iter(responses)

    def send_command(self, command):
        self.sent_commands.append(command)
        return next(self.responses)


class FakeTeslaClient:
    def __init__(self, monthly=None, live=None, charging=None):
        self.monthly = monthly or TeslaMonthlyEnergy(
            imported_kwh=100,
            exported_kwh=120,
            start_date="2026-06-01",
            end_date="2026-06-30",
            raw={},
        )
        self.live = live or TeslaLiveStatus(
            battery_percent=50,
            solar_power_w=0,
            load_power_w=0,
            grid_power_w=0,
            battery_power_w=0,
            grid_status="SystemGridConnected",
            storm_mode_enabled=False,
            raw={},
        )
        self.charging = charging or TeslaMonthlyCharging(
            total_charged_kwh=0,
            eligible_charged_kwh=0,
            session_count=0,
            eligible_session_count=0,
            start_date="2026-06-01",
            end_date="2026-06-30",
            raw={},
        )
        self.monthly_calls = 0
        self.live_calls = 0
        self.charging_calls = 0
        self.monthly_kwargs = []
        self.charging_kwargs = []

    def get_monthly_energy(self, *_args, **kwargs):
        self.monthly_calls += 1
        self.monthly_kwargs.append(kwargs)
        return self.monthly

    def get_live_status(self, *_args, **_kwargs):
        self.live_calls += 1
        return self.live

    def get_monthly_wall_connector_charging(self, *_args, **kwargs):
        self.charging_calls += 1
        self.charging_kwargs.append(kwargs)
        return self.charging


def powerwall_scheduler_with_fake_clients(
    tmp_path,
    initial_state,
    monthly=None,
    live=None,
    charging=None,
):
    app_config = config(tmp_path)
    object.__setattr__(
        app_config,
        "automation",
        AutomationConfig(
            dry_run=False,
            min_minutes_between_mode_changes=0,
            enable_powerwall_policy=True,
            softon_delay_seconds=0,
        ),
    )
    object.__setattr__(app_config, "tesla", TeslaConfig(energy_site_id=123))
    store = StateStore(app_config.state_path)
    store.save(initial_state)
    scheduler = AvalonScheduler(app_config)
    fake_client = FakeClient()
    fake_tesla = FakeTeslaClient(monthly=monthly, live=live, charging=charging)
    scheduler.client = fake_client
    scheduler.tesla_client = fake_tesla
    scheduler.state_store = store
    scheduler.mode_controller = ModeController(app_config, fake_client, store)
    return scheduler, store, fake_client, fake_tesla


def test_powerwall_evening_predrain_uses_super_when_month_is_net_exported(tmp_path):
    scheduler, store, fake_client, fake_tesla = powerwall_scheduler_with_fake_clients(
        tmp_path,
        SchedulerState(
            current_known_mode="eco",
            last_commanded_mode="eco",
            last_weather_decision=weather_snapshot("eco", "morning solar window started"),
        ),
    )

    scheduler.run_evening_low()

    assert fake_tesla.monthly_calls == 1
    assert fake_client.sent_commands == ["ascset|0,workmode,set,2"]
    assert store.load().current_known_mode == "super"


def test_powerwall_evening_conserves_when_month_is_net_imported(tmp_path):
    monthly = TeslaMonthlyEnergy(
        imported_kwh=595.9,
        exported_kwh=416.0,
        start_date="2026-06-01",
        end_date="2026-06-30",
        raw={},
    )
    scheduler, store, fake_client, _fake_tesla = powerwall_scheduler_with_fake_clients(
        tmp_path,
        SchedulerState(
            current_known_mode="super",
            last_commanded_mode="super",
            last_weather_decision=weather_snapshot("eco", "morning solar window started"),
        ),
        monthly=monthly,
    )

    scheduler.run_evening_low()

    assert fake_client.sent_commands == ["ascset|0,workmode,set,0"]
    assert store.load().current_known_mode == "eco"


def test_powerwall_gate_subtracts_only_grid_supplied_vehicle_energy(tmp_path):
    monthly = TeslaMonthlyEnergy(
        imported_kwh=60,
        exported_kwh=30,
        start_date="2026-07-01",
        end_date="2026-07-16",
        raw={},
        vehicle_grid_imported_kwh=22.9,
        vehicle_grid_source="site_energy_flow_balance",
        vehicle_grid_start_date="2026-07-12",
    )
    charging = TeslaMonthlyCharging(
        total_charged_kwh=32.3,
        eligible_charged_kwh=32.3,
        session_count=2,
        eligible_session_count=2,
        start_date="2026-07-12",
        end_date="2026-07-16",
        raw={},
        grid_charged_kwh=22.9,
        eligible_grid_charged_kwh=22.9,
        grid_source="site_energy_flow_balance",
    )
    scheduler, store, fake_client, fake_tesla = powerwall_scheduler_with_fake_clients(
        tmp_path,
        SchedulerState(current_known_mode="super", last_commanded_mode="super"),
        monthly=monthly,
        charging=charging,
    )
    object.__setattr__(
        scheduler.config,
        "energy_policy",
        EnergyPolicyConfig(
            enable_unlimited_charging_adjustment=True,
            unlimited_charging_start_date="2026-07-12",
        ),
    )

    scheduler.run_powerwall_evening_policy()

    snapshot = store.load().energy_policy_snapshot
    assert fake_tesla.charging_calls == 1
    assert fake_client.sent_commands == ["ascset|0,workmode,set,0"]
    assert snapshot["net_exported_kwh"] == -30
    assert snapshot["total_car_charging_kwh"] == pytest.approx(32.3)
    assert snapshot["car_grid_import_kwh"] == pytest.approx(22.9)
    assert snapshot["car_powerwall_or_other_kwh"] == pytest.approx(9.4)
    assert snapshot["car_charging_kwh"] == pytest.approx(22.9)
    assert snapshot["adjusted_imported_kwh"] == pytest.approx(37.1)
    assert snapshot["adjusted_net_exported_kwh"] == pytest.approx(-7.1)
    assert snapshot["car_charging_adjustment_status"] == "applied_grid_only"
    assert fake_tesla.monthly_kwargs[0]["vehicle_grid_start_date"].isoformat() == (
        "2026-07-12"
    )
    assert fake_tesla.charging_kwargs[0]["vehicle_grid_imported_kwh"] == 22.9
    assert (
        fake_tesla.charging_kwargs[0]["vehicle_grid_source"]
        == "site_energy_flow_balance"
    )


def test_powerwall_gate_does_not_subtract_total_when_grid_breakdown_is_missing(tmp_path):
    monthly = TeslaMonthlyEnergy(
        imported_kwh=60,
        exported_kwh=30,
        start_date="2026-07-01",
        end_date="2026-07-16",
        raw={},
    )
    charging = TeslaMonthlyCharging(
        total_charged_kwh=32.3,
        eligible_charged_kwh=32.3,
        session_count=2,
        eligible_session_count=2,
        start_date="2026-07-12",
        end_date="2026-07-16",
        raw={},
    )
    scheduler, store, fake_client, fake_tesla = powerwall_scheduler_with_fake_clients(
        tmp_path,
        SchedulerState(current_known_mode="super", last_commanded_mode="super"),
        monthly=monthly,
        charging=charging,
    )
    object.__setattr__(
        scheduler.config,
        "energy_policy",
        EnergyPolicyConfig(
            enable_unlimited_charging_adjustment=True,
            unlimited_charging_start_date="2026-07-12",
        ),
    )

    scheduler.run_powerwall_evening_policy()

    snapshot = store.load().energy_policy_snapshot
    assert fake_tesla.charging_calls == 1
    assert fake_client.sent_commands == ["ascset|0,workmode,set,0"]
    assert snapshot["total_car_charging_kwh"] == pytest.approx(32.3)
    assert snapshot["car_grid_import_kwh"] is None
    assert snapshot["car_charging_kwh"] is None
    assert snapshot["adjusted_imported_kwh"] == 60
    assert snapshot["adjusted_net_exported_kwh"] == -30
    assert (
        snapshot["car_charging_adjustment_status"]
        == "missing_grid_charging_breakdown"
    )


def test_powerwall_grid_vehicle_adjustment_is_capped_at_site_import(tmp_path):
    monthly = TeslaMonthlyEnergy(
        imported_kwh=10,
        exported_kwh=0,
        start_date="2026-07-01",
        end_date="2026-07-16",
        raw={},
        vehicle_grid_imported_kwh=12,
        vehicle_grid_source="site_energy_flow_balance",
        vehicle_grid_start_date="2026-07-12",
    )
    charging = TeslaMonthlyCharging(
        total_charged_kwh=14,
        eligible_charged_kwh=14,
        session_count=1,
        eligible_session_count=1,
        start_date="2026-07-12",
        end_date="2026-07-16",
        raw={},
        grid_charged_kwh=12,
        eligible_grid_charged_kwh=12,
        grid_source="site_energy_flow_balance",
    )
    scheduler, store, fake_client, _fake_tesla = powerwall_scheduler_with_fake_clients(
        tmp_path,
        SchedulerState(current_known_mode="eco", last_commanded_mode="eco"),
        monthly=monthly,
        charging=charging,
    )
    object.__setattr__(
        scheduler.config,
        "energy_policy",
        EnergyPolicyConfig(
            enable_unlimited_charging_adjustment=True,
            unlimited_charging_start_date="2026-07-12",
        ),
    )

    scheduler.run_powerwall_evening_policy()

    snapshot = store.load().energy_policy_snapshot
    assert fake_client.sent_commands == ["ascset|0,workmode,set,2"]
    assert snapshot["car_grid_import_kwh"] == 12
    assert snapshot["car_charging_kwh"] == 10
    assert snapshot["adjusted_imported_kwh"] == 0
    assert snapshot["adjusted_net_exported_kwh"] == 0


def test_powerwall_gate_leaves_car_adjustment_off_by_default(tmp_path):
    monthly = TeslaMonthlyEnergy(
        imported_kwh=100,
        exported_kwh=80,
        start_date="2026-07-01",
        end_date="2026-07-13",
        raw={},
    )
    charging = TeslaMonthlyCharging(
        total_charged_kwh=30,
        eligible_charged_kwh=30,
        session_count=2,
        eligible_session_count=2,
        start_date="2026-07-12",
        end_date="2026-07-13",
        raw={},
    )
    scheduler, store, fake_client, fake_tesla = powerwall_scheduler_with_fake_clients(
        tmp_path,
        SchedulerState(current_known_mode="super", last_commanded_mode="super"),
        monthly=monthly,
        charging=charging,
    )

    scheduler.run_powerwall_evening_policy()

    snapshot = store.load().energy_policy_snapshot
    assert fake_tesla.charging_calls == 0
    assert fake_client.sent_commands == ["ascset|0,workmode,set,0"]
    assert snapshot["adjusted_net_exported_kwh"] == -20
    assert snapshot["car_charging_adjustment_status"] == "disabled"


def test_powerwall_gate_refreshes_cache_when_grid_adjustment_settings_change(tmp_path):
    month_start = datetime.now(ZoneInfo("America/Chicago")).date().replace(day=1)
    monthly = TeslaMonthlyEnergy(
        imported_kwh=60,
        exported_kwh=30,
        start_date=month_start.isoformat(),
        end_date="2026-07-16",
        raw={},
        vehicle_grid_imported_kwh=22.9,
        vehicle_grid_source="site_energy_flow_balance",
        vehicle_grid_start_date="2026-07-12",
    )
    charging = TeslaMonthlyCharging(
        total_charged_kwh=32.3,
        eligible_charged_kwh=32.3,
        session_count=2,
        eligible_session_count=2,
        start_date="2026-07-12",
        end_date="2026-07-16",
        raw={},
        grid_charged_kwh=22.9,
        eligible_grid_charged_kwh=22.9,
        grid_source="site_energy_flow_balance",
    )
    scheduler, _store, _fake_client, fake_tesla = powerwall_scheduler_with_fake_clients(
        tmp_path,
        SchedulerState(
            current_known_mode="eco",
            last_commanded_mode="eco",
            energy_policy_snapshot={
                "month_start": month_start.isoformat(),
                "net_exported_kwh": 100,
                "adjusted_net_exported_kwh": 100,
                "car_charging_adjustment_cache_key": "grid-only-v1|False||00:00|06:00",
            },
        ),
        monthly=monthly,
        charging=charging,
    )
    object.__setattr__(
        scheduler.config,
        "energy_policy",
        EnergyPolicyConfig(
            enable_unlimited_charging_adjustment=True,
            unlimited_charging_start_date="2026-07-12",
        ),
    )

    allowed = scheduler._cheap_power_allowed(force_refresh=False)

    assert allowed is False
    assert fake_tesla.monthly_calls == 1
    assert fake_tesla.charging_calls == 1
    snapshot = scheduler.state_store.load().energy_policy_snapshot
    assert snapshot["car_charging_adjustment_cache_key"].endswith("|active")

    allowed_again = scheduler._cheap_power_allowed(force_refresh=False)

    assert allowed_again is False
    assert fake_tesla.monthly_calls == 1
    assert fake_tesla.charging_calls == 1


def test_powerwall_gate_refreshes_not_started_cache_when_plan_date_arrives(
    tmp_path,
    monkeypatch,
):
    current = [datetime(2026, 7, 11, 23, 0, tzinfo=ZoneInfo("America/Chicago"))]

    class FakeDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return current[0].astimezone(tz) if tz is not None else current[0]

    monkeypatch.setattr(scheduler_module, "datetime", FakeDateTime)
    monthly = TeslaMonthlyEnergy(
        imported_kwh=60,
        exported_kwh=30,
        start_date="2026-07-01",
        end_date="2026-07-12",
        raw={},
        vehicle_grid_imported_kwh=22.9,
        vehicle_grid_source="site_energy_flow_balance",
        vehicle_grid_start_date="2026-07-12",
    )
    charging = TeslaMonthlyCharging(
        total_charged_kwh=32.3,
        eligible_charged_kwh=32.3,
        session_count=2,
        eligible_session_count=2,
        start_date="2026-07-12",
        end_date="2026-07-13",
        raw={},
        grid_charged_kwh=22.9,
        eligible_grid_charged_kwh=22.9,
        grid_source="site_energy_flow_balance",
    )
    scheduler, store, _fake_client, fake_tesla = powerwall_scheduler_with_fake_clients(
        tmp_path,
        SchedulerState(current_known_mode="eco", last_commanded_mode="eco"),
        monthly=monthly,
        charging=charging,
    )
    object.__setattr__(
        scheduler.config,
        "energy_policy",
        EnergyPolicyConfig(
            enable_unlimited_charging_adjustment=True,
            unlimited_charging_start_date="2026-07-12",
        ),
    )

    allowed_before_start = scheduler._cheap_power_allowed(force_refresh=False)

    assert allowed_before_start is False
    assert fake_tesla.monthly_calls == 1
    assert fake_tesla.charging_calls == 0
    assert store.load().energy_policy_snapshot[
        "car_charging_adjustment_cache_key"
    ].endswith("|plan_not_started")

    current[0] = datetime(2026, 7, 12, 0, 1, tzinfo=ZoneInfo("America/Chicago"))
    allowed_after_start = scheduler._cheap_power_allowed(force_refresh=False)

    assert allowed_after_start is False
    assert fake_tesla.monthly_calls == 2
    assert fake_tesla.charging_calls == 1
    assert store.load().energy_policy_snapshot[
        "car_charging_adjustment_cache_key"
    ].endswith("|active")


def test_powerwall_guard_softoffs_at_reserve_when_month_is_net_imported(tmp_path):
    monthly = TeslaMonthlyEnergy(
        imported_kwh=10,
        exported_kwh=2,
        start_date="2026-06-01",
        end_date="2026-06-30",
        raw={},
    )
    live = TeslaLiveStatus(
        battery_percent=25,
        solar_power_w=0,
        load_power_w=0,
        grid_power_w=0,
        battery_power_w=0,
        grid_status="SystemGridConnected",
        storm_mode_enabled=False,
        raw={},
    )
    scheduler, store, fake_client, _fake_tesla = powerwall_scheduler_with_fake_clients(
        tmp_path,
        SchedulerState(current_known_mode="eco", last_commanded_mode="eco"),
        monthly=monthly,
        live=live,
    )

    scheduler.run_powerwall_battery_guard()

    assert fake_client.sent_commands[-1].startswith("ascset|0,softoff,1:")
    assert store.load().current_known_mode == "off"


def test_powerwall_guard_holds_eco_at_reserve_before_wind_hours_when_net_exported(tmp_path, monkeypatch):
    class FakeDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 6, 29, 21, 0, tzinfo=tz)

    monkeypatch.setattr(scheduler_module, "datetime", FakeDateTime)
    live = TeslaLiveStatus(
        battery_percent=25,
        solar_power_w=0,
        load_power_w=0,
        grid_power_w=0,
        battery_power_w=0,
        grid_status="SystemGridConnected",
        storm_mode_enabled=False,
        raw={},
    )
    scheduler, store, fake_client, _fake_tesla = powerwall_scheduler_with_fake_clients(
        tmp_path,
        SchedulerState(current_known_mode="super", last_commanded_mode="super"),
        live=live,
    )

    scheduler.run_powerwall_battery_guard()

    assert fake_client.sent_commands == ["ascset|0,workmode,set,0"]
    assert store.load().current_known_mode == "eco"


def test_powerwall_cheap_power_low_softoffs_until_morning(tmp_path):
    scheduler, store, fake_client, _fake_tesla = powerwall_scheduler_with_fake_clients(
        tmp_path,
        SchedulerState(current_known_mode="super", last_commanded_mode="super"),
    )

    scheduler.run_cheap_power_low()

    assert fake_client.sent_commands[-1].startswith("ascset|0,softoff,1:")
    assert store.load().current_known_mode == "off"


def test_tesla_request_budget_blocks_after_daily_limit(tmp_path):
    scheduler, store, _fake_client, fake_tesla = powerwall_scheduler_with_fake_clients(
        tmp_path,
        SchedulerState(),
    )
    object.__setattr__(
        scheduler.config,
        "energy_policy",
        EnergyPolicyConfig(max_tesla_requests_per_day=1),
    )

    assert scheduler._live_status() is not None
    assert scheduler._live_status() is None

    assert fake_tesla.live_calls == 1
    assert store.load().tesla_api_requests_today == 1


def test_mode_command_retries_while_miner_is_waking_after_softon(tmp_path, monkeypatch):
    app_config = config(tmp_path)
    object.__setattr__(
        app_config,
        "automation",
        AutomationConfig(
            dry_run=False,
            min_minutes_between_mode_changes=0,
            softon_delay_seconds=0,
            softon_mode_retry_interval_seconds=1,
            softon_mode_retry_timeout_seconds=300,
        ),
    )
    store = StateStore(app_config.state_path)
    store.save(
        SchedulerState(
            current_known_mode="off",
            last_commanded_mode="off",
            softoff_active=True,
        )
    )
    waking_response = (
        "STATUS=E,Code=120,Msg=ASC 0 set failed: idle or current mode 0 is caling. "
        "Don't permit switch mode|"
    )
    client = SequenceClient(
        [
            "STATUS=I,Code=118,Msg=success softon|",
            waking_response,
            waking_response,
            "STATUS=S,Code=0,Msg=ASC 0 set OK|",
        ]
    )
    monkeypatch.setattr(scheduler_module.time, "sleep", lambda _seconds: None)
    controller = ModeController(app_config, client, store)

    result = controller.apply_decision(Decision(TargetMode.SUPER, "cheap power window started"))

    assert result.endswith("STATUS=S,Code=0,Msg=ASC 0 set OK|")
    assert client.sent_commands[0].startswith("ascset|0,softon,1:")
    assert client.sent_commands[1:] == ["ascset|0,workmode,set,2"] * 3
    state = store.load()
    assert state.current_known_mode == "super"
    assert state.last_commanded_mode == "super"
    assert state.softoff_active is False


def test_successful_softon_is_persisted_when_mode_command_fails(tmp_path, monkeypatch):
    app_config = config(tmp_path)
    object.__setattr__(
        app_config,
        "automation",
        AutomationConfig(
            dry_run=False,
            min_minutes_between_mode_changes=0,
            softon_delay_seconds=0,
            softon_mode_retry_timeout_seconds=0,
        ),
    )
    store = StateStore(app_config.state_path)
    store.save(
        SchedulerState(
            current_known_mode="off",
            last_commanded_mode="off",
            softoff_active=True,
        )
    )
    client = SequenceClient(
        [
            "STATUS=I,Code=118,Msg=success softon|",
            (
                "STATUS=E,Code=120,Msg=ASC 0 set failed: idle or current mode 0 is caling. "
                "Don't permit switch mode|"
            ),
            "STATUS=S,Code=0,Msg=success softoff|",
        ]
    )
    monkeypatch.setattr(scheduler_module.time, "sleep", lambda _seconds: None)
    controller = ModeController(app_config, client, store)

    with pytest.raises(AvalonCommandRejected):
        controller.apply_decision(Decision(TargetMode.SUPER, "cheap power window started"))

    partially_woken_state = store.load()
    assert partially_woken_state.softoff_active is False
    assert partially_woken_state.current_known_mode is None

    controller.apply_decision(Decision(TargetMode.OFF, "cheap power window ended; returning to off"))

    assert client.sent_commands[-1].startswith("ascset|0,softoff,1:")
    assert store.load().softoff_active is True


class ImmediateStopEvent:
    def wait(self, _timeout):
        return True


class FakeBackgroundScheduler:
    def __init__(self, **_kwargs):
        self.jobs = []
        self.started = False
        self.shutdown_wait = None

    def add_job(self, func, trigger, **kwargs):
        self.jobs.append((func, trigger, kwargs))

    def start(self):
        self.started = True

    def shutdown(self, wait):
        self.shutdown_wait = wait


def patch_background_scheduler(monkeypatch):
    fake_scheduler = FakeBackgroundScheduler()
    monkeypatch.setattr(scheduler_module, "Event", ImmediateStopEvent)
    monkeypatch.setattr(
        scheduler_module,
        "BackgroundScheduler",
        lambda **_kwargs: fake_scheduler,
    )
    monkeypatch.setattr(
        AvalonScheduler,
        "_install_shutdown_handlers",
        staticmethod(lambda _event: {}),
    )
    monkeypatch.setattr(
        AvalonScheduler,
        "_restore_signal_handlers",
        staticmethod(lambda _handlers: None),
    )
    monkeypatch.setattr(AvalonScheduler, "_run_startup_catchup", lambda _self: None)
    return fake_scheduler


def test_scheduler_shutdown_event_stops_background_scheduler(tmp_path, monkeypatch):
    fake_scheduler = patch_background_scheduler(monkeypatch)

    AvalonScheduler(config(tmp_path)).start()

    assert fake_scheduler.started is True
    assert [job[2]["id"] for job in fake_scheduler.jobs] == [
        "morning-weather",
        "peak-solar-high",
        "peak-solar-low",
        "late-afternoon-bridge",
        "evening-low",
        "morning-weather-catchup",
    ]
    assert fake_scheduler.jobs[0][2]["coalesce"] is True
    assert fake_scheduler.jobs[0][2]["max_instances"] == 1
    assert fake_scheduler.jobs[0][2]["misfire_grace_time"] == 6 * 60 * 60
    assert fake_scheduler.jobs[-1][1] == "interval"
    assert fake_scheduler.jobs[-1][2]["seconds"] == 15 * 60
    assert fake_scheduler.shutdown_wait is False


def test_peak_solar_window_jobs_are_not_scheduled_when_disabled(tmp_path, monkeypatch):
    app_config = config(tmp_path)
    object.__setattr__(
        app_config,
        "automation",
        AutomationConfig(dry_run=True, enable_peak_solar_super_window=False),
    )
    fake_scheduler = patch_background_scheduler(monkeypatch)

    AvalonScheduler(app_config).start()

    assert [job[2]["id"] for job in fake_scheduler.jobs] == [
        "morning-weather",
        "late-afternoon-bridge",
        "evening-low",
        "morning-weather-catchup",
    ]


def test_late_afternoon_bridge_job_is_not_scheduled_when_disabled(tmp_path, monkeypatch):
    app_config = config(tmp_path)
    object.__setattr__(
        app_config,
        "automation",
        AutomationConfig(
            dry_run=True,
            enable_late_afternoon_solar_bridge=False,
        ),
    )
    fake_scheduler = patch_background_scheduler(monkeypatch)

    AvalonScheduler(app_config).start()

    assert [job[2]["id"] for job in fake_scheduler.jobs] == [
        "morning-weather",
        "peak-solar-high",
        "peak-solar-low",
        "evening-low",
        "morning-weather-catchup",
    ]


def test_cheap_power_window_jobs_are_scheduled_when_enabled(tmp_path, monkeypatch):
    app_config = config(tmp_path)
    object.__setattr__(
        app_config,
        "automation",
        AutomationConfig(dry_run=True, enable_cheap_power_window=True),
    )
    fake_scheduler = patch_background_scheduler(monkeypatch)

    AvalonScheduler(app_config).start()

    assert [job[2]["id"] for job in fake_scheduler.jobs] == [
        "morning-weather",
        "peak-solar-high",
        "peak-solar-low",
        "late-afternoon-bridge",
        "evening-low",
        "cheap-power-high",
        "cheap-power-low",
        "morning-weather-catchup",
    ]


def test_nest_monitor_job_is_scheduled_when_enabled(tmp_path, monkeypatch):
    app_config = config(tmp_path)
    object.__setattr__(
        app_config,
        "nest",
        NestConfig(enabled=True, poll_interval_seconds=60),
    )
    fake_scheduler = patch_background_scheduler(monkeypatch)

    AvalonScheduler(app_config).start()

    assert fake_scheduler.jobs[-1][2]["id"] == "nest-read-only-monitor"
    assert fake_scheduler.jobs[-1][1] == "interval"


def test_f2pool_monitor_job_is_scheduled_when_enabled(tmp_path, monkeypatch):
    app_config = config(tmp_path)
    object.__setattr__(
        app_config,
        "f2pool",
        F2PoolConfig(enabled=True, poll_interval_seconds=600),
    )
    fake_scheduler = patch_background_scheduler(monkeypatch)

    AvalonScheduler(app_config).start()

    assert fake_scheduler.jobs[-1][2]["id"] == "f2pool-read-only-monitor"
    assert fake_scheduler.jobs[-1][1] == "interval"
