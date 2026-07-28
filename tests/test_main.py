import sys

import avalon_q_scheduler.main as main_module
from avalon_q_scheduler.avalon_client import AvalonCommands
from avalon_q_scheduler.config import (
    AppConfig,
    AutomationConfig,
    EnergyPolicyConfig,
    MinerConfig,
    NestConfig,
    ScheduleConfig,
    TeslaConfig,
    WeatherConfig,
)
from avalon_q_scheduler.state_store import StateStore
from avalon_q_scheduler.weather_client import DayWeather


class FakeClient:
    def __init__(self):
        self.commands = AvalonCommands()
        self.sent_commands: list[str] = []

    def send_command(self, command: str) -> str:
        self.sent_commands.append(command)
        return "STATUS=I,Code=118,Msg=success softoff|"

    def get_version(self) -> str:
        return "version"

    def get_summary(self) -> str:
        return "summary"

    def get_stats(self) -> str:
        return "stats"


def test_manual_softoff_builds_timestamped_command(tmp_path, monkeypatch):
    config = AppConfig(
        timezone="America/Chicago",
        miner=MinerConfig(host="192.0.2.1"),
        automation=AutomationConfig(dry_run=False, softoff_delay_seconds=8),
        schedule=ScheduleConfig(),
        weather=WeatherConfig(),
        tesla=TeslaConfig(),
        nest=NestConfig(),
        energy_policy=EnergyPolicyConfig(),
        state_path=tmp_path / "state.json",
        log_path=tmp_path / "test.log",
    )
    client = FakeClient()
    monkeypatch.setattr(main_module, "load_config", lambda _path: config)
    monkeypatch.setattr(main_module, "configure_logging", lambda _path: None)
    monkeypatch.setattr(main_module, "AvalonClient", lambda *_args: client)
    monkeypatch.setattr(main_module.time, "time", lambda: 1_000)
    monkeypatch.setattr(sys, "argv", ["avalon-q-scheduler", "softoff"])

    assert main_module.main() == 0
    assert client.sent_commands == ["ascset|0,softoff,1:1008"]


def test_decide_now_saves_the_weather_decision(tmp_path, monkeypatch):
    config = AppConfig(
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
    forecast = DayWeather(
        date="2026-06-23",
        avg_cloud_cover_percent=20,
        max_precip_probability_percent=0,
        total_precip_mm=0,
        weather_codes=[0],
        raw={},
    )
    client = FakeClient()
    monkeypatch.setattr(main_module, "load_config", lambda _path: config)
    monkeypatch.setattr(main_module, "configure_logging", lambda _path: None)
    monkeypatch.setattr(main_module, "AvalonClient", lambda *_args: client)
    monkeypatch.setattr(
        main_module,
        "WeatherClient",
        lambda *_args: type(
            "FakeWeatherClient",
            (),
            {"get_today_forecast": lambda self: forecast},
        )(),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["avalon-q-scheduler", "decide-now", "--dry-run", "--force"],
    )

    assert main_module.main() == 0
    saved = StateStore(config.state_path).load().last_weather_decision
    assert saved is not None
    assert saved["target_mode"] == "eco"
    assert saved["weather"]["date"] == "2026-06-23"
