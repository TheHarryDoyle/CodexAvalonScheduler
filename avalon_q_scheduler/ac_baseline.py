from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any

from .nest_client import MonthlyRuntimeBackfill, NestThermostatReading


def load_readings(path: str | Path) -> list[dict[str, Any]]:
    path = Path(path)
    if not path.exists():
        return []
    readings: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            stripped = line.strip()
            if not stripped:
                continue
            data = json.loads(stripped)
            if isinstance(data, dict):
                readings.append(data)
    return readings


def summarize_ac_baseline(
    *,
    monthly_backfill: list[MonthlyRuntimeBackfill],
    readings: list[dict[str, Any]],
    latest_reading: NestThermostatReading | None = None,
) -> dict[str, Any]:
    current_month = _month_key(datetime.now().astimezone())
    monthly_rows = [asdict(row) for row in monthly_backfill]
    live_summary = summarize_live_readings(readings)
    hottest_months = sorted(
        monthly_rows,
        key=lambda row: row.get("cool_minutes") or 0,
        reverse=True,
    )[:5]
    same_month_history = [
        row
        for row in monthly_rows
        if str(row.get("month", ""))[5:7] == current_month[5:7]
    ]

    return {
        "current_month": current_month,
        "historical_month_count": len(monthly_rows),
        "hottest_cooling_months": hottest_months,
        "same_calendar_month_history": same_month_history,
        "monthly_backfill": monthly_rows,
        "live_readings": live_summary,
        "latest_reading": latest_reading.to_json_dict() if latest_reading else None,
        "status": _baseline_status(monthly_rows, live_summary, latest_reading),
    }


def summarize_live_readings(readings: list[dict[str, Any]]) -> dict[str, Any]:
    if not readings:
        return {
            "readings_count": 0,
            "cooling_runtime_minutes_by_day": {},
            "cooling_runtime_minutes_by_month": {},
            "latest_hvac_status": None,
        }

    sorted_readings = sorted(readings, key=lambda row: row.get("captured_at", ""))
    minutes_by_day: dict[str, float] = defaultdict(float)
    minutes_by_month: dict[str, float] = defaultdict(float)
    max_gap_seconds = 5 * 60

    for previous, current in zip(sorted_readings, sorted_readings[1:]):
        previous_time = _parse_timestamp(previous.get("captured_at"))
        current_time = _parse_timestamp(current.get("captured_at"))
        if previous_time is None or current_time is None:
            continue
        seconds = (current_time - previous_time).total_seconds()
        if seconds <= 0 or seconds > max_gap_seconds:
            continue
        if previous.get("hvac_status") != "COOLING":
            continue
        minutes = seconds / 60
        day = previous_time.date().isoformat()
        month = _month_key(previous_time)
        minutes_by_day[day] += minutes
        minutes_by_month[month] += minutes

    latest = sorted_readings[-1]
    return {
        "readings_count": len(sorted_readings),
        "cooling_runtime_minutes_by_day": {
            key: round(value, 2) for key, value in sorted(minutes_by_day.items())
        },
        "cooling_runtime_minutes_by_month": {
            key: round(value, 2) for key, value in sorted(minutes_by_month.items())
        },
        "latest_hvac_status": latest.get("hvac_status"),
        "latest_ambient_temp_c": latest.get("ambient_temp_c"),
        "latest_ambient_humidity_pct": latest.get("ambient_humidity_pct"),
        "latest_captured_at": latest.get("captured_at"),
    }


def _baseline_status(
    monthly_rows: list[dict[str, Any]],
    live_summary: dict[str, Any],
    latest_reading: NestThermostatReading | None,
) -> str:
    if latest_reading and latest_reading.hvac_status == "COOLING":
        return "AC is currently cooling; avoid treating Super as free heat until shadow data is collected."
    if live_summary["readings_count"] < 2:
        return "Need more live readings before deriving current cooling runtime."
    if not monthly_rows:
        return "No historical monthly backfill loaded; baseline will be live-only."
    return "Baseline inputs are available; keep guardrails in shadow mode until thresholds are tuned."


def _parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _month_key(value: datetime) -> str:
    return f"{value.year:04d}-{value.month:02d}"
