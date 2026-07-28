from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import httpx

from .config import WeatherConfig


@dataclass(frozen=True)
class DayWeather:
    date: str
    avg_cloud_cover_percent: float | None
    max_precip_probability_percent: float | None
    total_precip_mm: float | None
    weather_codes: list[int]
    raw: dict
    avg_shortwave_radiation_w_m2: float | None = None
    max_shortwave_radiation_w_m2: float | None = None


class WeatherClient:
    def __init__(self, config: WeatherConfig, timezone: str, timeout_seconds: float = 10):
        if config.provider != "open_meteo":
            raise ValueError(f"Unsupported weather provider: {config.provider}")
        self.config = config
        self.timezone = timezone
        self.timeout_seconds = timeout_seconds

    def get_today_forecast(
        self,
        *,
        window_start: str | None = None,
        window_end: str | None = None,
    ) -> DayWeather:
        today = date.today().isoformat()
        params = {
            "latitude": self.config.latitude,
            "longitude": self.config.longitude,
            "timezone": self.timezone,
            "hourly": ",".join(
                [
                    "cloud_cover",
                    "precipitation_probability",
                    "precipitation",
                    "weather_code",
                    "shortwave_radiation",
                ]
            ),
            "start_date": today,
            "end_date": today,
        }
        response = httpx.get(
            "https://api.open-meteo.com/v1/forecast",
            params=params,
            timeout=self.timeout_seconds,
        )
        response.raise_for_status()
        raw = response.json()
        return self._normalize(
            raw,
            window_start=window_start,
            window_end=window_end,
        )

    def _normalize(
        self,
        raw: dict,
        *,
        window_start: str | None = None,
        window_end: str | None = None,
    ) -> DayWeather:
        hourly = raw.get("hourly", {})
        times = hourly.get("time", [])
        start = window_start or self.config.forecast_day_start
        end = window_end or self.config.forecast_day_end

        selected_indexes = [
            index
            for index, timestamp in enumerate(times)
            if start <= timestamp.split("T", maxsplit=1)[1] <= end
        ]

        def values(name: str) -> list[float]:
            series = hourly.get(name, [])
            return [
                float(series[index])
                for index in selected_indexes
                if index < len(series) and series[index] is not None
            ]

        cloud_cover = values("cloud_cover")
        precip_probability = values("precipitation_probability")
        precipitation = values("precipitation")
        weather_codes = [int(value) for value in values("weather_code")]
        shortwave_radiation = values("shortwave_radiation")

        first_date = times[0].split("T", maxsplit=1)[0] if times else date.today().isoformat()
        return DayWeather(
            date=first_date,
            avg_cloud_cover_percent=(
                sum(cloud_cover) / len(cloud_cover) if cloud_cover else None
            ),
            max_precip_probability_percent=(
                max(precip_probability) if precip_probability else None
            ),
            total_precip_mm=sum(precipitation) if precipitation else None,
            weather_codes=weather_codes,
            raw=raw,
            avg_shortwave_radiation_w_m2=(
                sum(shortwave_radiation) / len(shortwave_radiation)
                if shortwave_radiation
                else None
            ),
            max_shortwave_radiation_w_m2=(
                max(shortwave_radiation) if shortwave_radiation else None
            ),
        )
