from __future__ import annotations

import csv
import json
import statistics
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .config import F2PoolConfig
from .f2pool_client import F2PoolAccountSnapshot
from .state_store import SchedulerState


BASELINE_MODES = {"eco", "super"}


@dataclass(frozen=True)
class F2PoolProfitabilityRecord:
    captured_at: str
    miner_mode: str | None
    mode_source: str
    snapshot: dict[str, Any]
    rate: dict[str, Any]
    baseline: dict[str, Any]
    shadow_decision: dict[str, Any]
    source: dict[str, Any] | None = None

    def to_json_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_summary_dict(self) -> dict[str, Any]:
        return {
            "captured_at": self.captured_at,
            "miner_mode": self.miner_mode,
            "hashrate_th_s": self.snapshot.get("hashrate_th_s"),
            "value_today": self.snapshot.get("value_today"),
            "btc_per_hour": self.rate.get("btc_per_hour"),
            "btc_per_th_hour": self.rate.get("btc_per_th_hour"),
            "baseline_mode": self.baseline.get("mode"),
            "baseline_samples": self.baseline.get("sample_count"),
            "baseline_btc_per_th_hour": self.baseline.get("btc_per_th_hour_median"),
            "profitability_ratio": self.shadow_decision.get("profitability_ratio"),
            "shadow_signal": self.shadow_decision.get("signal"),
            "shadow_reason": self.shadow_decision.get("reason"),
        }


def miner_mode_from_state(state: SchedulerState) -> tuple[str | None, str]:
    if state.softoff_active is True:
        return "off", "state.softoff_active"
    if state.current_known_mode:
        return state.current_known_mode, "state.current_known_mode"
    if state.last_commanded_mode:
        return state.last_commanded_mode, "state.last_commanded_mode"
    return None, "unknown"


def load_profitability_records(path: str | Path) -> list[dict[str, Any]]:
    path = Path(path)
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(data, dict):
                records.append(data)
    return records


def append_profitability_record(path: str | Path, record: F2PoolProfitabilityRecord) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record.to_json_dict(), sort_keys=True))
        handle.write("\n")


def import_settlement_csv_baselines(
    *,
    csv_path: str | Path,
    output_path: str | Path,
    config: F2PoolConfig,
    currency: str,
    mining_user: str,
    dry_run: bool = False,
) -> dict[str, Any]:
    csv_path = Path(csv_path)
    history = load_profitability_records(output_path)
    existing_keys = {
        _historical_source_key(row.get("source"))
        for row in history
        if isinstance(row.get("source"), dict)
    }
    existing_keys.discard(None)

    rows = _read_settlement_csv(csv_path)
    records: list[F2PoolProfitabilityRecord] = []
    skipped_mixed = 0
    skipped_invalid = 0
    skipped_duplicate = 0
    mode_counts = {"eco": 0, "super": 0}

    for row in rows:
        mode = _mode_from_daily_hashrate(row["hashrate_th_s"], config)
        if mode is None:
            skipped_mixed += 1
            continue

        source = {
            "kind": "f2pool_settlement_csv",
            "path": str(csv_path),
            "settlement_time": row["settlement_time"],
            "inferred_mode": mode,
            "inference": "daily average hashrate threshold",
        }
        key = _historical_source_key(source)
        if key in existing_keys:
            skipped_duplicate += 1
            continue

        btc_per_th_hour = row["amount_total_btc"] / (row["hashrate_th_s"] * 24)
        if btc_per_th_hour <= 0:
            skipped_invalid += 1
            continue

        record = F2PoolProfitabilityRecord(
            captured_at=row["captured_at"],
            miner_mode=mode,
            mode_source="historical_f2pool_csv",
            snapshot={
                "captured_at": row["captured_at"],
                "currency": currency,
                "mining_user": mining_user,
                "hashrate_th_s": row["hashrate_th_s"],
                "value_today": row["amount_total_btc"],
                "worker_length": None,
                "worker_length_online": None,
                "worker_names": [],
            },
            rate={
                "previous_captured_at": None,
                "previous_value_today": None,
                "elapsed_minutes": 1440,
                "delta_value_today_btc": row["amount_total_btc"],
                "btc_per_hour": row["amount_total_btc"] / 24,
                "btc_per_th_hour": btc_per_th_hour,
                "sample_usable_for_baseline": True,
                "sample_rejection_reason": None,
                "sample_source": "historical_f2pool_settlement_csv",
            },
            baseline={
                "mode": mode,
                "sample_count": 0,
                "btc_per_th_hour_median": None,
                "btc_per_th_hour_average": None,
                "reason": "historical seed row",
            },
            shadow_decision={
                "signal": "historical_seed",
                "profitability_ratio": None,
                "reason": "seeded from F2Pool settlement CSV",
            },
            source=source,
        )
        records.append(record)
        existing_keys.add(key)
        mode_counts[mode] += 1

    if not dry_run:
        for record in records:
            append_profitability_record(output_path, record)

    return {
        "csv_path": str(csv_path),
        "output_path": str(output_path),
        "dry_run": dry_run,
        "rows_read": len(rows),
        "rows_imported": len(records),
        "eco_rows_imported": mode_counts["eco"],
        "super_rows_imported": mode_counts["super"],
        "rows_skipped_mixed_or_partial": skipped_mixed,
        "rows_skipped_invalid": skipped_invalid,
        "rows_skipped_duplicate": skipped_duplicate,
        "thresholds": {
            "eco_min_hashrate_th_s": config.csv_seed_eco_min_hashrate_th_s,
            "eco_max_hashrate_th_s": config.csv_seed_eco_max_hashrate_th_s,
            "super_min_hashrate_th_s": config.csv_seed_super_min_hashrate_th_s,
            "super_max_hashrate_th_s": config.csv_seed_super_max_hashrate_th_s,
        },
        "imported_baselines": _baseline_summary_from_records(records),
    }


def build_profitability_record(
    *,
    snapshot: F2PoolAccountSnapshot,
    history: list[dict[str, Any]],
    config: F2PoolConfig,
    miner_mode: str | None,
    mode_source: str,
    last_commanded_at: str | None,
) -> F2PoolProfitabilityRecord:
    previous = _latest_prior_snapshot(history, snapshot)
    rate = _rate_dict(
        snapshot=snapshot,
        previous=previous,
        config=config,
        miner_mode=miner_mode,
        last_commanded_at=last_commanded_at,
    )
    baseline = _baseline_dict(
        history=history,
        config=config,
        snapshot=snapshot,
        miner_mode=miner_mode,
    )
    shadow_decision = _shadow_decision_dict(
        miner_mode=miner_mode,
        rate=rate,
        baseline=baseline,
        config=config,
    )
    return F2PoolProfitabilityRecord(
        captured_at=snapshot.captured_at,
        miner_mode=miner_mode,
        mode_source=mode_source,
        snapshot=snapshot.to_summary_dict(include_raw=True),
        rate=rate,
        baseline=baseline,
        shadow_decision=shadow_decision,
    )


def _latest_prior_snapshot(
    history: list[dict[str, Any]],
    snapshot: F2PoolAccountSnapshot,
) -> dict[str, Any] | None:
    current_at = _parse_datetime(snapshot.captured_at)
    candidates = []
    for row in history:
        row_snapshot = _snapshot_from_row(row)
        if row_snapshot.get("currency") != snapshot.currency:
            continue
        if row_snapshot.get("mining_user") != snapshot.mining_user:
            continue
        captured_at = _parse_datetime(row_snapshot.get("captured_at"))
        if captured_at is None:
            continue
        if current_at and captured_at >= current_at:
            continue
        if _number(row_snapshot.get("value_today")) is None:
            continue
        candidates.append((captured_at, row))
    if not candidates:
        return None
    return max(candidates, key=lambda item: item[0])[1]


def _rate_dict(
    *,
    snapshot: F2PoolAccountSnapshot,
    previous: dict[str, Any] | None,
    config: F2PoolConfig,
    miner_mode: str | None,
    last_commanded_at: str | None,
) -> dict[str, Any]:
    if previous is None:
        return _empty_rate("no previous F2Pool snapshot")

    previous_snapshot = _snapshot_from_row(previous)
    previous_at = _parse_datetime(previous_snapshot.get("captured_at"))
    current_at = _parse_datetime(snapshot.captured_at)
    if previous_at is None or current_at is None:
        return _empty_rate("snapshot timestamp unavailable")

    elapsed_minutes = (current_at - previous_at).total_seconds() / 60
    if elapsed_minutes < config.min_rate_sample_elapsed_minutes:
        return _empty_rate("previous snapshot is too close", elapsed_minutes=elapsed_minutes)
    if elapsed_minutes > config.max_rate_sample_age_minutes:
        return _empty_rate("previous snapshot is too old", elapsed_minutes=elapsed_minutes)

    previous_value = _number(previous_snapshot.get("value_today"))
    current_value = snapshot.value_today
    if previous_value is None or current_value is None:
        return _empty_rate("value_today unavailable", elapsed_minutes=elapsed_minutes)

    delta_btc = current_value - previous_value
    if delta_btc < 0:
        return _empty_rate("value_today reset or moved backward", elapsed_minutes=elapsed_minutes)

    elapsed_hours = elapsed_minutes / 60
    btc_per_hour = delta_btc / elapsed_hours if elapsed_hours > 0 else None
    hashrate_th_s = snapshot.hashrate_th_s
    btc_per_th_hour = (
        btc_per_hour / hashrate_th_s
        if btc_per_hour is not None and hashrate_th_s and hashrate_th_s > 0
        else None
    )

    usable, rejection_reason = _baseline_usability(
        previous_at=previous_at,
        current_at=current_at,
        miner_mode=miner_mode,
        btc_per_th_hour=btc_per_th_hour,
        last_commanded_at=last_commanded_at,
    )
    return {
        "previous_captured_at": previous_snapshot.get("captured_at"),
        "previous_value_today": previous_value,
        "elapsed_minutes": elapsed_minutes,
        "delta_value_today_btc": delta_btc,
        "btc_per_hour": btc_per_hour,
        "btc_per_th_hour": btc_per_th_hour,
        "sample_usable_for_baseline": usable,
        "sample_rejection_reason": rejection_reason,
    }


def _empty_rate(reason: str, *, elapsed_minutes: float | None = None) -> dict[str, Any]:
    return {
        "previous_captured_at": None,
        "previous_value_today": None,
        "elapsed_minutes": elapsed_minutes,
        "delta_value_today_btc": None,
        "btc_per_hour": None,
        "btc_per_th_hour": None,
        "sample_usable_for_baseline": False,
        "sample_rejection_reason": reason,
    }


def _baseline_usability(
    *,
    previous_at: datetime,
    current_at: datetime,
    miner_mode: str | None,
    btc_per_th_hour: float | None,
    last_commanded_at: str | None,
) -> tuple[bool, str | None]:
    if miner_mode not in BASELINE_MODES:
        return False, "miner mode is not eco or super"
    if btc_per_th_hour is None or btc_per_th_hour <= 0:
        return False, "rate is unavailable or zero"
    command_at = _parse_datetime(last_commanded_at)
    if command_at and previous_at < command_at <= current_at:
        return False, "sample crosses a mode change"
    return True, None


def _baseline_dict(
    *,
    history: list[dict[str, Any]],
    config: F2PoolConfig,
    snapshot: F2PoolAccountSnapshot,
    miner_mode: str | None,
) -> dict[str, Any]:
    if miner_mode not in BASELINE_MODES:
        return {
            "mode": miner_mode,
            "sample_count": 0,
            "btc_per_th_hour_median": None,
            "btc_per_th_hour_average": None,
            "reason": "no baseline for off/unknown mode",
        }

    current_at = _parse_datetime(snapshot.captured_at) or datetime.now().astimezone()
    cutoff = current_at - timedelta(days=config.baseline_window_days)
    historical_cutoff = current_at - timedelta(days=config.historical_seed_baseline_window_days)
    samples = []
    for row in history:
        row_snapshot = _snapshot_from_row(row)
        if row_snapshot.get("currency") != snapshot.currency:
            continue
        if row_snapshot.get("mining_user") != snapshot.mining_user:
            continue
        if row.get("miner_mode") != miner_mode:
            continue
        captured_at = _parse_datetime(row.get("captured_at") or row_snapshot.get("captured_at"))
        row_cutoff = historical_cutoff if _is_historical_seed(row) else cutoff
        if captured_at and captured_at < row_cutoff:
            continue
        rate = row.get("rate")
        if not isinstance(rate, dict) or not rate.get("sample_usable_for_baseline"):
            continue
        value = _number(rate.get("btc_per_th_hour"))
        if value is not None and value > 0:
            samples.append(value)

    if not samples:
        return {
            "mode": miner_mode,
            "sample_count": 0,
            "btc_per_th_hour_median": None,
            "btc_per_th_hour_average": None,
            "reason": "collecting first baseline samples",
        }

    return {
        "mode": miner_mode,
        "sample_count": len(samples),
        "btc_per_th_hour_median": statistics.median(samples),
        "btc_per_th_hour_average": sum(samples) / len(samples),
        "reason": None,
    }


def _shadow_decision_dict(
    *,
    miner_mode: str | None,
    rate: dict[str, Any],
    baseline: dict[str, Any],
    config: F2PoolConfig,
) -> dict[str, Any]:
    current = _number(rate.get("btc_per_th_hour"))
    baseline_value = _number(baseline.get("btc_per_th_hour_median"))
    sample_count = int(baseline.get("sample_count") or 0)

    if current is None:
        return {
            "signal": "collecting_rate",
            "profitability_ratio": None,
            "reason": rate.get("sample_rejection_reason") or "rate unavailable",
        }
    if baseline_value is None or sample_count < config.baseline_min_samples_per_mode:
        return {
            "signal": "collecting_baseline",
            "profitability_ratio": None,
            "reason": (
                f"have {sample_count} {miner_mode or 'unknown'} baseline samples; "
                f"need {config.baseline_min_samples_per_mode}"
            ),
        }

    ratio = current / baseline_value if baseline_value > 0 else None
    if ratio is None:
        return {
            "signal": "collecting_baseline",
            "profitability_ratio": None,
            "reason": "baseline is zero",
        }

    if miner_mode == "super":
        if ratio >= config.shadow_hold_super_ratio:
            return {
                "signal": "would_hold_super",
                "profitability_ratio": ratio,
                "reason": f"current rate is {ratio:.2f}x super baseline",
            }
        if ratio < config.shadow_exit_super_ratio:
            return {
                "signal": "would_return_to_eco",
                "profitability_ratio": ratio,
                "reason": f"current rate fell to {ratio:.2f}x super baseline",
            }
        return {
            "signal": "watching_super",
            "profitability_ratio": ratio,
            "reason": f"current rate is {ratio:.2f}x super baseline",
        }

    if miner_mode == "eco" and ratio >= config.shadow_enter_super_ratio:
        return {
            "signal": "would_enter_super",
            "profitability_ratio": ratio,
            "reason": f"current rate is {ratio:.2f}x eco baseline",
        }

    return {
        "signal": "normal",
        "profitability_ratio": ratio,
        "reason": f"current rate is {ratio:.2f}x {miner_mode or 'unknown'} baseline",
    }


def _snapshot_from_row(row: dict[str, Any]) -> dict[str, Any]:
    snapshot = row.get("snapshot")
    if isinstance(snapshot, dict):
        return snapshot
    return row


def _read_settlement_csv(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        raise FileNotFoundError(f"F2Pool settlement CSV not found: {path}")
    rows = []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            settlement_time = str(row.get("Time of Settlement", "")).strip()
            hashrate = _number(row.get("Avg. Hashrate Per Day"))
            amount = _number(row.get("Amount (Total)"))
            unit = str(row.get("Unit", "")).strip()
            if not settlement_time or hashrate is None or amount is None:
                continue
            if hashrate <= 0 or amount <= 0:
                continue
            rows.append(
                {
                    "settlement_time": settlement_time,
                    "captured_at": _settlement_time_to_iso(settlement_time),
                    "hashrate_th_s": _hashrate_to_th_s(hashrate, unit),
                    "amount_total_btc": amount,
                }
            )
    return rows


def _hashrate_to_th_s(value: float, unit: str) -> float:
    normalized = unit.strip().lower()
    if normalized in {"ph/s", "ph"}:
        return value * 1_000
    if normalized in {"gh/s", "gh"}:
        return value / 1_000
    return value


def _settlement_time_to_iso(value: str) -> str:
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, fmt).astimezone().isoformat()
        except ValueError:
            continue
    parsed = _parse_datetime(value)
    return (parsed or datetime.now().astimezone()).isoformat()


def _mode_from_daily_hashrate(hashrate_th_s: float, config: F2PoolConfig) -> str | None:
    if config.csv_seed_eco_min_hashrate_th_s <= hashrate_th_s <= config.csv_seed_eco_max_hashrate_th_s:
        return "eco"
    if config.csv_seed_super_min_hashrate_th_s <= hashrate_th_s <= config.csv_seed_super_max_hashrate_th_s:
        return "super"
    return None


def _historical_source_key(source: dict[str, Any] | None) -> tuple[str, str, str] | None:
    if not source or source.get("kind") != "f2pool_settlement_csv":
        return None
    return (
        str(source.get("kind")),
        str(source.get("settlement_time")),
        str(source.get("inferred_mode")),
    )


def _is_historical_seed(row: dict[str, Any]) -> bool:
    source = row.get("source")
    return isinstance(source, dict) and source.get("kind") == "f2pool_settlement_csv"


def _baseline_summary_from_records(records: list[F2PoolProfitabilityRecord]) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    for mode in sorted(BASELINE_MODES):
        mode_records = [record for record in records if record.miner_mode == mode]
        normalized_values = [
            _number(record.rate.get("btc_per_th_hour"))
            for record in mode_records
        ]
        normalized_values = [
            value for value in normalized_values if value is not None and value > 0
        ]
        btc_per_day_values = [
            _number(record.rate.get("delta_value_today_btc"))
            for record in mode_records
        ]
        btc_per_day_values = [
            value for value in btc_per_day_values if value is not None and value > 0
        ]
        hashrate_values = [
            _number(record.snapshot.get("hashrate_th_s"))
            for record in mode_records
        ]
        hashrate_values = [
            value for value in hashrate_values if value is not None and value > 0
        ]
        btc_per_day_median = (
            statistics.median(btc_per_day_values) if btc_per_day_values else None
        )
        btc_per_day_average = (
            sum(btc_per_day_values) / len(btc_per_day_values)
            if btc_per_day_values
            else None
        )
        summary[mode] = {
            "sample_count": len(normalized_values),
            "hashrate_th_s_median": statistics.median(hashrate_values)
            if hashrate_values
            else None,
            "btc_per_day_median": btc_per_day_median,
            "btc_per_day_average": btc_per_day_average,
            "sats_per_day_median": round(btc_per_day_median * 100_000_000)
            if btc_per_day_median is not None
            else None,
            "sats_per_day_average": round(btc_per_day_average * 100_000_000)
            if btc_per_day_average is not None
            else None,
            "btc_per_th_hour_median": statistics.median(normalized_values)
            if normalized_values
            else None,
            "btc_per_th_hour_average": sum(normalized_values) / len(normalized_values)
            if normalized_values
            else None,
        }
    return summary


def _parse_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.astimezone()
    return parsed


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None
