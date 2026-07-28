from __future__ import annotations

import logging
import os
import signal
import tempfile
import time
from dataclasses import asdict
from datetime import date, datetime, time as datetime_time, timedelta
from pathlib import Path
from threading import Event
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler

from .avalon_client import AvalonClient, AvalonCommandRejected
from .config import AppConfig
from .decision_engine import Decision, DecisionEngine, TargetMode
from .f2pool_client import (
    BitcoinNetworkContextClient,
    F2PoolClient,
    F2PoolNetworkStatusClient,
    F2PoolRevenuePermissionError,
    credentials_from_env as f2pool_credentials_from_env,
    network_context_with_pool_hashrate_estimate,
    reward_settlement_summary,
    snapshot_with_network_context,
    write_reward_settlement_csv,
)
from .f2pool_profitability import (
    append_profitability_record,
    build_profitability_record,
    load_profitability_records,
    miner_mode_from_state,
)
from .nest_client import (
    NestAuthError,
    NestSdmClient,
    append_reading,
    credentials_from_env as nest_credentials_from_env,
    load_refresh_token,
    refresh_token_source_version,
)
from .state_store import SchedulerState, StateStore
from .tesla_client import TeslaFleetClient, credentials_from_env
from .weather_client import WeatherClient


LOGGER = logging.getLogger(__name__)
_NEST_AUTH_BLOCK_UNREADABLE = object()


class ModeController:
    def __init__(
        self,
        config: AppConfig,
        client: AvalonClient,
        state_store: StateStore,
    ):
        self.config = config
        self.client = client
        self.state_store = state_store

    def apply_decision(
        self,
        decision: Decision,
        *,
        dry_run: bool | None = None,
        force: bool = False,
        update_weather_decision: bool = False,
    ) -> str:
        dry_run = self.config.automation.dry_run if dry_run is None else dry_run
        state = self.state_store.load()

        if state.automation_paused:
            LOGGER.info("Automation paused; target_mode=%s reason=%s", decision.target_mode, decision.reason)
            return "automation paused"

        if not force and self._already_in_target_mode(state, decision.target_mode):
            self._save_weather_decision_if_requested(
                state,
                decision,
                update_weather_decision,
            )
            LOGGER.info("Skipping duplicate target_mode=%s reason=%s", decision.target_mode, decision.reason)
            return "already in requested mode"

        if not force and self._too_soon(state):
            self._save_weather_decision_if_requested(
                state,
                decision,
                update_weather_decision,
            )
            LOGGER.info("Skipping mode change due to min interval; target_mode=%s", decision.target_mode)
            return "minimum interval has not elapsed"

        commands = self._commands_for_transition(state, decision.target_mode)
        LOGGER.info(
            "Decision target_mode=%s reason=%s dry_run=%s commands=%s",
            decision.target_mode.value,
            decision.reason,
            dry_run,
            commands,
        )

        responses: list[str] = []
        softon_succeeded = False
        for command in commands:
            if dry_run:
                response = f"DRY RUN: would send raw TCP command: {command}"
            else:
                if softon_succeeded and self._is_mode_command(command):
                    response = self._send_mode_after_softon(
                        command,
                        decision,
                        responses,
                    )
                else:
                    response = self.client.send_command(command)
                    responses.append(response)
                    self._log_command(decision, command, response)

                if AvalonClient.response_is_error(response):
                    state.last_reason = decision.reason
                    if update_weather_decision:
                        state.last_weather_decision = self._decision_snapshot(decision)
                    state.last_api_response = "\n".join(responses)
                    self.state_store.save(state)
                    raise AvalonCommandRejected(
                        f"Avalon rejected command {command!r}: {response}"
                    )

                if command.startswith("ascset|0,softon"):
                    softon_succeeded = True
                    state.softoff_active = False
                    state.current_known_mode = None
                    state.last_reason = decision.reason
                    state.last_api_response = "\n".join(responses)
                    self.state_store.save(state)
                    LOGGER.info(
                        "Softon accepted; waiting %s seconds for scheduled wake before setting %s",
                        self.config.automation.softon_delay_seconds,
                        decision.target_mode.value,
                    )
                    time.sleep(self.config.automation.softon_delay_seconds)

            if dry_run:
                responses.append(response)
                self._log_command(decision, command, response)

        if dry_run:
            state.last_reason = decision.reason
            if update_weather_decision:
                state.last_weather_decision = self._decision_snapshot(decision)
            state.last_api_response = "\n".join(responses)
            self.state_store.save(state)
            return state.last_api_response

        state.previous_mode = state.current_known_mode or state.last_commanded_mode
        state.current_known_mode = decision.target_mode.value
        state.last_commanded_mode = decision.target_mode.value
        state.softoff_active = decision.target_mode == TargetMode.OFF
        state.last_commanded_at = datetime.now(ZoneInfo(self.config.timezone)).isoformat()
        state.last_reason = decision.reason
        if update_weather_decision:
            state.last_weather_decision = self._decision_snapshot(decision)
        state.last_api_response = "\n".join(responses)
        self.state_store.save(state)
        return state.last_api_response

    def _save_weather_decision_if_requested(
        self,
        state: SchedulerState,
        decision: Decision,
        update_weather_decision: bool,
    ) -> None:
        if not update_weather_decision:
            return
        state.last_weather_decision = self._decision_snapshot(decision)
        state.last_reason = decision.reason
        self.state_store.save(state)

    def _send_mode_after_softon(
        self,
        command: str,
        decision: Decision,
        responses: list[str],
    ) -> str:
        timeout = max(0, self.config.automation.softon_mode_retry_timeout_seconds)
        deadline = time.monotonic() + timeout

        while True:
            response = self.client.send_command(command)
            responses.append(response)
            self._log_command(decision, command, response)

            if not AvalonClient.response_is_error(response):
                return response
            if not self._is_softon_warmup_rejection(response):
                return response

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                LOGGER.error(
                    "Miner did not finish waking within %s seconds; target_mode=%s",
                    timeout,
                    decision.target_mode.value,
                )
                return response

            retry_delay = min(
                max(1, self.config.automation.softon_mode_retry_interval_seconds),
                remaining,
            )
            LOGGER.warning(
                "Miner is still waking/calibrating; retrying raw_command=%s in %.1f seconds",
                command,
                retry_delay,
            )
            time.sleep(retry_delay)

    @staticmethod
    def _is_mode_command(command: str) -> bool:
        return command.startswith("ascset|0,workmode,set,")

    @staticmethod
    def _is_softon_warmup_rejection(response: str) -> bool:
        normalized = response.lower()
        return (
            "don't permit switch mode" in normalized
            and any(marker in normalized for marker in ("caling", "calibrat", "idle"))
        )

    @staticmethod
    def _log_command(decision: Decision, command: str, response: str) -> None:
        LOGGER.info(
            "Command target_mode=%s reason=%s raw_command=%s raw_response=%s",
            decision.target_mode.value,
            decision.reason,
            command,
            response,
        )

    @staticmethod
    def _decision_snapshot(decision: Decision) -> dict:
        if decision.weather:
            return {
                "target_mode": decision.target_mode.value,
                "reason": decision.reason,
                "weather": asdict(decision.weather),
            }
        return {"target_mode": decision.target_mode.value, "reason": decision.reason}

    def _commands_for_transition(self, state: SchedulerState, target_mode: TargetMode) -> list[str]:
        commands: list[str] = []
        was_off = state.softoff_active is True or (
            state.softoff_active is None
            and (state.current_known_mode or state.last_commanded_mode) == TargetMode.OFF.value
        )

        if target_mode == TargetMode.OFF:
            if self.config.automation.softoff_enabled:
                commands.append(self._softoff_command())
            else:
                LOGGER.info("Softoff disabled; no OFF command will be sent")
            return commands

        if was_off and self.config.automation.softon_enabled:
            commands.append(self.client.commands.softon(int(time.time()) + self.config.automation.softon_delay_seconds))

        if target_mode == TargetMode.ECO:
            commands.append(self.client.commands.eco)
        elif target_mode == TargetMode.SUPER:
            commands.append(self.client.commands.super)
        else:
            raise ValueError(f"Scheduler cannot use target mode: {target_mode}")
        return commands

    @staticmethod
    def _already_in_target_mode(state: SchedulerState, target_mode: TargetMode) -> bool:
        if target_mode == TargetMode.OFF:
            # Unknown is intentionally not treated as Off. This makes an upgraded
            # installation reassert softoff once and then persist the confirmation.
            return state.softoff_active is True
        if state.softoff_active is True:
            return False
        return (state.current_known_mode or state.last_commanded_mode) == target_mode.value

    def _softoff_command(self) -> str:
        if self.config.automation.softoff_uses_future_timestamp:
            timestamp = int(time.time()) + self.config.automation.softoff_delay_seconds
            return self.client.commands.softoff(timestamp)
        return self.client.commands.softoff_legacy

    def _too_soon(self, state: SchedulerState) -> bool:
        if not state.last_commanded_at:
            return False
        last_commanded_at = datetime.fromisoformat(state.last_commanded_at)
        minimum = timedelta(minutes=self.config.automation.min_minutes_between_mode_changes)
        now = datetime.now(ZoneInfo(self.config.timezone))
        return now - last_commanded_at < minimum


class AvalonScheduler:
    def __init__(self, config: AppConfig):
        self.config = config
        self.client = AvalonClient(
            config.miner.host,
            config.miner.port,
            config.miner.timeout_seconds,
        )
        self.state_store = StateStore(config.state_path)
        self.weather_client = WeatherClient(config.weather, config.timezone)
        self.tesla_client = (
            TeslaFleetClient(config.tesla, credentials_from_env(config.tesla))
            if config.tesla.enabled
            else None
        )
        self.nest_client: NestSdmClient | None = None
        self.nest_token_source_version: str | None = None
        nest_token_path = config.nest.refresh_token_dpapi_path
        self.nest_auth_block_path = (
            nest_token_path.with_name(f"{nest_token_path.name}.auth-block")
            if nest_token_path is not None
            else config.state_path.with_name(f"{config.state_path.name}.nest-auth-block")
        )
        self.nest_blocked_token_source_version = (
            self._load_nest_auth_block()
            if config.nest.enabled
            else None
        )
        self.nest_failure_count = 0
        self.nest_retry_after: datetime | None = None
        self.f2pool_client: F2PoolClient | None = None
        self.f2pool_network_status_client: F2PoolNetworkStatusClient | None = None
        self.bitcoin_network_context_client: BitcoinNetworkContextClient | None = None
        self.decision_engine = DecisionEngine(
            config.weather,
            rainy_behavior=config.automation.rainy_behavior,
            cloudy_behavior=config.automation.cloudy_behavior,
            use_standard=config.automation.use_standard,
        )
        self.mode_controller = ModeController(config, self.client, self.state_store)

    def run_morning_weather_decision(self) -> str:
        weather = self.weather_client.get_today_forecast()
        if self.config.automation.enable_peak_solar_super_window:
            decision = self.decision_engine.morning_solar_safety_decision(weather)
        else:
            decision = self.decision_engine.morning_weather_decision(weather)
        return self.mode_controller.apply_decision(decision, update_weather_decision=True)

    def run_peak_solar_high(self) -> str:
        state = self.state_store.load()
        today = datetime.now(ZoneInfo(self.config.timezone)).date().isoformat()
        if self._morning_weather_target_mode(state, today) == TargetMode.OFF:
            decision = Decision(
                TargetMode.OFF,
                "peak solar window skipped; rainy day remains off",
            )
            return self.mode_controller.apply_decision(decision)
        else:
            weather = self.weather_client.get_today_forecast(
                window_start=self.config.weather.peak_solar_start,
                window_end=self.config.weather.peak_solar_end,
            )
            decision = self.decision_engine.peak_solar_high_decision(weather)
        return self.mode_controller.apply_decision(decision, update_weather_decision=True)

    def run_peak_solar_low(self) -> str:
        state = self.state_store.load()
        today = datetime.now(ZoneInfo(self.config.timezone)).date().isoformat()
        if self._morning_weather_target_mode(state, today) == TargetMode.OFF:
            decision = Decision(
                TargetMode.OFF,
                "peak solar window ended; rainy day remains off",
            )
        else:
            decision = self.decision_engine.peak_solar_low_decision()
        return self.mode_controller.apply_decision(decision)

    def run_late_afternoon_bridge(self) -> str:
        state = self.state_store.load()
        today = datetime.now(ZoneInfo(self.config.timezone)).date().isoformat()
        if self._morning_weather_target_mode(state, today) == TargetMode.OFF:
            decision = Decision(
                TargetMode.OFF,
                "late afternoon bridge skipped; rainy day remains off",
            )
        else:
            weather = self.weather_client.get_today_forecast(
                window_start=self.config.weather.late_afternoon_bridge_start,
                window_end=self.config.weather.late_afternoon_bridge_end,
            )
            decision = self.decision_engine.late_afternoon_bridge_decision(
                weather,
                self.config.automation.evening_behavior,
            )
        return self.mode_controller.apply_decision(decision)

    def run_evening_low(self) -> str:
        state = self.state_store.load()
        today = datetime.now(ZoneInfo(self.config.timezone)).date().isoformat()
        target_mode = self._morning_weather_target_mode(state, today)
        if target_mode == TargetMode.OFF:
            decision = Decision(TargetMode.OFF, "evening solar window ended; rainy day remains off")
        elif self.config.automation.enable_powerwall_policy:
            return self.run_powerwall_evening_policy()
        else:
            decision = self.decision_engine.evening_decision(
                self.config.automation.evening_behavior
            )
        return self.mode_controller.apply_decision(decision)

    def run_cheap_power_high(self) -> str:
        if self.config.automation.enable_powerwall_policy:
            return self.run_powerwall_cheap_power_high()

        state = self.state_store.load()
        state.mode_before_cheap_power = state.current_known_mode or state.last_commanded_mode
        self.state_store.save(state)
        return self.mode_controller.apply_decision(self.decision_engine.cheap_power_high_decision())

    def run_cheap_power_low(self) -> str:
        if self.config.automation.enable_powerwall_policy:
            return self.run_powerwall_cheap_power_low()

        state = self.state_store.load()
        if state.mode_before_cheap_power == TargetMode.OFF.value:
            decision = Decision(TargetMode.OFF, "cheap power window ended; returning to off")
        else:
            decision = self.decision_engine.cheap_power_low_decision()
        result = self.mode_controller.apply_decision(decision)
        state = self.state_store.load()
        state.mode_before_cheap_power = None
        self.state_store.save(state)
        return result

    def run_powerwall_evening_policy(self) -> str:
        allowed = self._cheap_power_allowed(force_refresh=True)
        if allowed and self.config.energy_policy.enable_evening_predrain_super:
            decision = Decision(
                TargetMode.SUPER,
                "Powerwall policy: month is net exported; evening predrain before Wind Hours",
            )
        else:
            decision = Decision(
                TargetMode.ECO,
                "Powerwall policy: month is net imported; conserving battery in Eco",
            )
        return self.mode_controller.apply_decision(decision)

    def run_powerwall_battery_guard(self) -> str:
        live = self._live_status()
        if live is None:
            return "Tesla live status unavailable or daily request budget exhausted"

        if self._grid_status_requires_off(live.grid_status):
            return self.mode_controller.apply_decision(
                Decision(TargetMode.OFF, "Powerwall policy: grid outage detected; preserving battery")
            )

        allowed = self._cheap_power_allowed(force_refresh=False)
        battery_percent = live.battery_percent
        if battery_percent is None:
            return "Tesla live status did not include battery percentage"

        if battery_percent <= self.config.energy_policy.battery_reserve_percent:
            if allowed and not self._is_cheap_power_window():
                decision = Decision(
                    TargetMode.ECO,
                    "Powerwall policy: battery at reserve before Wind Hours; holding Eco",
                )
            elif allowed and self._is_cheap_power_window():
                decision = Decision(
                    TargetMode.SUPER,
                    "Powerwall policy: Wind Hours active and monthly export budget allows Super",
                )
            else:
                decision = Decision(
                    TargetMode.OFF,
                    "Powerwall policy: battery at reserve; softoff until morning",
                )
            return self.mode_controller.apply_decision(decision)

        if allowed:
            decision = Decision(
                TargetMode.SUPER,
                "Powerwall policy: battery above reserve and monthly export budget allows Super",
            )
        else:
            decision = Decision(
                TargetMode.ECO,
                "Powerwall policy: battery above reserve but month is net imported; staying Eco",
            )
        return self.mode_controller.apply_decision(decision)

    def run_powerwall_cheap_power_high(self) -> str:
        state = self.state_store.load()
        today = datetime.now(ZoneInfo(self.config.timezone)).date()
        yesterday = (today - timedelta(days=1)).isoformat()
        if (
            self._morning_weather_target_mode(state, yesterday) == TargetMode.OFF
            and state.softoff_active is True
        ):
            return self.mode_controller.apply_decision(
                Decision(TargetMode.OFF, "Wind Hours skipped; rainy/off day remains off")
            )

        state.mode_before_cheap_power = state.current_known_mode or state.last_commanded_mode
        self.state_store.save(state)
        if self._cheap_power_allowed(force_refresh=False):
            decision = Decision(
                TargetMode.SUPER,
                "Powerwall policy: Wind Hours started and monthly export budget allows Super",
            )
        else:
            decision = Decision(
                TargetMode.ECO,
                "Powerwall policy: Wind Hours started but month is net imported; staying Eco",
            )
        return self.mode_controller.apply_decision(decision)

    def run_powerwall_cheap_power_low(self) -> str:
        target = TargetMode.OFF if self.config.energy_policy.off_at_cheap_power_end else TargetMode.ECO
        reason = (
            "Powerwall policy: Wind Hours ended; softoff until morning"
            if target == TargetMode.OFF
            else "Powerwall policy: Wind Hours ended; returning to Eco"
        )
        result = self.mode_controller.apply_decision(Decision(target, reason))
        state = self.state_store.load()
        state.mode_before_cheap_power = None
        self.state_store.save(state)
        return result

    def run_nest_log_once(self) -> str:
        now = datetime.now(ZoneInfo(self.config.timezone))
        if self.nest_retry_after and now < self.nest_retry_after:
            return f"Nest read-only poll skipped until {self.nest_retry_after.isoformat()}"
        if self.nest_blocked_token_source_version is _NEST_AUTH_BLOCK_UNREADABLE:
            self.nest_blocked_token_source_version = self._load_nest_auth_block()
            if self.nest_blocked_token_source_version is _NEST_AUTH_BLOCK_UNREADABLE:
                retry_seconds = self._record_nest_failure(now)
                return (
                    "Nest read-only poll paused; retrying the local authorization "
                    f"marker after {retry_seconds} seconds"
                )
        try:
            token_source_version = refresh_token_source_version(self.config.nest)
        except Exception as exc:  # noqa: BLE001
            if self.nest_blocked_token_source_version is not None:
                retry_seconds = self._record_nest_failure(now)
                LOGGER.warning(
                    "Nest token source could not be checked while authorization is paused; "
                    "backing off for %s seconds: %s",
                    retry_seconds,
                    exc,
                )
                return "Nest read-only poll paused until the OAuth refresh grant changes"
            retry_seconds = self._record_nest_failure(now)
            LOGGER.warning(
                "Nest token source check failed; backing off for %s seconds: %s",
                retry_seconds,
                exc,
            )
            return f"Nest read-only poll failed; retrying after {retry_seconds} seconds: {exc}"
        if self.nest_blocked_token_source_version is not None:
            if token_source_version == self.nest_blocked_token_source_version:
                return "Nest read-only poll paused until the OAuth refresh grant changes"
            LOGGER.info("Nest refresh token source changed; resuming read-only polling")
            self.nest_blocked_token_source_version = None
            try:
                self._persist_nest_auth_block(None)
            except OSError as exc:
                LOGGER.warning("Nest authorization pause marker could not be removed: %s", exc)
            self.nest_client = None
            self.nest_token_source_version = None
            self.nest_failure_count = 0
            self.nest_retry_after = None

        nest_client = None
        try:
            nest_client = self._nest_client()
            reading = nest_client.get_thermostat_reading()
            persisted_version = getattr(
                nest_client,
                "persisted_refresh_token_source_version",
                None,
            )
            if (
                persisted_version
                and refresh_token_source_version(self.config.nest) == persisted_version
            ):
                self.nest_token_source_version = persisted_version
            append_reading(self.config.nest.readings_log_path, reading)
            self.nest_failure_count = 0
            self.nest_retry_after = None
        except NestAuthError as exc:
            if exc.requires_reauthorization:
                blocked_version = (
                    getattr(
                        nest_client,
                        "persisted_refresh_token_source_version",
                        None,
                    )
                    or self.nest_token_source_version
                    or token_source_version
                )
                self.nest_blocked_token_source_version = blocked_version
                try:
                    self._persist_nest_auth_block(blocked_version)
                except OSError as persist_exc:
                    LOGGER.error(
                        "Nest authorization pause marker could not be saved: %s",
                        persist_exc,
                    )
                self.nest_client = None
                self.nest_token_source_version = None
                self.nest_failure_count = 0
                self.nest_retry_after = None
                LOGGER.error(
                    "Nest OAuth authorization requires new consent; pausing further "
                    "Google requests until the encrypted refresh token changes: %s",
                    exc,
                )
                return "Nest read-only poll paused until the OAuth refresh grant changes"
            retry_seconds = self._record_nest_failure(now)
            LOGGER.warning(
                "Nest read-only poll auth failed; backing off for %s seconds until %s: %s",
                retry_seconds,
                self.nest_retry_after.isoformat() if self.nest_retry_after else "unknown",
                exc,
            )
            return f"Nest read-only poll failed; retrying after {retry_seconds} seconds: {exc}"
        except Exception as exc:  # noqa: BLE001
            retry_seconds = self._record_nest_failure(now)
            LOGGER.warning(
                "Nest read-only poll failed; backing off for %s seconds until %s: %s",
                retry_seconds,
                self.nest_retry_after.isoformat() if self.nest_retry_after else "unknown",
                exc,
            )
            return f"Nest read-only poll failed; retrying after {retry_seconds} seconds: {exc}"

        LOGGER.info(
            "Nest reading custom_name=%s room=%s hvac_status=%s ambient_temp_c=%s ambient_humidity_pct=%s",
            reading.custom_name,
            reading.room,
            reading.hvac_status,
            reading.ambient_temp_c,
            reading.ambient_humidity_pct,
        )
        return f"Nest reading logged hvac_status={reading.hvac_status}"

    def run_f2pool_log_once(self) -> str:
        try:
            snapshot = self._f2pool_client().get_account_snapshot()
            snapshot = self._add_f2pool_network_context(snapshot)
            state = self.state_store.load()
            miner_mode, mode_source = miner_mode_from_state(state)
            record = build_profitability_record(
                snapshot=snapshot,
                history=load_profitability_records(self.config.f2pool.snapshots_log_path),
                config=self.config.f2pool,
                miner_mode=miner_mode,
                mode_source=mode_source,
                last_commanded_at=state.last_commanded_at,
            )
            append_profitability_record(self.config.f2pool.snapshots_log_path, record)
            self._sync_f2pool_rewards()
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("F2Pool read-only poll failed: %s", exc)
            return f"F2Pool read-only poll failed: {exc}"

        LOGGER.info(
            (
                "F2Pool profitability miner_mode=%s hashrate_th_s=%s value_today=%s "
                "btc_per_hour=%s btc_per_th_hour=%s baseline_samples=%s "
                "profitability_ratio=%s shadow_signal=%s shadow_reason=%s "
                "pool_hashrate_th_s=%s network_hashrate_th_s=%s "
                "current_difficulty=%s difficulty_change_percent=%s"
            ),
            record.miner_mode,
            record.snapshot.get("hashrate_th_s"),
            record.snapshot.get("value_today"),
            record.rate.get("btc_per_hour"),
            record.rate.get("btc_per_th_hour"),
            record.baseline.get("sample_count"),
            record.shadow_decision.get("profitability_ratio"),
            record.shadow_decision.get("signal"),
            record.shadow_decision.get("reason"),
            record.snapshot.get("pool_hashrate_th_s"),
            record.snapshot.get("network_hashrate_th_s"),
            record.snapshot.get("current_difficulty"),
            record.snapshot.get("difficulty_change_percent"),
        )
        return (
            "F2Pool profitability logged "
            f"signal={record.shadow_decision.get('signal')} "
            f"ratio={record.shadow_decision.get('profitability_ratio')}"
        )

    @staticmethod
    def _morning_weather_target_mode(
        state: SchedulerState,
        expected_date: str,
    ) -> TargetMode | None:
        if not state.last_weather_decision:
            return None
        weather = state.last_weather_decision.get("weather")
        if not weather or weather.get("date") != expected_date:
            return None
        target = state.last_weather_decision.get("target_mode")
        if not target:
            return None
        return TargetMode(target)

    def start(self) -> None:
        if not self.config.automation.enabled:
            LOGGER.warning("Automation is disabled in config; scheduler will not start.")
            return

        scheduler = BackgroundScheduler(timezone=ZoneInfo(self.config.timezone))
        self._add_daily_job(scheduler, self.config.schedule.morning_weather_decision, self.run_morning_weather_decision, "morning-weather")
        if self.config.automation.enable_peak_solar_super_window:
            self._add_daily_job(scheduler, self.config.schedule.peak_solar_high, self.run_peak_solar_high, "peak-solar-high")
            self._add_daily_job(scheduler, self.config.schedule.peak_solar_low, self.run_peak_solar_low, "peak-solar-low")
        else:
            LOGGER.info("Peak solar Super window disabled; morning weather can choose Super for the daytime window.")
        if self.config.automation.enable_late_afternoon_solar_bridge:
            self._add_daily_job(scheduler, self.config.schedule.late_afternoon_bridge, self.run_late_afternoon_bridge, "late-afternoon-bridge")
        else:
            LOGGER.info("Late afternoon solar bridge disabled; evening behavior waits until the normal evening job.")
        self._add_daily_job(scheduler, self.config.schedule.evening_low, self.run_evening_low, "evening-low")
        if self.config.automation.enable_powerwall_policy:
            for guard_time in self.config.schedule.powerwall_guard_times:
                self._add_daily_job(
                    scheduler,
                    guard_time,
                    self.run_powerwall_battery_guard,
                    f"powerwall-guard-{guard_time.replace(':', '')}",
                )
        if self.config.automation.enable_cheap_power_window or self.config.automation.enable_powerwall_policy:
            self._add_daily_job(scheduler, self.config.schedule.cheap_power_high, self.run_cheap_power_high, "cheap-power-high")
            self._add_daily_job(scheduler, self.config.schedule.cheap_power_low, self.run_cheap_power_low, "cheap-power-low")
        else:
            LOGGER.info("Overnight cheap-power window disabled; leaving miner in evening mode overnight.")
        if self.config.automation.startup_catchup_enabled:
            self._add_interval_job(
                scheduler,
                max(60, self.config.automation.morning_catchup_interval_seconds),
                self._run_startup_catchup,
                "morning-weather-catchup",
            )
        if self.config.nest.enabled:
            self._add_interval_job(
                scheduler,
                max(30, self.config.nest.poll_interval_seconds),
                self.run_nest_log_once,
                "nest-read-only-monitor",
            )
        if self.config.f2pool.enabled:
            self._add_interval_job(
                scheduler,
                max(60, self.config.f2pool.poll_interval_seconds),
                self.run_f2pool_log_once,
                "f2pool-read-only-monitor",
            )

        LOGGER.info("Starting Avalon Q scheduler")
        self._run_startup_catchup()
        stop_event = Event()
        previous_handlers = self._install_shutdown_handlers(stop_event)
        scheduler.start()
        try:
            while not stop_event.wait(1):
                pass
        except KeyboardInterrupt:
            LOGGER.info("Ctrl+C received; stopping Avalon Q scheduler")
        finally:
            scheduler.shutdown(wait=False)
            self._restore_signal_handlers(previous_handlers)
            LOGGER.info("Avalon Q scheduler stopped")

    @staticmethod
    def _install_shutdown_handlers(stop_event: Event) -> dict[signal.Signals, object]:
        previous_handlers: dict[signal.Signals, object] = {}

        def request_shutdown(signum, _frame) -> None:
            LOGGER.info("Shutdown signal %s received", signum)
            stop_event.set()

        for signal_name in ("SIGINT", "SIGTERM", "SIGBREAK"):
            shutdown_signal = getattr(signal, signal_name, None)
            if shutdown_signal is None:
                continue
            try:
                previous_handlers[shutdown_signal] = signal.getsignal(shutdown_signal)
                signal.signal(shutdown_signal, request_shutdown)
            except (OSError, ValueError):
                LOGGER.debug("Could not install handler for %s", signal_name)
        return previous_handlers

    @staticmethod
    def _restore_signal_handlers(previous_handlers: dict[signal.Signals, object]) -> None:
        for shutdown_signal, previous_handler in previous_handlers.items():
            try:
                signal.signal(shutdown_signal, previous_handler)
            except (OSError, ValueError):
                LOGGER.debug("Could not restore handler for signal %s", shutdown_signal)

    def _add_daily_job(self, scheduler: BackgroundScheduler, hhmm: str, func, job_id: str) -> None:
        hour_text, minute_text = hhmm.split(":", maxsplit=1)
        scheduler.add_job(
            func,
            "cron",
            hour=int(hour_text),
            minute=int(minute_text),
            id=job_id,
            replace_existing=True,
            coalesce=True,
            max_instances=1,
            misfire_grace_time=max(1, self.config.automation.daily_job_misfire_grace_seconds),
        )

    def _add_interval_job(self, scheduler: BackgroundScheduler, seconds: int, func, job_id: str) -> None:
        scheduler.add_job(
            func,
            "interval",
            seconds=seconds,
            id=job_id,
            replace_existing=True,
            coalesce=True,
            max_instances=1,
            next_run_time=datetime.now(ZoneInfo(self.config.timezone)),
        )

    def _run_startup_catchup(self) -> None:
        if not self.config.automation.startup_catchup_enabled:
            return

        now = datetime.now(ZoneInfo(self.config.timezone))
        if not self._time_has_passed(now, self.config.schedule.morning_weather_decision):
            return
        if self._time_has_passed(now, self.config.schedule.evening_low):
            return

        state = self.state_store.load()
        today = now.date().isoformat()
        if self._morning_weather_target_mode(state, today) is not None:
            return

        LOGGER.warning(
            "Morning weather decision missing for %s after scheduled time %s; running startup catch-up",
            today,
            self.config.schedule.morning_weather_decision,
        )
        try:
            self.run_morning_weather_decision()
        except Exception:  # noqa: BLE001
            LOGGER.exception("Startup catch-up morning weather decision failed")

    def _time_has_passed(self, now: datetime, hhmm: str) -> bool:
        return now.time() >= self._parse_time(hhmm)

    def _record_nest_failure(self, now: datetime) -> int:
        self.nest_failure_count += 1
        base_seconds = max(60, self.config.nest.poll_interval_seconds)
        retry_seconds = min(
            max(60, self.config.nest.failure_backoff_max_seconds),
            base_seconds * (2 ** min(self.nest_failure_count - 1, 6)),
        )
        self.nest_retry_after = now + timedelta(seconds=retry_seconds)
        return retry_seconds

    def _persist_nest_auth_block(self, token_source_version: str | None) -> None:
        if token_source_version is None:
            self.nest_auth_block_path.unlink(missing_ok=True)
            return
        self.nest_auth_block_path.parent.mkdir(parents=True, exist_ok=True)
        file_descriptor, temp_name = tempfile.mkstemp(
            prefix=f".{self.nest_auth_block_path.name}.",
            suffix=".tmp",
            dir=self.nest_auth_block_path.parent,
        )
        temp_path = Path(temp_name)
        try:
            with os.fdopen(file_descriptor, "w", encoding="utf-8") as handle:
                handle.write(token_source_version)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, self.nest_auth_block_path)
        finally:
            temp_path.unlink(missing_ok=True)

    def _load_nest_auth_block(self) -> str | None | object:
        try:
            value = self.nest_auth_block_path.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            return None
        except (OSError, UnicodeError) as exc:
            LOGGER.warning("Nest authorization pause marker could not be read: %s", exc)
            return _NEST_AUTH_BLOCK_UNREADABLE
        return value or None

    def _nest_client(self) -> NestSdmClient:
        token_source_version = refresh_token_source_version(self.config.nest)
        token_source_changed = (
            self.nest_token_source_version is not None
            and token_source_version != self.nest_token_source_version
        )
        if self.nest_client is None or token_source_changed:
            refresh_token = None
            for _attempt in range(3):
                version_before_load = refresh_token_source_version(self.config.nest)
                refresh_token = load_refresh_token(self.config.nest)
                version_after_load = refresh_token_source_version(self.config.nest)
                if version_before_load == version_after_load:
                    token_source_version = version_after_load
                    break
            else:
                raise RuntimeError(
                    "Nest refresh token changed repeatedly while it was being loaded"
                )
            assert refresh_token is not None
            self.nest_client = NestSdmClient(
                self.config.nest,
                nest_credentials_from_env(self.config.nest),
                refresh_token,
            )
            self.nest_token_source_version = token_source_version
        return self.nest_client

    def _f2pool_client(self) -> F2PoolClient:
        if self.f2pool_client is None:
            self.f2pool_client = F2PoolClient(
                self.config.f2pool,
                f2pool_credentials_from_env(self.config.f2pool),
            )
        return self.f2pool_client

    def _bitcoin_network_context_client(self) -> BitcoinNetworkContextClient:
        if self.bitcoin_network_context_client is None:
            self.bitcoin_network_context_client = BitcoinNetworkContextClient(self.config.f2pool)
        return self.bitcoin_network_context_client

    def _f2pool_network_status_client(self) -> F2PoolNetworkStatusClient:
        if self.f2pool_network_status_client is None:
            self.f2pool_network_status_client = F2PoolNetworkStatusClient(self.config.f2pool)
        return self.f2pool_network_status_client

    def _add_f2pool_network_context(self, snapshot):
        if not self.config.f2pool.network_context_enabled:
            return snapshot
        try:
            context = self._f2pool_network_status_client().get_context()
            if context.pool_hashrate_h_s is None:
                try:
                    fallback = self._bitcoin_network_context_client().get_context()
                    context = network_context_with_pool_hashrate_estimate(context, fallback)
                except Exception as fallback_exc:  # noqa: BLE001
                    LOGGER.warning("Bitcoin pool hashrate estimate failed: %s", fallback_exc)
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("F2Pool Network Status read-only poll failed: %s", exc)
            try:
                context = self._bitcoin_network_context_client().get_context()
            except Exception as fallback_exc:  # noqa: BLE001
                LOGGER.warning("Bitcoin network context read-only poll failed: %s", fallback_exc)
                return snapshot
        return snapshot_with_network_context(snapshot, context)

    def _sync_f2pool_rewards(self) -> dict[str, object]:
        try:
            settlements = self._f2pool_client().get_reward_settlements()
            write_reward_settlement_csv(
                self.config.f2pool.historical_settlement_csv_path,
                settlements,
            )
            summary = reward_settlement_summary(
                self.config.f2pool.historical_settlement_csv_path,
                settlements,
                dry_run=False,
            )
            LOGGER.info(
                "F2Pool Rewards synced rows=%s newest=%s csv=%s",
                summary.get("rows"),
                summary.get("newest_settlement_time"),
                summary.get("csv_path"),
            )
            return summary
        except F2PoolRevenuePermissionError as exc:
            LOGGER.warning(
                "F2Pool Rewards sync needs assets/transactions/list permission: %s",
                exc,
            )
            return {
                "ok": False,
                "error": "missing_f2pool_revenue_permission",
                "needed_permission": "ASSETS_MODULE assets/transactions/list",
            }
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("F2Pool Rewards sync failed: %s", exc)
            return {"ok": False, "error": "sync_failed", "message": str(exc)}

    def _cheap_power_allowed(self, *, force_refresh: bool) -> bool:
        if not self.config.energy_policy.cheap_power_requires_monthly_net_export:
            return True

        snapshot = self._monthly_energy_snapshot(force_refresh=force_refresh)
        if not snapshot:
            LOGGER.warning("Powerwall policy has no monthly energy snapshot; cheap Super disabled")
            return False
        net_exported_kwh = snapshot.get("adjusted_net_exported_kwh")
        if not isinstance(net_exported_kwh, (int, float)):
            net_exported_kwh = snapshot.get("net_exported_kwh")
        if not isinstance(net_exported_kwh, (int, float)):
            LOGGER.warning("Powerwall policy monthly snapshot has no net export value; cheap Super disabled")
            return False
        allowed = net_exported_kwh >= self.config.energy_policy.monthly_net_export_min_kwh
        LOGGER.info(
            "Powerwall monthly policy net_exported_kwh=%.3f raw_net_exported_kwh=%s "
            "total_car_charging_kwh=%s car_grid_import_kwh=%s "
            "car_powerwall_or_other_kwh=%s adjustment_status=%s "
            "min_required=%.3f allowed=%s",
            net_exported_kwh,
            snapshot.get("net_exported_kwh"),
            snapshot.get("total_car_charging_kwh"),
            snapshot.get("car_grid_import_kwh"),
            snapshot.get("car_powerwall_or_other_kwh"),
            snapshot.get("car_charging_adjustment_status"),
            self.config.energy_policy.monthly_net_export_min_kwh,
            allowed,
        )
        return allowed

    def _monthly_energy_snapshot(self, *, force_refresh: bool) -> dict | None:
        state = self.state_store.load()
        now = datetime.now(ZoneInfo(self.config.timezone))
        month_start = now.date().replace(day=1).isoformat()
        adjustment_enabled = self.config.energy_policy.enable_unlimited_charging_adjustment
        adjustment_start_date = self.config.energy_policy.unlimited_charging_start_date
        plan_start_date: date | None = None
        adjustment_config_error: str | None = None
        if not adjustment_enabled:
            adjustment_plan_state = "disabled"
        elif not adjustment_start_date:
            adjustment_config_error = "missing_start_date"
            adjustment_plan_state = adjustment_config_error
        else:
            try:
                plan_start_date = date.fromisoformat(adjustment_start_date)
            except ValueError:
                adjustment_config_error = "invalid_start_date"
                adjustment_plan_state = adjustment_config_error
            else:
                adjustment_plan_state = (
                    "active" if plan_start_date <= now.date() else "plan_not_started"
                )
        adjustment_cache_key = "|".join(
            (
                "grid-only-v1",
                str(adjustment_enabled),
                adjustment_start_date or "",
                self.config.energy_policy.unlimited_charging_start,
                self.config.energy_policy.unlimited_charging_end,
                adjustment_plan_state,
            )
        )
        snapshot = state.energy_policy_snapshot
        compatible_snapshot = (
            snapshot
            if snapshot
            and snapshot.get("month_start") == month_start
            and snapshot.get("car_charging_adjustment_cache_key")
            == adjustment_cache_key
            else None
        )
        if (
            not force_refresh
            and compatible_snapshot
        ):
            return compatible_snapshot

        if self.tesla_client is None or self.config.tesla.energy_site_id is None:
            LOGGER.warning("Tesla is not configured; no monthly energy snapshot available")
            return compatible_snapshot
        if not self._reserve_tesla_request_slot("calendar_history"):
            return compatible_snapshot

        monthly = self.tesla_client.get_monthly_energy(
            self.config.tesla.energy_site_id,
            today=now.date(),
            time_zone=self.config.timezone,
            vehicle_grid_start_date=(
                plan_start_date
                if plan_start_date is not None and plan_start_date <= now.date()
                else None
            ),
        )
        total_car_charging_kwh: float | None = None
        eligible_car_charging_kwh: float | None = None
        car_grid_import_kwh: float | None = None
        car_powerwall_or_other_kwh: float | None = None
        applied_car_grid_import_kwh: float | None = None
        adjusted_imported_kwh = monthly.imported_kwh
        adjusted_net_exported_kwh = monthly.net_exported_kwh
        adjustment_status = "disabled"
        if adjustment_enabled:
            if adjustment_config_error is not None:
                adjustment_status = adjustment_config_error
                LOGGER.warning(
                    "Unlimited charging adjustment configuration is incomplete or invalid "
                    "status=%s; using raw Tesla net energy",
                    adjustment_status,
                )
            elif plan_start_date is not None and plan_start_date > now.date():
                adjustment_status = "plan_not_started"
            elif not self._reserve_tesla_request_slot("charge_history"):
                adjustment_status = "request_budget_exhausted"
            else:
                try:
                    assert plan_start_date is not None
                    charging = self.tesla_client.get_monthly_wall_connector_charging(
                        self.config.tesla.energy_site_id,
                        today=now.date(),
                        time_zone=self.config.timezone,
                        plan_start_date=plan_start_date,
                        eligible_start_time=self._parse_time(
                            self.config.energy_policy.unlimited_charging_start
                        ),
                        eligible_end_time=self._parse_time(
                            self.config.energy_policy.unlimited_charging_end
                        ),
                        vehicle_grid_imported_kwh=monthly.vehicle_grid_imported_kwh,
                        vehicle_grid_source=monthly.vehicle_grid_source,
                    )
                    total_car_charging_kwh = charging.total_charged_kwh
                    eligible_car_charging_kwh = charging.eligible_charged_kwh
                    car_grid_import_kwh = charging.eligible_grid_charged_kwh
                    if car_grid_import_kwh is not None:
                        car_powerwall_or_other_kwh = max(
                            0.0,
                            eligible_car_charging_kwh - car_grid_import_kwh,
                        )
                    if car_grid_import_kwh is None:
                        adjustment_status = "missing_grid_charging_breakdown"
                        LOGGER.warning(
                            "Tesla energy history did not expose a complete grid-to-vehicle "
                            "breakdown; using raw Tesla net energy"
                        )
                    elif monthly.imported_kwh is None:
                        adjustment_status = "missing_grid_import"
                    else:
                        applied_car_grid_import_kwh = min(
                            monthly.imported_kwh,
                            car_grid_import_kwh,
                        )
                        adjusted_imported_kwh = max(
                            0.0,
                            monthly.imported_kwh - applied_car_grid_import_kwh,
                        )
                        adjusted_net_exported_kwh = (
                            monthly.exported_kwh - adjusted_imported_kwh
                            if monthly.exported_kwh is not None
                            else None
                        )
                        adjustment_status = "applied_grid_only"
                except (TypeError, ValueError) as exc:
                    adjustment_status = "invalid_config_or_response"
                    LOGGER.warning(
                        "Unlimited charging adjustment unavailable; using raw Tesla net energy: %s",
                        exc,
                    )
                except Exception as exc:  # noqa: BLE001
                    adjustment_status = "api_unavailable"
                    LOGGER.warning(
                        "Tesla Wall Connector charge history unavailable; using raw Tesla net energy: %s",
                        exc,
                    )
        snapshot = {
            "captured_at": now.isoformat(),
            "month_start": month_start,
            "start_date": monthly.start_date,
            "end_date": monthly.end_date,
            "imported_kwh": monthly.imported_kwh,
            "exported_kwh": monthly.exported_kwh,
            "net_exported_kwh": monthly.net_exported_kwh,
            "car_charging_kwh": applied_car_grid_import_kwh,
            "total_car_charging_kwh": total_car_charging_kwh,
            "eligible_car_charging_kwh": eligible_car_charging_kwh,
            "car_grid_import_kwh": car_grid_import_kwh,
            "car_powerwall_or_other_kwh": car_powerwall_or_other_kwh,
            "vehicle_grid_imported_kwh": monthly.vehicle_grid_imported_kwh,
            "vehicle_grid_source": monthly.vehicle_grid_source,
            "adjusted_imported_kwh": adjusted_imported_kwh,
            "adjusted_net_exported_kwh": adjusted_net_exported_kwh,
            "car_charging_adjustment_status": adjustment_status,
            "unlimited_charging_start_date": adjustment_start_date,
            "car_charging_adjustment_cache_key": adjustment_cache_key,
        }
        state = self.state_store.load()
        state.energy_policy_snapshot = snapshot
        self.state_store.save(state)
        LOGGER.info("Powerwall monthly energy snapshot=%s", snapshot)
        return snapshot

    def _live_status(self):
        if self.tesla_client is None or self.config.tesla.energy_site_id is None:
            LOGGER.warning("Tesla is not configured; no live status available")
            return None
        if not self._reserve_tesla_request_slot("live_status"):
            return None
        live = self.tesla_client.get_live_status(self.config.tesla.energy_site_id)
        LOGGER.info(
            "Powerwall live status battery_percent=%s solar_power_w=%s load_power_w=%s grid_power_w=%s grid_status=%s storm_mode=%s",
            live.battery_percent,
            live.solar_power_w,
            live.load_power_w,
            live.grid_power_w,
            live.grid_status,
            live.storm_mode_enabled,
        )
        return live

    def _reserve_tesla_request_slot(self, endpoint: str) -> bool:
        today = datetime.now(ZoneInfo(self.config.timezone)).date().isoformat()
        state = self.state_store.load()
        if state.tesla_api_usage_date != today:
            state.tesla_api_usage_date = today
            state.tesla_api_requests_today = 0
        if state.tesla_api_requests_today >= self.config.energy_policy.max_tesla_requests_per_day:
            LOGGER.warning(
                "Tesla request budget exhausted; endpoint=%s date=%s used=%s limit=%s",
                endpoint,
                today,
                state.tesla_api_requests_today,
                self.config.energy_policy.max_tesla_requests_per_day,
            )
            self.state_store.save(state)
            return False
        state.tesla_api_requests_today += 1
        self.state_store.save(state)
        LOGGER.info(
            "Tesla request budget endpoint=%s date=%s used=%s limit=%s",
            endpoint,
            today,
            state.tesla_api_requests_today,
            self.config.energy_policy.max_tesla_requests_per_day,
        )
        return True

    def _is_cheap_power_window(self) -> bool:
        now = datetime.now(ZoneInfo(self.config.timezone)).time()
        start = self._parse_time(self.config.schedule.cheap_power_high)
        end = self._parse_time(self.config.schedule.cheap_power_low)
        if start < end:
            return start <= now < end
        return now >= start or now < end

    @staticmethod
    def _grid_status_requires_off(grid_status: str | None) -> bool:
        if not grid_status:
            return False
        normalized = grid_status.lower()
        return any(marker in normalized for marker in ("outage", "island", "off_grid", "off-grid"))

    @staticmethod
    def _parse_time(hhmm: str) -> datetime_time:
        hour_text, minute_text = hhmm.split(":", maxsplit=1)
        return datetime_time(hour=int(hour_text), minute=int(minute_text))
