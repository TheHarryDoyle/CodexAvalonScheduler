import pytest

from avalon_q_scheduler.config import WeatherConfig
from avalon_q_scheduler.decision_engine import DecisionEngine, TargetMode
from avalon_q_scheduler.weather_client import DayWeather


def weather(
    cloud=20,
    probability=0,
    precipitation=0,
    codes=None,
    avg_shortwave_radiation=None,
):
    return DayWeather(
        date="2026-06-17",
        avg_cloud_cover_percent=cloud,
        max_precip_probability_percent=probability,
        total_precip_mm=precipitation,
        weather_codes=codes or [0],
        raw={},
        avg_shortwave_radiation_w_m2=avg_shortwave_radiation,
    )


def test_morning_decision_uses_super_for_sunny_forecast():
    decision = DecisionEngine(WeatherConfig()).morning_weather_decision(weather(cloud=10))

    assert decision.target_mode == TargetMode.SUPER
    assert decision.reason == "sunny forecast"


def test_morning_solar_safety_decision_uses_eco_for_non_rainy_forecast():
    decision = DecisionEngine(WeatherConfig()).morning_solar_safety_decision(weather(cloud=10))

    assert decision.target_mode == TargetMode.ECO
    assert decision.reason == "morning solar window started"


def test_peak_solar_high_uses_super_for_strong_solar_radiation():
    decision = DecisionEngine(WeatherConfig()).peak_solar_high_decision(
        weather(cloud=65, avg_shortwave_radiation=700)
    )

    assert decision.target_mode == TargetMode.SUPER
    assert decision.reason == "peak solar window started"


def test_peak_solar_high_uses_eco_for_weak_solar_radiation():
    decision = DecisionEngine(WeatherConfig()).peak_solar_high_decision(
        weather(cloud=10, avg_shortwave_radiation=350)
    )

    assert decision.target_mode == TargetMode.ECO
    assert decision.reason == "peak solar forecast not strong enough"


def test_late_afternoon_bridge_uses_off_when_solar_is_weak_and_evening_is_off():
    decision = DecisionEngine(WeatherConfig()).late_afternoon_bridge_decision(
        weather(cloud=10, avg_shortwave_radiation=100),
        evening_behavior="off",
    )

    assert decision.target_mode == TargetMode.OFF
    assert decision.reason == "late afternoon solar weak; starting overnight off early"


def test_late_afternoon_bridge_keeps_eco_when_solar_is_usable():
    decision = DecisionEngine(WeatherConfig()).late_afternoon_bridge_decision(
        weather(cloud=10, avg_shortwave_radiation=300),
        evening_behavior="off",
    )

    assert decision.target_mode == TargetMode.ECO
    assert decision.reason == "late afternoon solar bridge remains usable"


def test_morning_decision_uses_off_for_high_precip_probability():
    decision = DecisionEngine(WeatherConfig()).morning_weather_decision(weather(probability=80))

    assert decision.target_mode == TargetMode.OFF
    assert decision.reason == "rainy forecast"


def test_morning_decision_uses_off_for_rain_weather_code():
    decision = DecisionEngine(WeatherConfig()).morning_weather_decision(
        weather(probability=25, precipitation=0.1, codes=[61])
    )

    assert decision.target_mode == TargetMode.OFF


def test_morning_decision_uses_eco_for_cloudy_forecast():
    decision = DecisionEngine(WeatherConfig()).morning_weather_decision(weather(cloud=65))

    assert decision.target_mode == TargetMode.ECO
    assert decision.reason == "cloudy forecast"


def test_schedule_decisions_do_not_use_standard():
    engine = DecisionEngine(WeatherConfig())

    assert engine.evening_low_decision().target_mode == TargetMode.ECO
    assert engine.peak_solar_low_decision().target_mode == TargetMode.ECO
    assert engine.cheap_power_high_decision().target_mode == TargetMode.SUPER
    assert engine.cheap_power_low_decision().target_mode == TargetMode.ECO


def test_evening_decision_can_be_configured_to_off():
    decision = DecisionEngine(WeatherConfig()).evening_decision("off")

    assert decision.target_mode == TargetMode.OFF
    assert decision.reason == "evening solar window ended; overnight off"


def test_evening_decision_rejects_super():
    with pytest.raises(ValueError):
        DecisionEngine(WeatherConfig()).evening_decision("super")


def test_weather_code_without_precip_signal_does_not_force_rainy():
    decision = DecisionEngine(WeatherConfig()).morning_weather_decision(
        weather(cloud=20, probability=1, precipitation=0, codes=[95])
    )

    assert decision.target_mode == TargetMode.SUPER


def test_low_probability_precip_amount_and_storm_codes_use_cloudy_behavior():
    decision = DecisionEngine(WeatherConfig()).morning_weather_decision(
        weather(cloud=68, probability=9, precipitation=1.4, codes=[95])
    )

    assert decision.target_mode == TargetMode.ECO
    assert decision.reason == "cloudy forecast"


def test_storm_codes_with_zero_daytime_precipitation_use_cloudy_behavior():
    decision = DecisionEngine(WeatherConfig()).morning_weather_decision(
        weather(cloud=50.3, probability=27, precipitation=0, codes=[95] * 12)
    )

    assert decision.target_mode == TargetMode.ECO
    assert decision.reason == "cloudy forecast"


def test_precip_amount_with_supporting_probability_is_rainy():
    decision = DecisionEngine(WeatherConfig()).morning_weather_decision(
        weather(cloud=68, probability=25, precipitation=1.4)
    )

    assert decision.target_mode == TargetMode.OFF
