from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


DEFAULT_CONFIG_PATH = Path("config.yaml")


@dataclass(frozen=True)
class MinerConfig:
    host: str
    port: int = 4028
    timeout_seconds: float = 5.0


@dataclass(frozen=True)
class AutomationConfig:
    enabled: bool = True
    dry_run: bool = True
    min_minutes_between_mode_changes: int = 10
    startup_catchup_enabled: bool = True
    morning_catchup_interval_seconds: int = 15 * 60
    daily_job_misfire_grace_seconds: int = 6 * 60 * 60
    enable_peak_solar_super_window: bool = True
    enable_late_afternoon_solar_bridge: bool = True
    enable_cheap_power_window: bool = False
    evening_behavior: str = "eco"
    cloudy_behavior: str = "eco"
    rainy_behavior: str = "off"
    use_standard: bool = False
    softoff_enabled: bool = True
    softon_enabled: bool = True
    softoff_uses_future_timestamp: bool = True
    softoff_delay_seconds: int = 8
    softon_delay_seconds: int = 8
    softon_mode_retry_interval_seconds: int = 15
    softon_mode_retry_timeout_seconds: int = 300
    enable_powerwall_policy: bool = False


@dataclass(frozen=True)
class ScheduleConfig:
    morning_weather_decision: str = "08:00"
    peak_solar_high: str = "11:30"
    peak_solar_low: str = "14:30"
    late_afternoon_bridge: str = "16:00"
    evening_low: str = "18:00"
    cheap_power_high: str = "00:00"
    cheap_power_low: str = "04:00"
    powerwall_guard_times: tuple[str, ...] = (
        "21:00",
        "22:00",
        "23:00",
        "23:45",
        "01:00",
        "02:00",
        "03:00",
        "03:30",
    )


@dataclass(frozen=True)
class WeatherConfig:
    provider: str = "open_meteo"
    latitude: float = 0.0
    longitude: float = 0.0
    forecast_day_start: str = "09:00"
    forecast_day_end: str = "18:00"
    peak_solar_start: str = "11:00"
    peak_solar_end: str = "15:00"
    peak_solar_min_avg_shortwave_radiation_w_m2: float = 550
    late_afternoon_bridge_start: str = "16:00"
    late_afternoon_bridge_end: str = "18:00"
    late_afternoon_bridge_min_avg_shortwave_radiation_w_m2: float = 250
    sunny_cloud_cover_max_percent: float = 40
    cloudy_cloud_cover_max_percent: float = 75
    rainy_precip_probability_min_percent: float = 50
    rainy_precip_mm_min: float = 1.0
    rainy_precip_amount_probability_min_percent: float = 20
    weather_code_precip_probability_min_percent: float = 20
    weather_code_precip_mm_min: float = 0.1


@dataclass(frozen=True)
class TeslaConfig:
    enabled: bool = False
    client_id_env: str = "TESLA_CLIENT_ID"
    client_secret_env: str = "TESLA_CLIENT_SECRET"
    token_cache_path: Path = Path("tesla_token_cache.json")
    energy_site_id: int | None = None
    redirect_uri: str = "http://localhost:8765/callback"
    auth_base_url: str = "https://auth.tesla.com"
    token_url: str = "https://fleet-auth.prd.vn.cloud.tesla.com/oauth2/v3/token"
    api_base_url: str = "https://fleet-api.prd.na.vn.cloud.tesla.com"
    timeout_seconds: float = 20


@dataclass(frozen=True)
class NestConfig:
    enabled: bool = False
    project_id: str = ""
    client_id_env: str = "NEST_CLIENT_ID"
    client_secret_env: str = "NEST_CLIENT_SECRET"
    refresh_token_env: str = "NEST_REFRESH_TOKEN"
    refresh_token_dpapi_path: Path | None = None
    readings_log_path: Path = Path("outputs/nest_readings.jsonl")
    monthly_backfill_path: Path = Path("outputs/nest_energy_dashboard_monthly_backfill.csv")
    selected_thermostat_name: str = "Hallway Thermostat"
    redirect_uri: str = "https://www.google.com"
    auth_base_url: str = "https://nestservices.google.com/partnerconnections"
    token_url: str = "https://oauth2.googleapis.com/token"
    api_base_url: str = "https://smartdevicemanagement.googleapis.com"
    timeout_seconds: float = 20
    poll_interval_seconds: int = 60
    failure_backoff_max_seconds: int = 60 * 60


@dataclass(frozen=True)
class F2PoolConfig:
    enabled: bool = False
    api_token_env: str = "F2POOL_API_TOKEN"
    mining_user_env: str = "F2POOL_MINING_USER"
    mining_user: str = ""
    currency: str = "bitcoin"
    api_base_url: str = "https://api.f2pool.com"
    api_v2_base_url: str = "https://api.f2pool.com/v2"
    network_context_enabled: bool = True
    network_context_api_base_url: str = "https://mempool.space/api/v1"
    network_context_pool_slug: str = "f2pool"
    network_status_page_url: str = "https://www.f2pool.com/coin/bitcoin"
    rewards_backfill_days: int = 14
    timeout_seconds: float = 20
    snapshots_log_path: Path = Path("outputs/f2pool_profitability.jsonl")
    historical_settlement_csv_path: Path = Path(
        "data/f2pool_btc_settlements_2025-07-01_2026-06-22.csv"
    )
    poll_interval_seconds: int = 600
    baseline_window_days: int = 14
    baseline_min_samples_per_mode: int = 24
    min_rate_sample_elapsed_minutes: int = 5
    max_rate_sample_age_minutes: int = 180
    shadow_enter_super_ratio: float = 2.0
    shadow_hold_super_ratio: float = 1.4
    shadow_exit_super_ratio: float = 1.3
    historical_seed_baseline_window_days: int = 365
    csv_seed_eco_min_hashrate_th_s: float = 50
    csv_seed_eco_max_hashrate_th_s: float = 65
    csv_seed_super_min_hashrate_th_s: float = 90
    csv_seed_super_max_hashrate_th_s: float = 110


@dataclass(frozen=True)
class EnergyPolicyConfig:
    battery_reserve_percent: float = 25.0
    monthly_net_export_min_kwh: float = 0.0
    max_tesla_requests_per_day: int = 12
    enable_evening_predrain_super: bool = True
    cheap_power_requires_monthly_net_export: bool = True
    off_at_cheap_power_end: bool = True
    enable_unlimited_charging_adjustment: bool = False
    unlimited_charging_start_date: str | None = None
    unlimited_charging_start: str = "00:00"
    unlimited_charging_end: str = "06:00"


@dataclass(frozen=True)
class AppConfig:
    timezone: str
    miner: MinerConfig
    automation: AutomationConfig
    schedule: ScheduleConfig
    weather: WeatherConfig
    tesla: TeslaConfig
    nest: NestConfig
    energy_policy: EnergyPolicyConfig
    f2pool: F2PoolConfig = field(default_factory=F2PoolConfig)
    state_path: Path = Path("state.json")
    log_path: Path = Path("avalon_q_scheduler.log")


def _read_mapping(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(
            f"Config file not found: {path}. Copy config.example.yaml to config.yaml first."
        )

    with path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    if not isinstance(data, dict):
        raise ValueError(f"Config file must contain a YAML mapping: {path}")
    return data


def load_config(path: str | Path = DEFAULT_CONFIG_PATH) -> AppConfig:
    path = Path(path)
    data = _read_mapping(path)

    miner = MinerConfig(**data.get("miner", {}))
    automation = AutomationConfig(**data.get("automation", {}))
    schedule_data = data.get("schedule", {})
    if "powerwall_guard_times" in schedule_data:
        schedule_data = {
            **schedule_data,
            "powerwall_guard_times": tuple(schedule_data["powerwall_guard_times"]),
        }
    schedule = ScheduleConfig(**schedule_data)
    weather = WeatherConfig(**data.get("weather", {}))
    tesla_data = data.get("tesla", {})
    if "token_cache_path" in tesla_data:
        tesla_data = {
            **tesla_data,
            "token_cache_path": Path(tesla_data["token_cache_path"]),
        }
    tesla = TeslaConfig(**tesla_data)
    nest_data = data.get("nest", {})
    for key in ("refresh_token_dpapi_path", "readings_log_path", "monthly_backfill_path"):
        if key in nest_data and nest_data[key] is not None:
            nest_data = {**nest_data, key: Path(nest_data[key])}
    nest = NestConfig(**nest_data)
    if nest.enabled and nest.refresh_token_dpapi_path is None:
        raise ValueError(
            "nest.refresh_token_dpapi_path is required when Nest monitoring is enabled "
            "so rotated refresh tokens can be saved automatically."
        )
    f2pool_data = data.get("f2pool", {})
    for key in ("snapshots_log_path", "historical_settlement_csv_path"):
        if key in f2pool_data:
            f2pool_data = {**f2pool_data, key: Path(f2pool_data[key])}
    f2pool = F2PoolConfig(**f2pool_data)
    energy_policy = EnergyPolicyConfig(**data.get("energy_policy", {}))

    return AppConfig(
        timezone=data.get("timezone", "America/Chicago"),
        miner=miner,
        automation=automation,
        schedule=schedule,
        weather=weather,
        tesla=tesla,
        nest=nest,
        energy_policy=energy_policy,
        f2pool=f2pool,
        state_path=Path(data.get("state_path", "state.json")),
        log_path=Path(data.get("log_path", "avalon_q_scheduler.log")),
    )
