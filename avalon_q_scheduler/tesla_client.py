from __future__ import annotations

import json
import os
import secrets
import threading
import time
import urllib.parse
import webbrowser
from dataclasses import asdict, dataclass
from datetime import date, datetime, time as datetime_time, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from .config import TeslaConfig


TESLA_SCOPES = "openid offline_access energy_device_data"


@dataclass(frozen=True)
class TeslaCredentials:
    client_id: str
    client_secret: str


@dataclass
class TeslaTokenCache:
    access_token: str | None = None
    refresh_token: str | None = None
    expires_at: float = 0

    @property
    def access_token_valid(self) -> bool:
        return bool(self.access_token) and time.time() < self.expires_at - 120


@dataclass(frozen=True)
class TeslaLiveStatus:
    battery_percent: float | None
    solar_power_w: float | None
    load_power_w: float | None
    grid_power_w: float | None
    battery_power_w: float | None
    grid_status: str | None
    storm_mode_enabled: bool | None
    raw: dict[str, Any]


@dataclass(frozen=True)
class TeslaMonthlyEnergy:
    imported_kwh: float | None
    exported_kwh: float | None
    start_date: str
    end_date: str
    raw: dict[str, Any]
    vehicle_grid_imported_kwh: float | None = None
    vehicle_grid_source: str | None = None
    vehicle_grid_start_date: str | None = None

    @property
    def net_exported_kwh(self) -> float | None:
        if self.imported_kwh is None or self.exported_kwh is None:
            return None
        return self.exported_kwh - self.imported_kwh


@dataclass(frozen=True)
class TeslaMonthlyCharging:
    total_charged_kwh: float
    eligible_charged_kwh: float
    session_count: int
    eligible_session_count: int
    start_date: str
    end_date: str
    raw: dict[str, Any]
    grid_charged_kwh: float | None = None
    eligible_grid_charged_kwh: float | None = None
    grid_source: str | None = None


class OAuthCallbackState:
    def __init__(self, expected_state: str):
        self.expected_state = expected_state
        self.code: str | None = None
        self.error: str | None = None
        self.event = threading.Event()


def credentials_from_env(config: TeslaConfig) -> TeslaCredentials:
    client_id = os.environ.get(config.client_id_env, "").strip()
    client_secret = os.environ.get(config.client_secret_env, "").strip()
    if not client_id or not client_secret:
        raise ValueError(
            f"Set {config.client_id_env} and {config.client_secret_env} before using Tesla Fleet API."
        )
    return TeslaCredentials(client_id=client_id, client_secret=client_secret)


def run_interactive_oauth(config: TeslaConfig, credentials: TeslaCredentials) -> TeslaTokenCache:
    oauth_state = secrets.token_urlsafe(24)
    callback = OAuthCallbackState(oauth_state)
    parsed_redirect = urllib.parse.urlparse(config.redirect_uri)
    if parsed_redirect.hostname not in {"localhost", "127.0.0.1"}:
        raise ValueError("Interactive auth only supports a localhost redirect URI.")

    server = ThreadingHTTPServer(
        (parsed_redirect.hostname or "localhost", parsed_redirect.port or 80),
        _make_oauth_handler(callback, parsed_redirect.path or "/callback"),
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    params = {
        "client_id": credentials.client_id,
        "locale": "en-US",
        "prompt": "login",
        "redirect_uri": config.redirect_uri,
        "response_type": "code",
        "scope": TESLA_SCOPES,
        "state": oauth_state,
        "prompt_missing_scopes": "true",
        "require_requested_scopes": "true",
    }
    auth_url = f"{config.auth_base_url}/oauth2/v3/authorize?{urllib.parse.urlencode(params)}"
    print("Open this Tesla OAuth URL if your browser does not open automatically:")
    print(auth_url)
    webbrowser.open(auth_url)

    try:
        if not callback.event.wait(timeout=300):
            raise TimeoutError("Timed out waiting for Tesla OAuth callback.")
    finally:
        server.shutdown()
        server.server_close()

    if callback.error:
        raise RuntimeError(callback.error)
    if not callback.code:
        raise RuntimeError("Tesla OAuth callback did not include a code.")

    client = TeslaFleetClient(config, credentials)
    return client.exchange_authorization_code(callback.code)


def _make_oauth_handler(
    state: OAuthCallbackState,
    callback_path: str,
) -> type[BaseHTTPRequestHandler]:
    class OAuthHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            parsed = urllib.parse.urlparse(self.path)
            qs = urllib.parse.parse_qs(parsed.query)
            if parsed.path != callback_path:
                self.send_response(404)
                self.end_headers()
                self.wfile.write(b"Not found")
                return

            returned_state = qs.get("state", [""])[0]
            code = qs.get("code", [""])[0]
            error = qs.get("error", [""])[0]
            if error:
                state.error = error
            elif returned_state != state.expected_state:
                state.error = "OAuth state did not match; refusing callback."
            elif not code:
                state.error = "OAuth callback did not include an authorization code."
            else:
                state.code = code

            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            if state.code:
                self.wfile.write(b"<html><body><h1>Tesla OAuth captured.</h1><p>You can close this tab.</p></body></html>")
            else:
                message = (state.error or "Unknown OAuth error").replace("<", "&lt;")
                self.wfile.write(f"<html><body><h1>OAuth failed.</h1><p>{message}</p></body></html>".encode())
            state.event.set()

        def log_message(self, fmt: str, *args: object) -> None:
            return

    return OAuthHandler


class TeslaFleetClient:
    def __init__(
        self,
        config: TeslaConfig,
        credentials: TeslaCredentials | None = None,
        token_cache: TeslaTokenCache | None = None,
    ):
        self.config = config
        self.credentials = credentials
        self.token_cache = token_cache or self.load_token_cache(config.token_cache_path)

    def exchange_authorization_code(self, code: str) -> TeslaTokenCache:
        if self.credentials is None:
            raise ValueError("Tesla credentials are required for OAuth token exchange.")
        payload = {
            "grant_type": "authorization_code",
            "client_id": self.credentials.client_id,
            "client_secret": self.credentials.client_secret,
            "code": code,
            "audience": self.config.api_base_url,
            "redirect_uri": self.config.redirect_uri,
        }
        data = self._post_token(payload)
        return self._cache_from_token_response(data)

    def refresh_access_token(self) -> TeslaTokenCache:
        if self.credentials is None:
            raise ValueError("Tesla credentials are required to refresh tokens.")
        if not self.token_cache.refresh_token:
            raise ValueError("Tesla token cache has no refresh token; run tesla-auth.")
        payload = {
            "grant_type": "refresh_token",
            "client_id": self.credentials.client_id,
            "client_secret": self.credentials.client_secret,
            "refresh_token": self.token_cache.refresh_token,
            "audience": self.config.api_base_url,
        }
        data = self._post_token(payload)
        cache = self._cache_from_token_response(data)
        if not cache.refresh_token:
            cache.refresh_token = self.token_cache.refresh_token
        self.token_cache = cache
        self.save_token_cache(self.config.token_cache_path, cache)
        return cache

    def get_products(self) -> dict[str, Any]:
        return self._get_json("/api/1/products")

    def get_live_status(self, energy_site_id: int) -> TeslaLiveStatus:
        raw = self._get_json(f"/api/1/energy_sites/{energy_site_id}/live_status")
        response = _response_mapping(raw)
        return TeslaLiveStatus(
            battery_percent=_number_from_keys(
                response,
                "percentage_charged",
                "state_of_energy",
                "battery_percentage",
                "battery_percent",
            ),
            solar_power_w=_number_from_keys(response, "solar_power"),
            load_power_w=_number_from_keys(response, "load_power"),
            grid_power_w=_number_from_keys(response, "grid_power"),
            battery_power_w=_number_from_keys(response, "battery_power"),
            grid_status=_string_from_keys(response, "grid_status"),
            storm_mode_enabled=_bool_from_keys(
                response,
                "storm_mode_enabled",
                "storm_mode_active",
                "storm_mode",
            ),
            raw=raw,
        )

    def get_monthly_energy(
        self,
        energy_site_id: int,
        *,
        today: date,
        time_zone: str,
        vehicle_grid_start_date: date | None = None,
    ) -> TeslaMonthlyEnergy:
        start = today.replace(day=1)
        end = today + timedelta(days=1)
        zone = ZoneInfo(time_zone)
        start_at = datetime.combine(start, datetime_time.min, tzinfo=zone).isoformat()
        end_at = datetime.combine(end, datetime_time.min, tzinfo=zone).isoformat()
        params = urllib.parse.urlencode(
            {
                "kind": "energy",
                "period": "day",
                "start_date": start_at,
                "end_date": end_at,
                "time_zone": time_zone,
            }
        )
        raw = self._get_json(
            f"/api/1/energy_sites/{energy_site_id}/calendar_history?{params}"
        )
        response = raw.get("response", raw)
        imported_wh, exported_wh = _extract_grid_import_export_wh(response)
        effective_vehicle_start = (
            max(start, vehicle_grid_start_date)
            if vehicle_grid_start_date is not None
            else start
        )
        vehicle_grid_wh, vehicle_grid_source = _extract_vehicle_grid_import_wh(
            response,
            start_date=(effective_vehicle_start if effective_vehicle_start > start else None),
        )
        return TeslaMonthlyEnergy(
            imported_kwh=(imported_wh / 1000 if imported_wh is not None else None),
            exported_kwh=(exported_wh / 1000 if exported_wh is not None else None),
            start_date=start.isoformat(),
            end_date=end.isoformat(),
            raw=raw,
            vehicle_grid_imported_kwh=(
                vehicle_grid_wh / 1000 if vehicle_grid_wh is not None else None
            ),
            vehicle_grid_source=vehicle_grid_source,
            vehicle_grid_start_date=effective_vehicle_start.isoformat(),
        )

    def get_monthly_wall_connector_charging(
        self,
        energy_site_id: int,
        *,
        today: date,
        time_zone: str,
        plan_start_date: date,
        eligible_start_time: datetime_time = datetime_time(0, 0),
        eligible_end_time: datetime_time = datetime_time(6, 0),
        vehicle_grid_imported_kwh: float | None = None,
        vehicle_grid_source: str | None = None,
    ) -> TeslaMonthlyCharging:
        start = max(today.replace(day=1), plan_start_date)
        end = today + timedelta(days=1)
        zone = ZoneInfo(time_zone)
        start_at = datetime.combine(start, datetime_time.min, tzinfo=zone).isoformat()
        end_at = datetime.combine(end, datetime_time.min, tzinfo=zone).isoformat()
        params = urllib.parse.urlencode(
            {
                "kind": "charge",
                "start_date": start_at,
                "end_date": end_at,
                "time_zone": time_zone,
            }
        )
        raw = self._get_json(
            f"/api/1/energy_sites/{energy_site_id}/telemetry_history?{params}"
        )
        total_wh, eligible_wh, session_count, eligible_session_count = (
            _extract_wall_connector_charge_wh(
                raw,
                time_zone=time_zone,
                eligible_start_time=eligible_start_time,
                eligible_end_time=eligible_end_time,
            )
        )
        total_charged_kwh = total_wh / 1000
        eligible_charged_kwh = eligible_wh / 1000
        grid_charged_kwh: float | None = None
        eligible_grid_charged_kwh: float | None = None
        if vehicle_grid_imported_kwh is not None:
            if vehicle_grid_imported_kwh < 0:
                raise ValueError("Tesla vehicle grid import cannot be negative")
            if total_charged_kwh > 0:
                grid_charged_kwh = min(total_charged_kwh, vehicle_grid_imported_kwh)
                noneligible_charged_kwh = max(
                    0.0,
                    total_charged_kwh - eligible_charged_kwh,
                )
                eligible_grid_charged_kwh = min(
                    eligible_charged_kwh,
                    max(0.0, grid_charged_kwh - noneligible_charged_kwh),
                )
            elif vehicle_grid_imported_kwh == 0:
                grid_charged_kwh = 0.0
                eligible_grid_charged_kwh = 0.0

        return TeslaMonthlyCharging(
            total_charged_kwh=total_charged_kwh,
            eligible_charged_kwh=eligible_charged_kwh,
            session_count=session_count,
            eligible_session_count=eligible_session_count,
            start_date=start.isoformat(),
            end_date=end.isoformat(),
            raw=raw,
            grid_charged_kwh=grid_charged_kwh,
            eligible_grid_charged_kwh=eligible_grid_charged_kwh,
            grid_source=vehicle_grid_source if grid_charged_kwh is not None else None,
        )

    def _get_json(self, path: str) -> dict[str, Any]:
        token = self._access_token()
        url = f"{self.config.api_base_url}{path}"
        with httpx.Client(timeout=self.config.timeout_seconds) as client:
            response = client.get(
                url,
                headers={
                    "Authorization": f"Bearer {token}",
                    "User-Agent": "avalon-q-scheduler/1.0",
                },
            )
        response.raise_for_status()
        return response.json()

    def _access_token(self) -> str:
        if self.token_cache.access_token_valid:
            assert self.token_cache.access_token is not None
            return self.token_cache.access_token
        return self.refresh_access_token().access_token or ""

    def _post_token(self, payload: dict[str, Any]) -> dict[str, Any]:
        with httpx.Client(timeout=self.config.timeout_seconds) as client:
            response = client.post(
                self.config.token_url,
                json=payload,
                headers={"User-Agent": "avalon-q-scheduler/1.0"},
            )
        response.raise_for_status()
        return response.json()

    @staticmethod
    def _cache_from_token_response(data: dict[str, Any]) -> TeslaTokenCache:
        return TeslaTokenCache(
            access_token=data.get("access_token"),
            refresh_token=data.get("refresh_token"),
            expires_at=time.time() + float(data.get("expires_in", 0)),
        )

    @staticmethod
    def load_token_cache(path: str | Path) -> TeslaTokenCache:
        path = Path(path)
        if not path.exists():
            return TeslaTokenCache()
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
        if not isinstance(data, dict):
            raise ValueError(f"Tesla token cache must contain a JSON object: {path}")
        return TeslaTokenCache(**{**asdict(TeslaTokenCache()), **data})

    @staticmethod
    def save_token_cache(path: str | Path, token_cache: TeslaTokenCache) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as handle:
            json.dump(asdict(token_cache), handle, indent=2, sort_keys=True)
            handle.write("\n")


def _response_mapping(raw: dict[str, Any]) -> dict[str, Any]:
    response = raw.get("response", raw)
    return response if isinstance(response, dict) else {}


def _number_from_keys(data: dict[str, Any], *keys: str) -> float | None:
    for key in keys:
        value = data.get(key)
        if isinstance(value, (int, float)):
            return float(value)
    return None


def _string_from_keys(data: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = data.get(key)
        if isinstance(value, str):
            return value
    return None


def _bool_from_keys(data: dict[str, Any], *keys: str) -> bool | None:
    for key in keys:
        value = data.get(key)
        if isinstance(value, bool):
            return value
    return None


def _extract_grid_import_export_wh(data: Any) -> tuple[float | None, float | None]:
    rows = _time_series_rows(data)
    imported = _sum_preferred(rows, ("grid_energy_imported", "grid_imported_energy"))
    exported = _sum_preferred(
        rows,
        (
            "grid_energy_exported",
            "grid_energy_exported_from_solar",
            "grid_energy_exported_from_battery",
            "grid_exported_energy",
        ),
    )
    if imported is not None and exported is not None:
        return imported, exported

    flattened = _flatten_numbers(data)
    if imported is None:
        imported = sum(
            value
            for key, value in flattened
            if "grid" in key and "import" in key
        ) or None
    if exported is None:
        exported = sum(
            value
            for key, value in flattened
            if "grid" in key and "export" in key
        ) or None
    return imported, exported


_VEHICLE_GRID_IMPORT_KEYS = (
    "vehicle_energy_imported_from_grid",
    "wall_connector_energy_imported_from_grid",
    "charger_energy_imported_from_grid",
    "charging_energy_imported_from_grid",
    "car_energy_imported_from_grid",
)


def _extract_vehicle_grid_import_wh(
    data: Any,
    *,
    start_date: date | None = None,
) -> tuple[float | None, str | None]:
    """Return grid energy sent to the vehicle, never total vehicle charge energy.

    Tesla's site energy rows expose the grid source and its known home and
    Powerwall destinations. Newer payloads may expose a direct vehicle field;
    otherwise the vehicle destination is the non-negative source balance:
    grid imported - home from grid - Powerwall from grid.
    """

    rows = _time_series_rows(data)
    if not rows:
        return None, None

    total_wh = 0.0
    found_rows = 0
    methods: set[str] = set()
    for row in rows:
        if start_date is not None:
            timestamp = row.get("timestamp")
            if not isinstance(timestamp, str):
                return None, None
            try:
                row_date = datetime.fromisoformat(timestamp.replace("Z", "+00:00")).date()
            except ValueError:
                return None, None
            if row_date < start_date:
                continue

        direct_vehicle_wh = _number_from_keys(row, *_VEHICLE_GRID_IMPORT_KEYS)
        if direct_vehicle_wh is not None:
            if direct_vehicle_wh < 0:
                return None, None
            total_wh += direct_vehicle_wh
            found_rows += 1
            methods.add("direct_vehicle_grid_field")
            continue

        grid_imported_wh = _number_from_keys(
            row,
            "grid_energy_imported",
            "grid_imported_energy",
        )
        home_from_grid_wh = _number_from_keys(
            row,
            "consumer_energy_imported_from_grid",
            "home_energy_imported_from_grid",
        )
        battery_from_grid_wh = _number_from_keys(
            row,
            "battery_energy_imported_from_grid",
            "powerwall_energy_imported_from_grid",
        )
        if (
            grid_imported_wh is None
            or home_from_grid_wh is None
            or battery_from_grid_wh is None
            or min(grid_imported_wh, home_from_grid_wh, battery_from_grid_wh) < 0
        ):
            return None, None

        total_wh += max(
            0.0,
            grid_imported_wh - home_from_grid_wh - battery_from_grid_wh,
        )
        found_rows += 1
        methods.add("site_energy_flow_balance")

    if found_rows == 0:
        return 0.0, "site_energy_flow_balance"
    method = methods.pop() if len(methods) == 1 else "mixed_site_energy_fields"
    return total_wh, method


def _extract_wall_connector_charge_wh(
    data: Any,
    *,
    time_zone: str,
    eligible_start_time: datetime_time,
    eligible_end_time: datetime_time,
) -> tuple[float, float, int, int]:
    response = data.get("response", data) if isinstance(data, dict) else None
    if not isinstance(response, dict) or not isinstance(response.get("charge_history"), list):
        raise ValueError("Tesla charge history response did not contain charge_history rows")

    zone = ZoneInfo(time_zone)
    total_wh = 0.0
    eligible_wh = 0.0
    session_count = 0
    eligible_session_count = 0
    for row in response["charge_history"]:
        if not isinstance(row, dict):
            continue
        start_seconds = _nested_number(row, "charge_start_time", "seconds")
        duration_seconds = _nested_number(row, "charge_duration", "seconds")
        energy_added_wh = row.get("energy_added_wh")
        if (
            start_seconds is None
            or duration_seconds is None
            or not isinstance(energy_added_wh, (int, float))
            or duration_seconds <= 0
            or energy_added_wh < 0
        ):
            continue

        session_start = datetime.fromtimestamp(start_seconds, tz=zone)
        session_end = session_start + timedelta(seconds=duration_seconds)
        overlap_seconds = _eligible_window_overlap_seconds(
            session_start,
            session_end,
            eligible_start_time=eligible_start_time,
            eligible_end_time=eligible_end_time,
        )
        session_count += 1
        total_wh += float(energy_added_wh)
        if overlap_seconds > 0:
            eligible_session_count += 1
            eligible_wh += float(energy_added_wh) * min(
                1.0,
                overlap_seconds / duration_seconds,
            )

    return total_wh, eligible_wh, session_count, eligible_session_count


def _nested_number(data: dict[str, Any], key: str, nested_key: str) -> float | None:
    nested = data.get(key)
    if not isinstance(nested, dict):
        return None
    value = nested.get(nested_key)
    return float(value) if isinstance(value, (int, float)) else None


def _eligible_window_overlap_seconds(
    session_start: datetime,
    session_end: datetime,
    *,
    eligible_start_time: datetime_time,
    eligible_end_time: datetime_time,
) -> float:
    if session_end <= session_start:
        return 0.0

    total = 0.0
    first_day = session_start.date() - timedelta(days=1)
    last_day = session_end.date()
    day = first_day
    while day <= last_day:
        window_start = datetime.combine(day, eligible_start_time, tzinfo=session_start.tzinfo)
        window_end = datetime.combine(day, eligible_end_time, tzinfo=session_start.tzinfo)
        if window_end <= window_start:
            window_end += timedelta(days=1)
        overlap_start = max(session_start, window_start)
        overlap_end = min(session_end, window_end)
        if overlap_end > overlap_start:
            total += (overlap_end - overlap_start).total_seconds()
        day += timedelta(days=1)
    return total


def _time_series_rows(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, dict):
        rows = data.get("time_series") or data.get("timeSeries")
        if isinstance(rows, list):
            return [row for row in rows if isinstance(row, dict)]
        return [data]
    if isinstance(data, list):
        return [row for row in data if isinstance(row, dict)]
    return []


def _sum_preferred(rows: list[dict[str, Any]], keys: tuple[str, ...]) -> float | None:
    total = 0.0
    found = False
    for row in rows:
        for key in keys:
            value = row.get(key)
            if isinstance(value, (int, float)):
                total += float(value)
                found = True
    return total if found else None


def _flatten_numbers(data: Any, prefix: str = "") -> list[tuple[str, float]]:
    if isinstance(data, dict):
        found: list[tuple[str, float]] = []
        for key, value in data.items():
            key_path = f"{prefix}.{key}".strip(".").lower()
            found.extend(_flatten_numbers(value, key_path))
        return found
    if isinstance(data, list):
        found = []
        for value in data:
            found.extend(_flatten_numbers(value, prefix))
        return found
    if isinstance(data, (int, float)):
        return [(prefix, float(data))]
    return []
