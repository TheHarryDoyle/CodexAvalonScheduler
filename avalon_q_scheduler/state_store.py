from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


@dataclass
class SchedulerState:
    current_known_mode: str | None = None
    previous_mode: str | None = None
    last_commanded_mode: str | None = None
    last_commanded_at: str | None = None
    last_reason: str | None = None
    last_weather_decision: dict[str, Any] | None = None
    last_api_response: str | None = None
    mode_before_cheap_power: str | None = None
    softoff_active: bool | None = None
    automation_paused: bool = False
    tesla_api_usage_date: str | None = None
    tesla_api_requests_today: int = 0
    energy_policy_snapshot: dict[str, Any] | None = None


class StateStore:
    def __init__(self, path: str | Path = "state.json"):
        self.path = Path(path)

    def load(self) -> SchedulerState:
        if not self.path.exists():
            return SchedulerState()

        with self.path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
        if not isinstance(data, dict):
            raise ValueError(f"State file must contain a JSON object: {self.path}")
        return SchedulerState(**{**asdict(SchedulerState()), **data})

    def save(self, state: SchedulerState) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("w", encoding="utf-8") as handle:
            json.dump(asdict(state), handle, indent=2, sort_keys=True)
            handle.write("\n")
