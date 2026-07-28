from __future__ import annotations

import csv
import hashlib
import hmac
import json
import os
import subprocess
import tempfile
import time
import urllib.parse
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

from .config import NestConfig


SDM_SCOPE = "https://www.googleapis.com/auth/sdm.service"


@dataclass(frozen=True)
class NestCredentials:
    client_id: str
    client_secret: str


@dataclass
class NestTokenCache:
    access_token: str | None = None
    expires_at: float = 0

    @property
    def access_token_valid(self) -> bool:
        return bool(self.access_token) and time.time() < self.expires_at - 120


class NestAuthError(RuntimeError):
    """Raised when Google rejects the Nest OAuth refresh request."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        oauth_error: str | None = None,
        oauth_error_description: str | None = None,
    ):
        super().__init__(message)
        self.status_code = status_code
        self.oauth_error = oauth_error
        self.oauth_error_description = oauth_error_description

    @property
    def refresh_grant_invalid(self) -> bool:
        return self.oauth_error == "invalid_grant"

    @property
    def requires_reauthorization(self) -> bool:
        return self.refresh_grant_invalid or self.oauth_error == "sdm_unauthorized_after_refresh"


class NestTokenPersistenceError(RuntimeError):
    """Raised when a rotated refresh token cannot yet be saved securely."""


@dataclass(frozen=True)
class NestAuthorizationTokens:
    access_token: str | None
    refresh_token: str | None
    expires_at: float


@dataclass(frozen=True)
class NestThermostatReading:
    captured_at: str
    device_id: str
    custom_name: str | None
    room: str | None
    device_type: str
    hvac_status: str | None
    thermostat_mode: str | None
    ambient_temp_c: float | None
    ambient_humidity_pct: float | None
    heat_setpoint_c: float | None
    cool_setpoint_c: float | None
    eco_mode: str | None
    fan_timer_mode: str | None
    fan_timer_timeout: str | None
    connectivity_status: str | None
    raw: dict[str, Any]

    @property
    def ambient_temp_f(self) -> float | None:
        if self.ambient_temp_c is None:
            return None
        return self.ambient_temp_c * 9 / 5 + 32

    def to_json_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["ambient_temp_f"] = self.ambient_temp_f
        return data


@dataclass(frozen=True)
class MonthlyRuntimeBackfill:
    month: str
    total_runtime_minutes: int
    heat_minutes: int
    cool_minutes: int
    daily_average_minutes: int
    comparison_text: str
    leaves_earned: int | None
    source: str
    source_confidence: str
    notes: str


def credentials_from_env(config: NestConfig) -> NestCredentials:
    client_id = os.environ.get(config.client_id_env, "").strip()
    client_secret = os.environ.get(config.client_secret_env, "").strip()
    if not client_id or not client_secret:
        raise ValueError(
            f"Set {config.client_id_env} and {config.client_secret_env} before using Nest SDM."
        )
    return NestCredentials(client_id=client_id, client_secret=client_secret)


def load_refresh_token(config: NestConfig) -> str:
    if config.refresh_token_dpapi_path and config.refresh_token_dpapi_path.exists():
        return _load_dpapi_refresh_token(config.refresh_token_dpapi_path)
    env_token = os.environ.get(config.refresh_token_env, "").strip()
    if env_token:
        return env_token
    if config.refresh_token_dpapi_path:
        return _load_dpapi_refresh_token(config.refresh_token_dpapi_path)
    raise ValueError(
        f"Set {config.refresh_token_env} or nest.refresh_token_dpapi_path before using Nest SDM."
    )


def refresh_token_source_version(config: NestConfig) -> str:
    """Return a non-secret version marker for automatic token reloads."""

    path = config.refresh_token_dpapi_path
    if path and path.exists():
        digest = hashlib.sha256()
        digest.update(str(path.resolve()).encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        return f"dpapi:{digest.hexdigest()}"
    env_token = os.environ.get(config.refresh_token_env, "").strip()
    if env_token:
        digest = hashlib.sha256(env_token.encode("utf-8")).hexdigest()
        return f"env:{digest}"
    return "missing"


def build_authorization_url(config: NestConfig, credentials: NestCredentials) -> str:
    if not config.project_id:
        raise ValueError("Set nest.project_id before authorizing Nest SDM.")
    params = {
        "redirect_uri": config.redirect_uri,
        "access_type": "offline",
        "prompt": "consent",
        "client_id": credentials.client_id,
        "response_type": "code",
        "scope": SDM_SCOPE,
    }
    project_id = urllib.parse.quote(config.project_id, safe="")
    return f"{config.auth_base_url.rstrip('/')}/{project_id}/auth?{urllib.parse.urlencode(params)}"


def authorization_code_from_input(value: str) -> str:
    text = value.strip()
    if not text:
        raise ValueError("Nest authorization code is empty.")
    parsed = urllib.parse.urlparse(text)
    if parsed.query:
        code = urllib.parse.parse_qs(parsed.query).get("code", [""])[0].strip()
        if code:
            return code
    return text


def exchange_authorization_code(
    config: NestConfig,
    credentials: NestCredentials,
    authorization_code: str,
) -> NestAuthorizationTokens:
    payload = {
        "client_id": credentials.client_id,
        "client_secret": credentials.client_secret,
        "code": authorization_code,
        "grant_type": "authorization_code",
        "redirect_uri": config.redirect_uri,
    }
    with httpx.Client(timeout=config.timeout_seconds) as client:
        response = client.post(
            config.token_url,
            data=payload,
            headers={"User-Agent": "avalon-q-scheduler/1.0"},
        )
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        oauth_error, oauth_description = _oauth_error_fields(response)
        raise NestAuthError(
            f"Nest authorization failed: HTTP {response.status_code}{_oauth_error_detail(response)}",
            status_code=response.status_code,
            oauth_error=oauth_error,
            oauth_error_description=oauth_description,
        ) from exc
    data = response.json()
    return NestAuthorizationTokens(
        access_token=data.get("access_token"),
        refresh_token=data.get("refresh_token"),
        expires_at=time.time() + float(data.get("expires_in", 0)),
    )


def save_refresh_token(config: NestConfig, refresh_token: str) -> str:
    if not refresh_token.strip():
        raise ValueError("Nest refresh token is empty.")
    if config.refresh_token_dpapi_path:
        _save_dpapi_refresh_token(config.refresh_token_dpapi_path, refresh_token)
        return str(config.refresh_token_dpapi_path)
    raise ValueError(
        f"Set nest.refresh_token_dpapi_path before saving a Nest token, or set {config.refresh_token_env} manually."
    )


def append_reading(path: str | Path, reading: NestThermostatReading) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(reading.to_json_dict(), sort_keys=True))
        handle.write("\n")


def load_monthly_backfill(path: str | Path) -> list[MonthlyRuntimeBackfill]:
    path = Path(path)
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return [_monthly_from_row(row) for row in csv.DictReader(handle)]


class NestSdmClient:
    def __init__(
        self,
        config: NestConfig,
        credentials: NestCredentials,
        refresh_token: str,
        token_cache: NestTokenCache | None = None,
    ):
        if not config.project_id:
            raise ValueError("Set nest.project_id before using Nest SDM.")
        self.config = config
        self.credentials = credentials
        self.refresh_token = refresh_token
        self.token_cache = token_cache or NestTokenCache()
        self.persisted_refresh_token_source_version: str | None = None
        self.refresh_token_persistence_pending = False

    def refresh_access_token(self) -> NestTokenCache:
        payload = {
            "client_id": self.credentials.client_id,
            "client_secret": self.credentials.client_secret,
            "refresh_token": self.refresh_token,
            "grant_type": "refresh_token",
        }
        with httpx.Client(timeout=self.config.timeout_seconds) as client:
            response = client.post(
                self.config.token_url,
                data=payload,
                headers={"User-Agent": "avalon-q-scheduler/1.0"},
            )
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            oauth_error, oauth_description = _oauth_error_fields(response)
            raise NestAuthError(
                f"Nest token refresh failed: HTTP {response.status_code}{_oauth_error_detail(response)}",
                status_code=response.status_code,
                oauth_error=oauth_error,
                oauth_error_description=oauth_description,
            ) from exc
        data = response.json()
        self.token_cache = NestTokenCache(
            access_token=data.get("access_token"),
            expires_at=time.time() + float(data.get("expires_in", 0)),
        )
        rotated_refresh_token = data.get("refresh_token")
        if (
            isinstance(rotated_refresh_token, str)
            and rotated_refresh_token.strip()
            and rotated_refresh_token != self.refresh_token
        ):
            self.refresh_token = rotated_refresh_token
            self.refresh_token_persistence_pending = True
            self._persist_pending_refresh_token()
        return self.token_cache

    def get_structures(self) -> list[dict[str, Any]]:
        data = self._get_json(f"/v1/enterprises/{self.config.project_id}/structures")
        structures = data.get("structures", [])
        return structures if isinstance(structures, list) else []

    def get_devices(self) -> list[dict[str, Any]]:
        data = self._get_json(f"/v1/enterprises/{self.config.project_id}/devices")
        devices = data.get("devices", [])
        return devices if isinstance(devices, list) else []

    def get_thermostat_reading(self) -> NestThermostatReading:
        devices = self.get_devices()
        selected = self._select_thermostat(devices)
        return reading_from_device(selected)

    def _select_thermostat(self, devices: list[dict[str, Any]]) -> dict[str, Any]:
        thermostats = [
            device
            for device in devices
            if device.get("type") == "sdm.devices.types.THERMOSTAT"
        ]
        if not thermostats:
            raise RuntimeError("Nest SDM returned no thermostat devices.")
        if not self.config.selected_thermostat_name:
            return thermostats[0]

        for device in thermostats:
            traits = _mapping(device.get("traits"))
            info = _mapping(traits.get("sdm.devices.traits.Info"))
            if info.get("customName") == self.config.selected_thermostat_name:
                return device
        names = [
            _mapping(_mapping(device.get("traits")).get("sdm.devices.traits.Info")).get("customName")
            for device in thermostats
        ]
        raise RuntimeError(
            f"Thermostat {self.config.selected_thermostat_name!r} not found. Available thermostats: {names}"
        )

    def _get_json(self, path: str) -> dict[str, Any]:
        if self.refresh_token_persistence_pending:
            self._persist_pending_refresh_token()
        had_valid_cached_token = self.token_cache.access_token_valid
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
            if response.status_code == 401:
                self.token_cache = NestTokenCache()
                if not had_valid_cached_token:
                    raise NestAuthError(
                        "Nest SDM authorization failed: HTTP 401 with a newly "
                        "refreshed access token",
                        status_code=401,
                        oauth_error="sdm_unauthorized_after_refresh",
                    )
                token = self.refresh_access_token().access_token or ""
                response = client.get(
                    url,
                    headers={
                        "Authorization": f"Bearer {token}",
                        "User-Agent": "avalon-q-scheduler/1.0",
                    },
                )
                if response.status_code == 401:
                    self.token_cache = NestTokenCache()
                    raise NestAuthError(
                        "Nest SDM authorization failed: HTTP 401 after automatic "
                        "access-token refresh",
                        status_code=401,
                        oauth_error="sdm_unauthorized_after_refresh",
                    )
        response.raise_for_status()
        data = response.json()
        return data if isinstance(data, dict) else {}

    def _persist_pending_refresh_token(self) -> None:
        try:
            save_refresh_token(self.config, self.refresh_token)
        except Exception as exc:
            raise NestTokenPersistenceError(
                "Nest refresh token rotated but secure persistence failed; "
                "the scheduler will retry automatically"
            ) from exc
        self.persisted_refresh_token_source_version = refresh_token_source_version(
            self.config
        )
        self.refresh_token_persistence_pending = False

    def _access_token(self) -> str:
        if self.token_cache.access_token_valid:
            assert self.token_cache.access_token is not None
            return self.token_cache.access_token
        return self.refresh_access_token().access_token or ""


def reading_from_device(device: dict[str, Any]) -> NestThermostatReading:
    traits = _mapping(device.get("traits"))
    info = _mapping(traits.get("sdm.devices.traits.Info"))
    humidity = _mapping(traits.get("sdm.devices.traits.Humidity"))
    connectivity = _mapping(traits.get("sdm.devices.traits.Connectivity"))
    fan = _mapping(traits.get("sdm.devices.traits.Fan"))
    mode = _mapping(traits.get("sdm.devices.traits.ThermostatMode"))
    eco = _mapping(traits.get("sdm.devices.traits.ThermostatEco"))
    hvac = _mapping(traits.get("sdm.devices.traits.ThermostatHvac"))
    setpoints = _mapping(traits.get("sdm.devices.traits.ThermostatTemperatureSetpoint"))
    temperature = _mapping(traits.get("sdm.devices.traits.Temperature"))
    parent = _parent_relation(device)

    return NestThermostatReading(
        captured_at=datetime.now().astimezone().isoformat(),
        device_id=str(device.get("name", "")),
        custom_name=_string(info.get("customName")),
        room=_string(parent.get("displayName")),
        device_type=str(device.get("type", "")),
        hvac_status=_string(hvac.get("status")),
        thermostat_mode=_string(mode.get("mode")),
        ambient_temp_c=_number(temperature.get("ambientTemperatureCelsius")),
        ambient_humidity_pct=_number(humidity.get("ambientHumidityPercent")),
        heat_setpoint_c=_number(setpoints.get("heatCelsius")),
        cool_setpoint_c=_number(setpoints.get("coolCelsius")),
        eco_mode=_string(eco.get("mode")),
        fan_timer_mode=_string(fan.get("timerMode")),
        fan_timer_timeout=_string(fan.get("timerTimeout")),
        connectivity_status=_string(connectivity.get("status")),
        raw=device,
    )


def _load_dpapi_refresh_token(path: Path) -> str:
    if not path.exists():
        raise FileNotFoundError(f"Nest DPAPI refresh token file not found: {path}")
    command = [
        "powershell",
        "-NoProfile",
        "-NonInteractive",
        "-Command",
        (
            "$ErrorActionPreference = 'Stop'; "
            f"$encrypted = (Get-Content -LiteralPath {str(path)!r} -Raw).Trim(); "
            "$secure = ConvertTo-SecureString -String $encrypted; "
            "$ptr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure); "
            "try { [Runtime.InteropServices.Marshal]::PtrToStringBSTR($ptr) } "
            "finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($ptr) }"
        ),
    ]
    result = subprocess.run(
        command,
        check=True,
        capture_output=True,
        text=True,
        timeout=20,
    )
    token = result.stdout.strip()
    if not token:
        raise RuntimeError(f"Failed to decrypt Nest DPAPI refresh token: {path}")
    return token


def _save_dpapi_refresh_token(path: Path, refresh_token: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    file_descriptor, temp_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    os.close(file_descriptor)
    temp_path = Path(temp_name)
    try:
        command = [
            "powershell",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            (
                "$ErrorActionPreference = 'Stop'; "
                "$token = [Console]::In.ReadToEnd().Trim(); "
                "if (-not $token) { throw 'No Nest refresh token supplied.' }; "
                "$secure = ConvertTo-SecureString -String $token -AsPlainText -Force; "
                "$encrypted = ConvertFrom-SecureString -SecureString $secure; "
                f"Set-Content -LiteralPath {str(temp_path)!r} -Value $encrypted "
                "-NoNewline -Encoding UTF8"
            ),
        ]
        subprocess.run(
            command,
            input=refresh_token,
            check=True,
            capture_output=True,
            text=True,
            timeout=20,
        )
        verified_token = _load_dpapi_refresh_token(temp_path)
        if not hmac.compare_digest(verified_token, refresh_token):
            raise RuntimeError(
                "Failed to verify encrypted Nest refresh token before replacement"
            )
        os.replace(temp_path, path)
    finally:
        temp_path.unlink(missing_ok=True)


def _monthly_from_row(row: dict[str, str]) -> MonthlyRuntimeBackfill:
    return MonthlyRuntimeBackfill(
        month=row["month"],
        total_runtime_minutes=_int(row.get("total_runtime_minutes")),
        heat_minutes=_int(row.get("heat_minutes")),
        cool_minutes=_int(row.get("cool_minutes")),
        daily_average_minutes=_int(row.get("daily_average_minutes")),
        comparison_text=row.get("comparison_text", ""),
        leaves_earned=_optional_int(row.get("leaves_earned")),
        source=row.get("source", ""),
        source_confidence=row.get("source_confidence", ""),
        notes=row.get("notes", ""),
    )


def _parent_relation(device: dict[str, Any]) -> dict[str, Any]:
    relations = device.get("parentRelations")
    if isinstance(relations, list) and relations and isinstance(relations[0], dict):
        return relations[0]
    if isinstance(relations, dict):
        return relations
    return {}


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _oauth_error_detail(response: httpx.Response) -> str:
    error, description = _oauth_error_fields(response)
    fields = [field for field in (error, description) if field]
    return f" ({'; '.join(fields)})" if fields else ""


def _oauth_error_fields(response: httpx.Response) -> tuple[str | None, str | None]:
    try:
        data = response.json()
    except ValueError:
        return None, None
    if not isinstance(data, dict):
        return None, None
    error = str(data["error"]) if data.get("error") else None
    description = (
        str(data["error_description"])
        if data.get("error_description")
        else None
    )
    return error, description


def _number(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) else None


def _string(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _int(value: str | None) -> int:
    return int(value or "0")


def _optional_int(value: str | None) -> int | None:
    return int(value) if value else None
