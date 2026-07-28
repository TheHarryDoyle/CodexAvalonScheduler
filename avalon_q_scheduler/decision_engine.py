from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .config import WeatherConfig
from .weather_client import DayWeather


class TargetMode(str, Enum):
    ECO = "eco"
    SUPER = "super"
    OFF = "off"


@dataclass(frozen=True)
class Decision:
    target_mode: TargetMode
    reason: str
    weather: DayWeather | None = None


RAIN_OR_STORM_CODES = {
    51,
    53,
    55,
    56,
    57,
    61,
    63,
    65,
    66,
    67,
    80,
    81,
    82,
    95,
    96,
    99,
}


class DecisionEngine:
    def __init__(
        self,
        weather_config: WeatherConfig,
        rainy_behavior: str = "off",
        cloudy_behavior: str = "eco",
        use_standard: bool = False,
    ):
        self.weather_config = weather_config
        self.rainy_behavior = rainy_behavior
        self.cloudy_behavior = cloudy_behavior
        self.use_standard = use_standard

    def morning_weather_decision(self, weather: DayWeather) -> Decision:
        if self.is_rainy(weather):
            return Decision(self._behavior_to_mode(self.rainy_behavior), "rainy forecast", weather)
        if self.is_sunny(weather):
            return Decision(TargetMode.SUPER, "sunny forecast", weather)
        return Decision(self._behavior_to_mode(self.cloudy_behavior), "cloudy forecast", weather)

    def morning_solar_safety_decision(self, weather: DayWeather) -> Decision:
        if self.is_rainy(weather):
            return Decision(self._behavior_to_mode(self.rainy_behavior), "rainy forecast", weather)
        return Decision(TargetMode.ECO, "morning solar window started", weather)

    def peak_solar_high_decision(self, weather: DayWeather) -> Decision:
        if self.is_rainy(weather):
            return Decision(self._behavior_to_mode(self.rainy_behavior), "rainy forecast", weather)
        if self.is_peak_solar_strong(weather):
            return Decision(TargetMode.SUPER, "peak solar window started", weather)
        return Decision(TargetMode.ECO, "peak solar forecast not strong enough", weather)

    def peak_solar_low_decision(self) -> Decision:
        return Decision(TargetMode.ECO, "peak solar window ended")

    def late_afternoon_bridge_decision(
        self,
        weather: DayWeather,
        evening_behavior: str = "eco",
    ) -> Decision:
        if self.is_rainy(weather):
            return Decision(self._behavior_to_mode(self.rainy_behavior), "rainy forecast", weather)
        if self.is_late_afternoon_solar_usable(weather):
            return Decision(TargetMode.ECO, "late afternoon solar bridge remains usable", weather)

        mode = self._behavior_to_mode(evening_behavior)
        if mode == TargetMode.OFF:
            return Decision(TargetMode.OFF, "late afternoon solar weak; starting overnight off early", weather)
        if mode != TargetMode.ECO:
            raise ValueError("Evening behavior only supports eco or off")
        return Decision(TargetMode.ECO, "late afternoon solar weak; staying eco", weather)

    def evening_low_decision(self) -> Decision:
        return Decision(TargetMode.ECO, "evening solar window ended")

    def evening_decision(self, behavior: str = "eco") -> Decision:
        mode = self._behavior_to_mode(behavior)
        if mode == TargetMode.OFF:
            return Decision(TargetMode.OFF, "evening solar window ended; overnight off")
        if mode != TargetMode.ECO:
            raise ValueError("Evening behavior only supports eco or off")
        return Decision(TargetMode.ECO, "evening solar window ended")

    def cheap_power_high_decision(self) -> Decision:
        return Decision(TargetMode.SUPER, "cheap power window started")

    def cheap_power_low_decision(self) -> Decision:
        return Decision(TargetMode.ECO, "cheap power window ended")

    def is_rainy(self, weather: DayWeather) -> bool:
        probability = weather.max_precip_probability_percent
        precipitation = weather.total_precip_mm
        precipitation_amount_is_confident = (
            probability is None
            or probability
            >= self.weather_config.rainy_precip_amount_probability_min_percent
        )
        has_precip_signal_for_weather_code = (
            probability is not None
            and probability
            >= self.weather_config.weather_code_precip_probability_min_percent
            and precipitation is not None
            and precipitation >= self.weather_config.weather_code_precip_mm_min
        )
        return (
            (
                probability is not None
                and probability
                >= self.weather_config.rainy_precip_probability_min_percent
            )
            or (
                precipitation is not None
                and precipitation >= self.weather_config.rainy_precip_mm_min
                and precipitation_amount_is_confident
            )
            or (
                has_precip_signal_for_weather_code
                and any(code in RAIN_OR_STORM_CODES for code in weather.weather_codes)
            )
        )

    def is_sunny(self, weather: DayWeather) -> bool:
        cloud_cover = weather.avg_cloud_cover_percent
        return (
            cloud_cover is not None
            and cloud_cover <= self.weather_config.sunny_cloud_cover_max_percent
            and not self.is_rainy(weather)
        )

    def is_peak_solar_strong(self, weather: DayWeather) -> bool:
        avg_radiation = weather.avg_shortwave_radiation_w_m2
        if avg_radiation is not None:
            return (
                avg_radiation
                >= self.weather_config.peak_solar_min_avg_shortwave_radiation_w_m2
                and not self.is_rainy(weather)
            )
        return self.is_sunny(weather)

    def is_late_afternoon_solar_usable(self, weather: DayWeather) -> bool:
        avg_radiation = weather.avg_shortwave_radiation_w_m2
        if avg_radiation is not None:
            return (
                avg_radiation
                >= self.weather_config.late_afternoon_bridge_min_avg_shortwave_radiation_w_m2
                and not self.is_rainy(weather)
            )
        return self.is_sunny(weather)

    def _behavior_to_mode(self, behavior: str) -> TargetMode:
        normalized = behavior.lower().strip()
        if normalized == "standard":
            raise ValueError("Standard mode is not allowed for scheduler automation")
        if normalized == "eco":
            return TargetMode.ECO
        if normalized == "super":
            return TargetMode.SUPER
        if normalized in {"off", "standby"}:
            return TargetMode.OFF
        raise ValueError(f"Unsupported automation behavior: {behavior}")
