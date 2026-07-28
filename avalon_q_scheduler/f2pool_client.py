from __future__ import annotations

import json
import os
import csv
import re
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx

from .config import F2PoolConfig


@dataclass(frozen=True)
class F2PoolCredentials:
    api_token: str


@dataclass(frozen=True)
class F2PoolAccountSnapshot:
    captured_at: str
    currency: str
    mining_user: str
    hashrate_h_s: float | None
    hashes_last_hour: float | None
    hashes_last_day: float | None
    value_today: float | None
    balance: float | None
    worker_length: int | None
    worker_length_online: int | None
    stale_hashes_rejected_last_hour: float | None
    stale_hashes_rejected_last_day: float | None
    worker_names: list[str]
    raw: dict[str, Any]
    pool_hashrate_h_s: float | None = None
    network_hashrate_h_s: float | None = None
    current_difficulty: float | None = None
    next_difficulty: float | None = None
    difficulty_change_percent: float | None = None
    difficulty_adjustment_at: str | None = None

    @property
    def hashrate_th_s(self) -> float | None:
        if self.hashrate_h_s is None:
            return None
        return self.hashrate_h_s / 1_000_000_000_000

    @property
    def pool_hashrate_th_s(self) -> float | None:
        if self.pool_hashrate_h_s is None:
            return None
        return self.pool_hashrate_h_s / 1_000_000_000_000

    @property
    def network_hashrate_th_s(self) -> float | None:
        if self.network_hashrate_h_s is None:
            return None
        return self.network_hashrate_h_s / 1_000_000_000_000

    def to_json_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["hashrate_th_s"] = self.hashrate_th_s
        data["pool_hashrate_th_s"] = self.pool_hashrate_th_s
        data["network_hashrate_th_s"] = self.network_hashrate_th_s
        return data

    def to_summary_dict(self, *, include_raw: bool = False) -> dict[str, Any]:
        summary = {
            "captured_at": self.captured_at,
            "currency": self.currency,
            "mining_user": self.mining_user,
            "hashrate_h_s": self.hashrate_h_s,
            "hashrate_th_s": self.hashrate_th_s,
            "hashes_last_hour": self.hashes_last_hour,
            "hashes_last_day": self.hashes_last_day,
            "value_today": self.value_today,
            "balance": self.balance,
            "worker_length": self.worker_length,
            "worker_length_online": self.worker_length_online,
            "stale_hashes_rejected_last_hour": self.stale_hashes_rejected_last_hour,
            "stale_hashes_rejected_last_day": self.stale_hashes_rejected_last_day,
            "worker_names": self.worker_names,
            "pool_hashrate_h_s": self.pool_hashrate_h_s,
            "pool_hashrate_th_s": self.pool_hashrate_th_s,
            "network_hashrate_h_s": self.network_hashrate_h_s,
            "network_hashrate_th_s": self.network_hashrate_th_s,
            "current_difficulty": self.current_difficulty,
            "next_difficulty": self.next_difficulty,
            "difficulty_change_percent": self.difficulty_change_percent,
            "difficulty_adjustment_at": self.difficulty_adjustment_at,
        }
        if include_raw:
            summary["raw"] = self.raw
        return summary


@dataclass(frozen=True)
class BitcoinNetworkContext:
    captured_at: str
    provider: str
    pool_slug: str
    pool_hashrate_h_s: float | None
    network_hashrate_h_s: float | None
    current_difficulty: float | None
    next_difficulty: float | None
    difficulty_change_percent: float | None
    difficulty_adjustment_at: str | None
    raw: dict[str, Any]


@dataclass(frozen=True)
class F2PoolRewardSettlement:
    settlement_time: str
    mining_date: str | None
    amount_btc: float
    avg_hashrate_th_s: float | None
    difficulty: str | None
    description: str | None
    transaction_type: str | None
    created_at: str | None


class F2PoolRevenuePermissionError(RuntimeError):
    """Raised when the token cannot read F2Pool revenue transaction rows."""


def credentials_from_env(config: F2PoolConfig) -> F2PoolCredentials:
    api_token = os.environ.get(config.api_token_env, "").strip()
    if not api_token:
        raise ValueError(f"Set {config.api_token_env} before using F2Pool API.")
    return F2PoolCredentials(api_token=api_token)


def mining_user_from_env_or_config(config: F2PoolConfig) -> str:
    mining_user = os.environ.get(config.mining_user_env, "").strip() or config.mining_user.strip()
    if not mining_user:
        raise ValueError(
            f"Set {config.mining_user_env} or f2pool.mining_user before using F2Pool API."
        )
    return mining_user


def append_snapshot(path: str | Path, snapshot: F2PoolAccountSnapshot) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(snapshot.to_json_dict(), sort_keys=True))
        handle.write("\n")


class F2PoolClient:
    def __init__(
        self,
        config: F2PoolConfig,
        credentials: F2PoolCredentials,
    ):
        self.config = config
        self.credentials = credentials

    def get_account_details(self, mining_user: str | None = None) -> dict[str, Any]:
        user = mining_user or mining_user_from_env_or_config(self.config)
        url = f"{self.config.api_base_url.rstrip('/')}/{self.config.currency}/{user}"
        with httpx.Client(timeout=self.config.timeout_seconds) as client:
            response = client.get(
                url,
                headers={
                    "Content-Type": "application/json",
                    "F2P-API-SECRET": self.credentials.api_token,
                    "User-Agent": "avalon-q-scheduler/1.0",
                },
            )
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict):
            raise RuntimeError("F2Pool account endpoint did not return a JSON object.")
        return data

    def get_account_snapshot(self, mining_user: str | None = None) -> F2PoolAccountSnapshot:
        user = mining_user or mining_user_from_env_or_config(self.config)
        data = self.get_account_details(user)
        return snapshot_from_account_details(
            data,
            currency=self.config.currency,
            mining_user=user,
        )

    def list_revenue_transactions(
        self,
        *,
        mining_user: str | None = None,
        start_time: int | None = None,
        end_time: int | None = None,
    ) -> dict[str, Any]:
        user = mining_user or mining_user_from_env_or_config(self.config)
        url = f"{self.config.api_v2_base_url.rstrip('/')}/assets/transactions/list"
        body: dict[str, Any] = {
            "currency": self.config.currency,
            "mining_user_name": user,
            "type": "revenue",
        }
        if start_time is not None:
            body["start_time"] = int(start_time)
        if end_time is not None:
            body["end_time"] = int(end_time)

        with httpx.Client(timeout=self.config.timeout_seconds) as client:
            response = client.post(
                url,
                json=body,
                headers={
                    "Content-Type": "application/json",
                    "F2P-API-SECRET": self.credentials.api_token,
                    "User-Agent": "avalon-q-scheduler/1.0",
                },
            )
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict):
            raise RuntimeError("F2Pool transactions endpoint did not return a JSON object.")
        _raise_for_f2pool_v2_error(data)
        return data

    def get_reward_settlements(
        self,
        *,
        mining_user: str | None = None,
        days: int | None = None,
        end_time: int | None = None,
    ) -> list[F2PoolRewardSettlement]:
        end = int(end_time or datetime.now(timezone.utc).timestamp())
        backfill_days = days if days is not None else self.config.rewards_backfill_days
        start = end - max(1, int(backfill_days)) * 24 * 60 * 60
        data = self.list_revenue_transactions(
            mining_user=mining_user,
            start_time=start,
            end_time=end,
        )
        return reward_settlements_from_transactions_payload(data)


class BitcoinNetworkContextClient:
    def __init__(self, config: F2PoolConfig):
        self.config = config

    def get_context(self) -> BitcoinNetworkContext:
        base_url = self.config.network_context_api_base_url.rstrip("/")
        pool_slug = self.config.network_context_pool_slug.strip() or "f2pool"
        with httpx.Client(timeout=self.config.timeout_seconds) as client:
            difficulty_adjustment = _get_json_object(
                client,
                f"{base_url}/difficulty-adjustment",
            )
            hashrate = _get_json_object(
                client,
                f"{base_url}/mining/hashrate/3d",
            )
            pool = _get_json_object(
                client,
                f"{base_url}/mining/pool/{pool_slug}",
            )

        return network_context_from_mempool_payloads(
            difficulty_adjustment=difficulty_adjustment,
            hashrate=hashrate,
            pool=pool,
            pool_slug=pool_slug,
        )


class F2PoolNetworkStatusClient:
    def __init__(self, config: F2PoolConfig):
        self.config = config

    def get_context(self) -> BitcoinNetworkContext:
        url = self.config.network_status_page_url
        with httpx.Client(timeout=self.config.timeout_seconds, follow_redirects=True) as client:
            page = client.get(url, headers={"User-Agent": "avalon-q-scheduler/1.0"})
            page.raise_for_status()
            chart = client.post(
                url,
                data={
                    "action": "load_chart_hashrate",
                    "currency_code": "btc",
                    "history_days": "30d",
                    "interval": "60m",
                },
                headers={"User-Agent": "avalon-q-scheduler/1.0"},
            )
            chart_payload: dict[str, Any] = {}
            if chart.status_code < 400:
                try:
                    parsed = chart.json()
                except ValueError:
                    parsed = {}
                if isinstance(parsed, dict):
                    chart_payload = parsed

        return network_context_from_f2pool_status_page(
            page.text,
            chart_payload=chart_payload,
            page_url=url,
        )


def snapshot_from_account_details(
    data: dict[str, Any],
    *,
    currency: str,
    mining_user: str,
) -> F2PoolAccountSnapshot:
    workers = data.get("workers")
    worker_names = _worker_names(workers)
    raw = _sanitized_account_details(data)
    return F2PoolAccountSnapshot(
        captured_at=datetime.now().astimezone().isoformat(),
        currency=currency,
        mining_user=mining_user,
        hashrate_h_s=_number(data.get("hashrate")),
        hashes_last_hour=_number(data.get("hashes_last_hour")),
        hashes_last_day=_number(data.get("hashes_last_day")),
        value_today=_number(data.get("value_today")),
        balance=_number(data.get("balance")),
        worker_length=_int(data.get("worker_length")),
        worker_length_online=_int(data.get("worker_length_online")),
        stale_hashes_rejected_last_hour=_number(data.get("stale_hashes_rejected_last_hour")),
        stale_hashes_rejected_last_day=_number(data.get("stale_hashes_rejected_last_day")),
        worker_names=worker_names,
        raw=raw,
        pool_hashrate_h_s=_first_number(
            raw,
            (
                "pool_hashrate_h_s",
                "pool_hashrate",
                "pool_hash_rate",
                "pool_rate_h_s",
                "pool_rate",
            ),
        ),
        network_hashrate_h_s=_first_number(
            raw,
            (
                "network_hashrate_h_s",
                "network_hashrate",
                "global_hashrate_h_s",
                "global_hashrate",
                "global_hash_rate",
            ),
        ),
        current_difficulty=_first_number(
            raw,
            ("current_difficulty", "network_difficulty", "difficulty"),
        ),
        next_difficulty=_first_number(
            raw,
            ("next_difficulty", "estimated_next_difficulty", "estimated_difficulty"),
        ),
        difficulty_change_percent=_first_number(
            raw,
            (
                "difficulty_change_percent",
                "estimated_difficulty_change_percent",
                "next_difficulty_change_percent",
            ),
        ),
        difficulty_adjustment_at=_first_str(
            raw,
            ("difficulty_adjustment_at", "difficulty_adjustment_time", "next_difficulty_time"),
        ),
    )


def network_context_from_mempool_payloads(
    *,
    difficulty_adjustment: dict[str, Any],
    hashrate: dict[str, Any],
    pool: dict[str, Any],
    pool_slug: str = "f2pool",
    captured_at: str | None = None,
) -> BitcoinNetworkContext:
    current_difficulty = _first_number(
        hashrate,
        ("currentDifficulty", "current_difficulty", "difficulty"),
    )
    difficulty_change_percent = _first_number(
        difficulty_adjustment,
        ("difficultyChange", "difficulty_change_percent"),
    )
    next_difficulty = _first_number(
        difficulty_adjustment,
        ("nextDifficulty", "next_difficulty", "estimated_next_difficulty"),
    )
    if (
        next_difficulty is None
        and current_difficulty is not None
        and difficulty_change_percent is not None
    ):
        next_difficulty = current_difficulty * (1 + difficulty_change_percent / 100)

    return BitcoinNetworkContext(
        captured_at=captured_at or datetime.now().astimezone().isoformat(),
        provider="mempool.space",
        pool_slug=pool_slug,
        pool_hashrate_h_s=_first_positive_number(
            pool,
            (
                "reportedHashrate",
                "reported_hashrate",
                "estimatedHashrate",
                "estimated_hashrate",
            ),
        ),
        network_hashrate_h_s=_first_positive_number(
            hashrate,
            ("currentHashrate", "current_hashrate", "network_hashrate_h_s"),
        ),
        current_difficulty=current_difficulty,
        next_difficulty=next_difficulty,
        difficulty_change_percent=difficulty_change_percent,
        difficulty_adjustment_at=_retarget_timestamp_to_iso(
            difficulty_adjustment.get("estimatedRetargetDate")
        )
        or _first_str(
            difficulty_adjustment,
            ("estimated_retarget_date", "difficulty_adjustment_at"),
        ),
        raw=_sanitized_network_context_raw(
            difficulty_adjustment=difficulty_adjustment,
            hashrate=hashrate,
            pool=pool,
            pool_slug=pool_slug,
        ),
    )


def network_context_from_f2pool_status_page(
    html: str,
    *,
    chart_payload: dict[str, Any] | None = None,
    page_url: str = "https://www.f2pool.com/coin/bitcoin",
    captured_at: str | None = None,
) -> BitcoinNetworkContext:
    current_difficulty = _data_origin_after_title(html, "Difficulty")
    next_difficulty = _data_origin_after_title(html, "Next Difficulty Change Estimate")
    change_percent = _difficulty_change_percent_after_title(
        html,
        "Next Difficulty Change Estimate",
    )
    adjustment_at = _difficulty_eta_after_title(html, "Time Until Difficulty Change")
    network_hashrate = _latest_chart_hashrate(chart_payload or {})
    if network_hashrate is None:
        network_hashrate = _first_float_match(
            html,
            r"CURRENCY_FUNC\.getScale\(\s*([0-9.eE+-]+)",
        )
    pool_hashrate = _data_origin_after_title(html, "Pool Hashrate")

    raw = {
        "provider": "f2pool_network_status",
        "network_context_source": "f2pool_network_status",
        "page_url": page_url,
        "f2pool_network_status_hashrate_h_s": network_hashrate,
        "f2pool_network_status_pool_hashrate_h_s": pool_hashrate,
        "f2pool_network_status_difficulty": current_difficulty,
        "f2pool_network_status_next_difficulty": next_difficulty,
        "f2pool_network_status_difficulty_change_percent": change_percent,
        "f2pool_difficulty_adjustment_at": adjustment_at,
    }
    return BitcoinNetworkContext(
        captured_at=captured_at or datetime.now().astimezone().isoformat(),
        provider="f2pool_network_status",
        pool_slug="f2pool",
        pool_hashrate_h_s=pool_hashrate,
        network_hashrate_h_s=network_hashrate,
        current_difficulty=current_difficulty,
        next_difficulty=next_difficulty,
        difficulty_change_percent=change_percent,
        difficulty_adjustment_at=adjustment_at,
        raw={key: value for key, value in raw.items() if value is not None},
    )


def snapshot_with_network_context(
    snapshot: F2PoolAccountSnapshot,
    context: BitcoinNetworkContext,
) -> F2PoolAccountSnapshot:
    raw = dict(snapshot.raw)
    raw["network_context"] = context.raw
    raw.setdefault("network_context_source", context.provider)
    raw.setdefault("network_context_provider", context.provider)
    if context.provider.startswith("f2pool"):
        raw["f2pool_network_status_hashrate_h_s"] = context.network_hashrate_h_s
        if context.raw.get("pool_hashrate_source") != "mempool_space":
            raw["f2pool_network_status_pool_hashrate_h_s"] = context.pool_hashrate_h_s
        raw["f2pool_network_status_difficulty"] = context.current_difficulty
        raw["f2pool_network_status_next_difficulty"] = context.next_difficulty
        raw["f2pool_network_status_difficulty_change_percent"] = (
            context.difficulty_change_percent
        )
        raw["f2pool_difficulty_adjustment_at"] = context.difficulty_adjustment_at
    for key in (
        "pool_hashrate_source",
        "pool_hashrate_label",
        "pool_hashrate_estimate_h_s",
        "mempool_pool_hashrate_h_s",
        "mempool_pool_block_share_24h",
        "mempool_pool_block_share_1w",
    ):
        if key in context.raw:
            raw[key] = context.raw[key]
    return replace(
        snapshot,
        raw=raw,
        pool_hashrate_h_s=_coalesce(context.pool_hashrate_h_s, snapshot.pool_hashrate_h_s),
        network_hashrate_h_s=_coalesce(
            context.network_hashrate_h_s,
            snapshot.network_hashrate_h_s,
        ),
        current_difficulty=_coalesce(
            context.current_difficulty,
            snapshot.current_difficulty,
        ),
        next_difficulty=_coalesce(context.next_difficulty, snapshot.next_difficulty),
        difficulty_change_percent=_coalesce(
            context.difficulty_change_percent,
            snapshot.difficulty_change_percent,
        ),
        difficulty_adjustment_at=_coalesce(
            context.difficulty_adjustment_at,
            snapshot.difficulty_adjustment_at,
        ),
    )


def network_context_with_pool_hashrate_estimate(
    context: BitcoinNetworkContext,
    fallback: BitcoinNetworkContext,
) -> BitcoinNetworkContext:
    if context.pool_hashrate_h_s is not None or fallback.pool_hashrate_h_s is None:
        return context

    source = (
        "mempool_space"
        if "mempool" in fallback.provider.lower()
        else fallback.provider.replace(".", "_").replace("-", "_")
    )
    label = (
        "mempool.space public estimate"
        if source == "mempool_space"
        else f"{fallback.provider} estimate"
    )
    raw = dict(context.raw)
    raw["pool_hashrate_source"] = source
    raw["pool_hashrate_label"] = label
    raw["pool_hashrate_estimate_h_s"] = fallback.pool_hashrate_h_s
    if source == "mempool_space":
        raw["mempool_pool_hashrate_h_s"] = fallback.pool_hashrate_h_s

    pool = fallback.raw.get("pool") if isinstance(fallback.raw.get("pool"), dict) else {}
    block_share = pool.get("blockShare") if isinstance(pool.get("blockShare"), dict) else {}
    if "24h" in block_share:
        raw["mempool_pool_block_share_24h"] = block_share["24h"]
    if "1w" in block_share:
        raw["mempool_pool_block_share_1w"] = block_share["1w"]

    return replace(context, pool_hashrate_h_s=fallback.pool_hashrate_h_s, raw=raw)


def reward_settlements_from_transactions_payload(
    data: dict[str, Any],
) -> list[F2PoolRewardSettlement]:
    settlements: list[F2PoolRewardSettlement] = []
    for item in _transactions_from_payload(data):
        if not isinstance(item, dict):
            continue
        tx_type = _first_str(item, ("type", "transaction_type", "category"))
        if tx_type and "revenue" not in tx_type.lower():
            continue
        amount = _number(item.get("changed_balance"))
        if amount is None:
            amount = _number(item.get("amount"))
        if amount is None or amount <= 0:
            continue

        mining_extra = item.get("mining_extra")
        if not isinstance(mining_extra, dict):
            mining_extra = {}
        settlement_timestamp = (
            _timestamp_number(mining_extra.get("settle_date"))
            or _timestamp_number(item.get("created_at"))
        )
        if settlement_timestamp is None:
            continue
        settlement_time = _timestamp_to_local_minute(settlement_timestamp)
        mining_date = _date_from_timestamp(mining_extra.get("mining_date"))
        hashrate = _hashrate_to_th_s_value(
            _first_existing(
                mining_extra,
                (
                    "hash_rate",
                    "hashrate",
                    "avg_hashrate",
                    "average_hashrate",
                    "avg_hashrate_per_day",
                ),
            )
        )
        difficulty = _first_str(
            mining_extra,
            ("difficulty", "network_difficulty", "diff"),
        )
        description = (
            _first_str(mining_extra, ("comment", "description", "payment_type"))
            or _first_str(item, ("comment", "description"))
            or "With PPLNS"
        )
        created_at = _timestamp_to_iso(_timestamp_number(item.get("created_at")))
        settlements.append(
            F2PoolRewardSettlement(
                settlement_time=settlement_time,
                mining_date=mining_date,
                amount_btc=amount,
                avg_hashrate_th_s=hashrate,
                difficulty=difficulty,
                description=description,
                transaction_type=tx_type,
                created_at=created_at,
            )
        )

    return sorted(
        _dedupe_settlements(settlements),
        key=lambda row: row.settlement_time,
    )


def write_reward_settlement_csv(
    path: str | Path,
    settlements: list[F2PoolRewardSettlement],
) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    new_rows = sorted(settlements, key=lambda row: row.settlement_time)
    rows = new_rows
    if path.exists():
        existing_rows = _read_reward_settlement_csv(path)
        if new_rows:
            cutoff = _settlement_sort_key(new_rows[0].settlement_time)
            preserved = [
                row
                for row in existing_rows
                if _settlement_sort_key(row.settlement_time) < cutoff
            ]
            rows = sorted([*preserved, *new_rows], key=lambda row: row.settlement_time)
        else:
            rows = existing_rows
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "Settlement Time",
                "Avg. Hashrate Per Day",
                "Unit",
                "Amount",
                "Difficulty",
                "Description",
                "Mining Date",
                "Transaction Type",
            ],
        )
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    "Settlement Time": row.settlement_time,
                    "Avg. Hashrate Per Day": _format_optional_float(
                        row.avg_hashrate_th_s,
                        digits=2,
                    ),
                    "Unit": "TH/s" if row.avg_hashrate_th_s is not None else "",
                    "Amount": _format_btc(row.amount_btc),
                    "Difficulty": row.difficulty or "",
                    "Description": row.description or "",
                    "Mining Date": row.mining_date or "",
                    "Transaction Type": row.transaction_type or "",
                }
            )


def reward_settlement_summary(
    path: str | Path,
    settlements: list[F2PoolRewardSettlement],
    *,
    dry_run: bool,
) -> dict[str, Any]:
    latest = settlements[-1] if settlements else None
    return {
        "ok": True,
        "dry_run": dry_run,
        "source": "f2pool_v2_assets_transactions_list",
        "needed_permission": "assets/transactions/list",
        "csv_path": str(path),
        "rows": len(settlements),
        "newest_settlement_time": latest.settlement_time if latest else None,
        "last_five": [
            {
                "settlement_time": row.settlement_time,
                "amount_btc": row.amount_btc,
                "amount_sats": round(row.amount_btc * 100_000_000),
                "avg_hashrate_th_s": row.avg_hashrate_th_s,
                "difficulty": row.difficulty,
                "description": row.description,
            }
            for row in settlements[-5:]
        ],
        "token_printed": False,
        "raw_payload_printed": False,
    }


def _read_reward_settlement_csv(path: Path) -> list[F2PoolRewardSettlement]:
    rows: list[F2PoolRewardSettlement] = []
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            for row in reader:
                settlement_time = _csv_first_value(
                    row,
                    ("Settlement Time", "Time of Settlement", "settlement_time", "Time"),
                )
                amount = _number(
                    _csv_first_value(row, ("Amount", "Amount (Total)", "amount_btc"))
                )
                if not settlement_time or amount is None or amount <= 0:
                    continue
                avg_hashrate = _hashrate_to_th_s_value(
                    _csv_first_value(
                        row,
                        (
                            "Avg. Hashrate Per Day",
                            "Avg Hashrate Per Day",
                            "Average Hashrate",
                        ),
                    )
                )
                rows.append(
                    F2PoolRewardSettlement(
                        settlement_time=settlement_time,
                        mining_date=_csv_first_value(row, ("Mining Date", "mining_date")),
                        amount_btc=amount,
                        avg_hashrate_th_s=avg_hashrate,
                        difficulty=_csv_first_value(row, ("Difficulty", "difficulty")),
                        description=_csv_first_value(row, ("Description", "description")),
                        transaction_type=_csv_first_value(
                            row,
                            ("Transaction Type", "transaction_type"),
                        ),
                        created_at=None,
                    )
                )
    except OSError:
        return []
    return rows


def _csv_first_value(row: dict[str, Any], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        value = row.get(key)
        if value not in (None, ""):
            return str(value).strip()
    return None


def _settlement_sort_key(value: str) -> datetime:
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    parsed = _timestamp_number(value)
    if parsed is not None:
        return datetime.fromtimestamp(parsed, tz=timezone.utc).replace(tzinfo=None)
    return datetime.min


def _transactions_from_payload(data: dict[str, Any]) -> list[Any]:
    transactions = data.get("transactions")
    if isinstance(transactions, list):
        return transactions
    payload_data = data.get("data")
    if isinstance(payload_data, dict):
        transactions = payload_data.get("transactions")
        if isinstance(transactions, list):
            return transactions
    if isinstance(payload_data, list):
        return payload_data
    return []


def _raise_for_f2pool_v2_error(data: dict[str, Any]) -> None:
    code = data.get("code")
    status = str(data.get("status", "")).strip().lower()
    message = str(
        data.get("msg")
        or data.get("message")
        or data.get("reason")
        or ""
    ).strip()
    ok_status = status in {"", "ok", "success"}
    ok_code = code in (None, 0, "0", "ok", "success")
    if ok_status and ok_code:
        return

    normalized = message.lower()
    if any(word in normalized for word in ("permission", "forbidden", "unauthorized", "denied")):
        raise F2PoolRevenuePermissionError(
            "F2Pool token cannot read revenue transactions. "
            "Enable only ASSETS_MODULE assets/transactions/list for settled Rewards rows."
        )
    raise RuntimeError(f"F2Pool v2 API returned an error: {message or status or code}")


def _first_existing(values: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        if key in values and values[key] not in (None, ""):
            return values[key]
    return None


def _timestamp_number(value: Any) -> float | None:
    if isinstance(value, bool) or value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
    elif isinstance(value, str):
        text = value.strip()
        try:
            number = float(text)
        except ValueError:
            try:
                return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
            except ValueError:
                return None
    else:
        return None
    return number / 1000 if number > 10_000_000_000 else number


def _timestamp_to_local_minute(seconds: float) -> str:
    return datetime.fromtimestamp(seconds, tz=timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M")


def _timestamp_to_iso(seconds: float | None) -> str | None:
    if seconds is None:
        return None
    try:
        return datetime.fromtimestamp(seconds, tz=timezone.utc).astimezone().isoformat()
    except (OSError, OverflowError, ValueError):
        return None


def _date_from_timestamp(value: Any) -> str | None:
    seconds = _timestamp_number(value)
    if seconds is not None:
        return datetime.fromtimestamp(seconds, tz=timezone.utc).astimezone().date().isoformat()
    if value in (None, ""):
        return None
    return str(value)


def _hashrate_to_th_s_value(value: Any) -> float | None:
    number = _number(value)
    if number is None:
        return None
    unit = ""
    if isinstance(value, str):
        match = re.search(r"([kmgtpe])h(?:/s)?|([kmgtpe])(?:/s)?", value, re.IGNORECASE)
        unit = (match.group(1) or match.group(2)).lower() if match else ""
    if unit == "e":
        return number * 1_000_000
    if unit == "p":
        return number * 1_000
    if unit == "g":
        return number / 1_000
    if unit == "m":
        return number / 1_000_000
    if unit == "k":
        return number / 1_000_000_000
    if number > 1_000_000_000:
        return number / 1_000_000_000_000
    return number


def _dedupe_settlements(
    rows: list[F2PoolRewardSettlement],
) -> list[F2PoolRewardSettlement]:
    deduped: dict[tuple[str, float], F2PoolRewardSettlement] = {}
    for row in rows:
        deduped[(row.settlement_time, row.amount_btc)] = row
    return list(deduped.values())


def _format_optional_float(value: float | None, *, digits: int) -> str:
    if value is None:
        return ""
    return f"{value:.{digits}f}"


def _format_btc(value: float) -> str:
    return f"{value:.8f}"


def _data_origin_after_title(html: str, title: str) -> float | None:
    pattern = (
        r"<span[^>]*>\s*"
        + re.escape(title)
        + r"\s*</span>(?P<body>.{0,1200}?)data-origin=[\"'](?P<value>[^\"']+)"
    )
    match = re.search(pattern, html, re.IGNORECASE | re.DOTALL)
    if not match:
        return None
    return _number(match.group("value"))


def _difficulty_change_percent_after_title(html: str, title: str) -> float | None:
    body = _html_after_title(html, title)
    if not body:
        return None
    return _first_float_match(body, r"\(([+-]?\d+(?:\.\d+)?)%\)")


def _difficulty_eta_after_title(html: str, title: str) -> str | None:
    body = _html_after_title(html, title)
    if not body:
        return None
    text = _strip_tags(body)
    days = _first_float_match(text, r"([0-9]+(?:\.[0-9]+)?)\s*days?")
    hours = _first_float_match(text, r"([0-9]+(?:\.[0-9]+)?)\s*hours?")
    if days is None and hours is None:
        return None
    delta = timedelta(days=days or 0, hours=hours or 0)
    return (datetime.now().astimezone() + delta).isoformat()


def _html_after_title(html: str, title: str) -> str | None:
    pattern = r"<span[^>]*>\s*" + re.escape(title) + r"\s*</span>(?P<body>.{0,1200})"
    match = re.search(pattern, html, re.IGNORECASE | re.DOTALL)
    if not match:
        return None
    return match.group("body")


def _strip_tags(value: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", value)).strip()


def _latest_chart_hashrate(data: dict[str, Any]) -> float | None:
    points = data.get("data")
    if not isinstance(points, list):
        return None
    for point in reversed(points):
        if isinstance(point, dict):
            value = _number(point.get("hashrate"))
            if value is not None and value > 0:
                return value
    return None


def _first_float_match(text: str, pattern: str) -> float | None:
    match = re.search(pattern, text, re.IGNORECASE | re.DOTALL)
    if not match:
        return None
    return _number(match.group(1))


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        text = value.strip().replace(",", "")
        try:
            return float(text)
        except ValueError:
            match = re.search(r"[-+]?\d+(?:\.\d+)?(?:e[-+]?\d+)?", text, re.IGNORECASE)
            if match:
                try:
                    return float(match.group(0))
                except ValueError:
                    return None
    return None


def _first_number(values: dict[str, Any], keys: tuple[str, ...]) -> float | None:
    for key in keys:
        number = _number(values.get(key))
        if number is not None:
            return number
    return None


def _first_positive_number(values: dict[str, Any], keys: tuple[str, ...]) -> float | None:
    for key in keys:
        number = _number(values.get(key))
        if number is not None and number > 0:
            return number
    return None


def _first_str(values: dict[str, Any], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        value = values.get(key)
        if value not in (None, ""):
            return str(value)
    return None


def _coalesce(value: Any, fallback: Any) -> Any:
    return value if value is not None else fallback


def _get_json_object(client: httpx.Client, url: str) -> dict[str, Any]:
    response = client.get(url, headers={"User-Agent": "avalon-q-scheduler/1.0"})
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, dict):
        raise RuntimeError(f"Network context endpoint did not return a JSON object: {url}")
    return data


def _retarget_timestamp_to_iso(value: Any) -> str | None:
    number = _number(value)
    if number is None:
        return None
    seconds = number / 1000 if number > 10_000_000_000 else number
    try:
        return datetime.fromtimestamp(seconds, tz=timezone.utc).isoformat()
    except (OSError, OverflowError, ValueError):
        return None


def _copy_keys(values: dict[str, Any], keys: tuple[str, ...]) -> dict[str, Any]:
    return {key: values[key] for key in keys if key in values}


def _sanitized_network_context_raw(
    *,
    difficulty_adjustment: dict[str, Any],
    hashrate: dict[str, Any],
    pool: dict[str, Any],
    pool_slug: str,
) -> dict[str, Any]:
    pool_details = pool.get("pool") if isinstance(pool.get("pool"), dict) else {}
    pool_raw = _copy_keys(
        pool,
        ("estimatedHashrate", "reportedHashrate", "blockCount", "totalReward"),
    )
    pool_raw["name"] = pool_details.get("name") or pool.get("name")
    pool_raw["slug"] = pool_details.get("slug") or pool.get("slug") or pool_slug
    block_share = pool.get("blockShare")
    if isinstance(block_share, dict):
        pool_raw["blockShare"] = _copy_keys(block_share, ("24h", "1w", "all"))

    return {
        "provider": "mempool.space",
        "pool_slug": pool_slug,
        "difficulty_adjustment": _copy_keys(
            difficulty_adjustment,
            (
                "difficultyChange",
                "estimatedRetargetDate",
                "remainingBlocks",
                "remainingTime",
                "nextRetargetHeight",
                "progressPercent",
            ),
        ),
        "hashrate": _copy_keys(
            hashrate,
            ("currentHashrate", "currentDifficulty"),
        ),
        "pool": pool_raw,
    }


def _int(value: Any) -> int | None:
    number = _number(value)
    if number is None:
        return None
    return int(number)


def _worker_names(workers: Any) -> list[str]:
    if isinstance(workers, dict):
        return sorted(str(name) for name in workers)
    if isinstance(workers, list):
        names = []
        for worker in workers:
            if isinstance(worker, list) and worker:
                names.append(str(worker[0]))
            elif isinstance(worker, dict) and worker.get("name"):
                names.append(str(worker["name"]))
        return sorted(names)
    return []


def _sanitized_account_details(data: dict[str, Any]) -> dict[str, Any]:
    blocked = {
        "payout_history",
        "payout_history_fee",
        "user_payout",
    }
    return {
        key: value
        for key, value in data.items()
        if key not in blocked
    }
