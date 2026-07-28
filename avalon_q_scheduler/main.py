from __future__ import annotations

import argparse
import json
import logging
import time
import webbrowser
from datetime import date, time as datetime_time

from .avalon_client import AvalonClient
from .config import load_config
from .ac_baseline import load_readings, summarize_ac_baseline
from .decision_engine import DecisionEngine
from .f2pool_client import (
    BitcoinNetworkContextClient,
    F2PoolClient,
    F2PoolNetworkStatusClient,
    F2PoolRevenuePermissionError,
    credentials_from_env as f2pool_credentials_from_env,
    mining_user_from_env_or_config,
    network_context_with_pool_hashrate_estimate,
    reward_settlement_summary,
    snapshot_with_network_context,
    write_reward_settlement_csv,
)
from .f2pool_profitability import (
    append_profitability_record,
    build_profitability_record,
    import_settlement_csv_baselines,
    load_profitability_records,
    miner_mode_from_state,
)
from .logging_setup import configure_logging
from .nest_client import (
    NestAuthError,
    NestSdmClient,
    NestTokenCache,
    append_reading,
    authorization_code_from_input,
    build_authorization_url,
    credentials_from_env as nest_credentials_from_env,
    exchange_authorization_code,
    load_monthly_backfill,
    load_refresh_token,
    save_refresh_token,
)
from .scheduler import AvalonScheduler, ModeController
from .state_store import StateStore
from .tesla_client import (
    TeslaFleetClient,
    credentials_from_env,
    run_interactive_oauth,
)
from .weather_client import WeatherClient


LOGGER = logging.getLogger(__name__)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Avalon Q weather and time-of-use scheduler")
    parser.add_argument("--config", default="config.yaml", help="Path to config YAML")
    subparsers = parser.add_subparsers(dest="command", required=True)

    for command in [
        "test-api",
        "version",
        "summary",
        "stats",
        "set-eco",
        "set-super",
        "set-standard",
        "softoff",
        "softon",
        "tesla-auth",
        "tesla-products",
        "tesla-live-status",
        "tesla-monthly-energy",
        "nest-status",
        "nest-log-once",
        "nest-baseline",
        "f2pool-account",
        "f2pool-log-once",
        "run",
    ]:
        subparsers.add_parser(command)

    tesla_charging = subparsers.add_parser("tesla-monthly-charging")
    tesla_charging.add_argument(
        "--start-date",
        default=None,
        help="Unlimited charging plan start date (YYYY-MM-DD); defaults to energy_policy config",
    )

    nest_auth = subparsers.add_parser("nest-auth")
    nest_auth.add_argument("--code", default=None, help="Authorization code returned by Google")
    nest_auth.add_argument(
        "--redirect-url",
        default=None,
        help="Full Google redirect URL containing the authorization code",
    )
    nest_auth.add_argument("--no-browser", action="store_true", help="Print the URL without opening a browser")
    subparsers.add_parser("nest-refresh-check")

    decide = subparsers.add_parser("decide-now")
    decide.add_argument("--dry-run", action="store_true", help="Do not send miner commands")
    decide.add_argument("--force", action="store_true", help="Ignore duplicate/minimum interval checks")
    f2pool_import = subparsers.add_parser("f2pool-import-settlements")
    f2pool_import.add_argument(
        "--csv",
        default=None,
        help="Path to F2Pool BTC settlement CSV; defaults to f2pool.historical_settlement_csv_path",
    )
    f2pool_import.add_argument("--dry-run", action="store_true", help="Show import summary without writing")
    f2pool_sync = subparsers.add_parser("f2pool-sync-rewards")
    f2pool_sync.add_argument(
        "--days",
        type=int,
        default=None,
        help="Number of recent days to fetch; defaults to f2pool.rewards_backfill_days",
    )
    f2pool_sync.add_argument("--dry-run", action="store_true", help="Fetch and summarize without writing CSV")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    config = load_config(args.config)
    configure_logging(config.log_path)

    client = AvalonClient(
        config.miner.host,
        config.miner.port,
        config.miner.timeout_seconds,
    )

    if args.command == "test-api":
        for label, func in [
            ("version", client.get_version),
            ("summary", client.get_summary),
            ("stats", client.get_stats),
        ]:
            print(f"\n--- {label} ---")
            print(func())
        return 0

    if args.command == "tesla-auth":
        credentials = credentials_from_env(config.tesla)
        token_cache = run_interactive_oauth(config.tesla, credentials)
        TeslaFleetClient.save_token_cache(config.tesla.token_cache_path, token_cache)
        print(f"Tesla token cache saved to {config.tesla.token_cache_path}")
        print("Access and refresh tokens were not printed.")
        return 0

    if args.command == "tesla-products":
        client = TeslaFleetClient(config.tesla, credentials_from_env(config.tesla))
        products = client.get_products()
        summaries = []
        response = products.get("response", [])
        if isinstance(response, list):
            for item in response:
                if isinstance(item, dict):
                    summaries.append(
                        {
                            "resource_type": item.get("resource_type"),
                            "energy_site_id": item.get("energy_site_id"),
                            "site_name": item.get("site_name"),
                            "id": item.get("id"),
                        }
                    )
        print(json.dumps(summaries, indent=2))
        return 0

    if args.command == "tesla-live-status":
        if config.tesla.energy_site_id is None:
            raise ValueError("Set tesla.energy_site_id in config.yaml before running tesla-live-status.")
        client = TeslaFleetClient(config.tesla, credentials_from_env(config.tesla))
        live = client.get_live_status(config.tesla.energy_site_id)
        print(
            json.dumps(
                {
                    "battery_percent": live.battery_percent,
                    "solar_power_w": live.solar_power_w,
                    "load_power_w": live.load_power_w,
                    "grid_power_w": live.grid_power_w,
                    "battery_power_w": live.battery_power_w,
                    "grid_status": live.grid_status,
                    "storm_mode_enabled": live.storm_mode_enabled,
                },
                indent=2,
            )
        )
        return 0

    if args.command == "tesla-monthly-energy":
        if config.tesla.energy_site_id is None:
            raise ValueError("Set tesla.energy_site_id in config.yaml before running tesla-monthly-energy.")
        client = TeslaFleetClient(config.tesla, credentials_from_env(config.tesla))
        monthly = client.get_monthly_energy(
            config.tesla.energy_site_id,
            today=date.today(),
            time_zone=config.timezone,
        )
        print(
            json.dumps(
                {
                    "start_date": monthly.start_date,
                    "end_date": monthly.end_date,
                    "imported_kwh": monthly.imported_kwh,
                    "exported_kwh": monthly.exported_kwh,
                    "net_exported_kwh": monthly.net_exported_kwh,
                },
                indent=2,
            )
        )
        return 0

    if args.command == "nest-auth":
        credentials = nest_credentials_from_env(config.nest)
        auth_url = build_authorization_url(config.nest, credentials)
        print("Open this Nest Device Access URL if your browser does not open automatically:")
        print(auth_url)
        if not args.no_browser:
            webbrowser.open(auth_url)
        raw_code = args.code or args.redirect_url
        if not raw_code:
            raw_code = input("Paste the authorization code or full redirected URL: ")
        token_response = exchange_authorization_code(
            config.nest,
            credentials,
            authorization_code_from_input(raw_code),
        )
        if not token_response.refresh_token:
            raise NestAuthError(
                "Google did not return a Nest refresh token. Re-run nest-auth and keep prompt=consent in the URL."
            )
        saved_to = save_refresh_token(config.nest, token_response.refresh_token)
        nest_client = NestSdmClient(
            config.nest,
            credentials,
            token_response.refresh_token,
            NestTokenCache(
                access_token=token_response.access_token,
                expires_at=token_response.expires_at,
            ),
        )
        devices = nest_client.get_devices()
        print(
            json.dumps(
                {
                    "ok": True,
                    "refresh_token_saved_to": saved_to,
                    "refresh_token_printed": False,
                    "access_token_printed": False,
                    "devices_listed": len(devices),
                },
                indent=2,
            )
        )
        return 0

    if args.command == "tesla-monthly-charging":
        if config.tesla.energy_site_id is None:
            raise ValueError(
                "Set tesla.energy_site_id in config.yaml before running tesla-monthly-charging."
            )
        start_date_text = (
            args.start_date or config.energy_policy.unlimited_charging_start_date
        )
        if not start_date_text:
            raise ValueError(
                "Pass --start-date YYYY-MM-DD or set "
                "energy_policy.unlimited_charging_start_date."
            )
        plan_start_date = date.fromisoformat(start_date_text)
        client = TeslaFleetClient(config.tesla, credentials_from_env(config.tesla))
        monthly = client.get_monthly_energy(
            config.tesla.energy_site_id,
            today=date.today(),
            time_zone=config.timezone,
            vehicle_grid_start_date=plan_start_date,
        )
        charging = client.get_monthly_wall_connector_charging(
            config.tesla.energy_site_id,
            today=date.today(),
            time_zone=config.timezone,
            plan_start_date=plan_start_date,
            eligible_start_time=datetime_time.fromisoformat(
                config.energy_policy.unlimited_charging_start
            ),
            eligible_end_time=datetime_time.fromisoformat(
                config.energy_policy.unlimited_charging_end
            ),
            vehicle_grid_imported_kwh=monthly.vehicle_grid_imported_kwh,
            vehicle_grid_source=monthly.vehicle_grid_source,
        )
        eligible_non_grid_kwh = (
            max(
                0.0,
                charging.eligible_charged_kwh
                - charging.eligible_grid_charged_kwh,
            )
            if charging.eligible_grid_charged_kwh is not None
            else None
        )
        print(
            json.dumps(
                {
                    "start_date": charging.start_date,
                    "end_date": charging.end_date,
                    "total_charged_kwh": charging.total_charged_kwh,
                    "eligible_charged_kwh": charging.eligible_charged_kwh,
                    "grid_charged_kwh": charging.grid_charged_kwh,
                    "eligible_grid_charged_kwh": charging.eligible_grid_charged_kwh,
                    "eligible_powerwall_or_other_kwh": eligible_non_grid_kwh,
                    "grid_source": charging.grid_source,
                    "session_count": charging.session_count,
                    "eligible_session_count": charging.eligible_session_count,
                    "eligible_window": {
                        "start": config.energy_policy.unlimited_charging_start,
                        "end": config.energy_policy.unlimited_charging_end,
                    },
                },
                indent=2,
            )
        )
        return 0

    if args.command == "nest-refresh-check":
        nest_client = _build_nest_client(config)
        token_cache = nest_client.refresh_access_token()
        print(
            json.dumps(
                {
                    "ok": bool(token_cache.access_token),
                    "expires_at": token_cache.expires_at,
                    "access_token_printed": False,
                },
                indent=2,
            )
        )
        return 0

    if args.command == "nest-status":
        reading = _build_nest_client(config).get_thermostat_reading()
        print(json.dumps(reading.to_json_dict(), indent=2, sort_keys=True))
        return 0

    if args.command == "nest-log-once":
        reading = _build_nest_client(config).get_thermostat_reading()
        append_reading(config.nest.readings_log_path, reading)
        print(
            json.dumps(
                {
                    "logged_to": str(config.nest.readings_log_path),
                    "custom_name": reading.custom_name,
                    "hvac_status": reading.hvac_status,
                    "ambient_temp_c": reading.ambient_temp_c,
                    "ambient_temp_f": reading.ambient_temp_f,
                    "ambient_humidity_pct": reading.ambient_humidity_pct,
                },
                indent=2,
            )
        )
        return 0

    if args.command == "nest-baseline":
        latest = None
        if config.nest.enabled:
            try:
                latest = _build_nest_client(config).get_thermostat_reading()
            except Exception as exc:  # noqa: BLE001
                LOGGER.warning("Could not fetch live Nest reading for baseline summary: %s", exc)
        summary = summarize_ac_baseline(
            monthly_backfill=load_monthly_backfill(config.nest.monthly_backfill_path),
            readings=load_readings(config.nest.readings_log_path),
            latest_reading=latest,
        )
        print(json.dumps(summary, indent=2, sort_keys=True))
        return 0

    if args.command == "f2pool-account":
        snapshot = _build_f2pool_client(config).get_account_snapshot()
        snapshot = _attach_network_context(config, snapshot)
        print(json.dumps(snapshot.to_summary_dict(), indent=2, sort_keys=True))
        return 0

    if args.command == "f2pool-log-once":
        f2pool_client = _build_f2pool_client(config)
        snapshot = f2pool_client.get_account_snapshot()
        snapshot = _attach_network_context(config, snapshot)
        state = StateStore(config.state_path).load()
        miner_mode, mode_source = miner_mode_from_state(state)
        record = build_profitability_record(
            snapshot=snapshot,
            history=load_profitability_records(config.f2pool.snapshots_log_path),
            config=config.f2pool,
            miner_mode=miner_mode,
            mode_source=mode_source,
            last_commanded_at=state.last_commanded_at,
        )
        append_profitability_record(config.f2pool.snapshots_log_path, record)
        summary = {
            **record.to_summary_dict(),
            "logged_to": str(config.f2pool.snapshots_log_path),
            "rewards_sync": _sync_f2pool_rewards(config, f2pool_client),
        }
        print(json.dumps(summary, indent=2, sort_keys=True))
        return 0

    if args.command == "f2pool-sync-rewards":
        f2pool_client = _build_f2pool_client(config)
        try:
            settlements = f2pool_client.get_reward_settlements(days=args.days)
        except F2PoolRevenuePermissionError as exc:
            print(
                json.dumps(
                    {
                        "ok": False,
                        "error": "missing_f2pool_revenue_permission",
                        "message": str(exc),
                        "needed_permission": "ASSETS_MODULE assets/transactions/list",
                        "token_printed": False,
                        "raw_payload_printed": False,
                    },
                    indent=2,
                    sort_keys=True,
                )
            )
            return 2
        if not args.dry_run:
            write_reward_settlement_csv(config.f2pool.historical_settlement_csv_path, settlements)
        print(
            json.dumps(
                reward_settlement_summary(
                    config.f2pool.historical_settlement_csv_path,
                    settlements,
                    dry_run=args.dry_run,
                ),
                indent=2,
                sort_keys=True,
            )
        )
        return 0

    if args.command == "f2pool-import-settlements":
        csv_path = args.csv or config.f2pool.historical_settlement_csv_path
        summary = import_settlement_csv_baselines(
            csv_path=csv_path,
            output_path=config.f2pool.snapshots_log_path,
            config=config.f2pool,
            currency=config.f2pool.currency,
            mining_user=mining_user_from_env_or_config(config.f2pool),
            dry_run=args.dry_run,
        )
        print(json.dumps(summary, indent=2, sort_keys=True))
        return 0

    read_only = {
        "version": client.get_version,
        "summary": client.get_summary,
        "stats": client.get_stats,
    }
    if args.command in read_only:
        print(read_only[args.command]())
        return 0

    if args.command in {"set-eco", "set-super", "set-standard", "softoff", "softon"}:
        command_map = {
            "set-eco": client.commands.eco,
            "set-super": client.commands.super,
            "set-standard": client.commands.standard,
        }
        if args.command == "softoff":
            if config.automation.softoff_uses_future_timestamp:
                raw_command = client.commands.softoff(
                    int(time.time()) + config.automation.softoff_delay_seconds
                )
            else:
                raw_command = client.commands.softoff_legacy
        elif args.command == "softon":
            raw_command = client.commands.softon(
                int(time.time()) + config.automation.softon_delay_seconds
            )
        else:
            raw_command = command_map[args.command]

        if config.automation.dry_run:
            message = f"DRY RUN: would send raw TCP command: {raw_command}"
            LOGGER.info(message)
            print(message)
        else:
            LOGGER.info("Manual command raw_command=%s", raw_command)
            print(client.send_command(raw_command))
        return 0

    if args.command == "decide-now":
        weather = WeatherClient(config.weather, config.timezone).get_today_forecast()
        decision_engine = DecisionEngine(
            config.weather,
            rainy_behavior=config.automation.rainy_behavior,
            cloudy_behavior=config.automation.cloudy_behavior,
            use_standard=config.automation.use_standard,
        )
        if config.automation.enable_peak_solar_super_window:
            decision = decision_engine.morning_solar_safety_decision(weather)
        else:
            decision = decision_engine.morning_weather_decision(weather)
        dry_run = True if args.dry_run else config.automation.dry_run
        controller = ModeController(config, client, StateStore(config.state_path))
        result = controller.apply_decision(
            decision,
            dry_run=dry_run,
            force=args.force,
            update_weather_decision=True,
        )
        print(f"Decision: {decision.target_mode.value} ({decision.reason})")
        print(result)
        return 0

    if args.command == "run":
        AvalonScheduler(config).start()
        return 0

    raise ValueError(f"Unhandled command: {args.command}")


def _build_nest_client(config) -> NestSdmClient:
    return NestSdmClient(
        config.nest,
        nest_credentials_from_env(config.nest),
        load_refresh_token(config.nest),
    )


def _build_f2pool_client(config) -> F2PoolClient:
    return F2PoolClient(config.f2pool, f2pool_credentials_from_env(config.f2pool))


def _attach_network_context(config, snapshot):
    if not config.f2pool.network_context_enabled:
        return snapshot
    try:
        context = F2PoolNetworkStatusClient(config.f2pool).get_context()
        if context.pool_hashrate_h_s is None:
            try:
                fallback = BitcoinNetworkContextClient(config.f2pool).get_context()
                context = network_context_with_pool_hashrate_estimate(context, fallback)
            except Exception as fallback_exc:  # noqa: BLE001
                LOGGER.warning("Bitcoin pool hashrate estimate failed: %s", fallback_exc)
    except Exception as exc:  # noqa: BLE001
        LOGGER.warning("F2Pool Network Status read-only poll failed: %s", exc)
        try:
            context = BitcoinNetworkContextClient(config.f2pool).get_context()
        except Exception as fallback_exc:  # noqa: BLE001
            LOGGER.warning("Bitcoin network context read-only poll failed: %s", fallback_exc)
            return snapshot
    return snapshot_with_network_context(snapshot, context)


def _sync_f2pool_rewards(config, client: F2PoolClient) -> dict[str, object]:
    try:
        settlements = client.get_reward_settlements()
        write_reward_settlement_csv(config.f2pool.historical_settlement_csv_path, settlements)
        return reward_settlement_summary(
            config.f2pool.historical_settlement_csv_path,
            settlements,
            dry_run=False,
        )
    except F2PoolRevenuePermissionError as exc:
        LOGGER.warning("F2Pool Rewards sync needs assets/transactions/list permission: %s", exc)
        return {
            "ok": False,
            "error": "missing_f2pool_revenue_permission",
            "needed_permission": "ASSETS_MODULE assets/transactions/list",
        }
    except Exception as exc:  # noqa: BLE001
        LOGGER.warning("F2Pool Rewards sync failed: %s", exc)
        return {"ok": False, "error": "sync_failed", "message": str(exc)}


if __name__ == "__main__":
    raise SystemExit(main())
