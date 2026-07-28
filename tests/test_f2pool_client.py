import os

import pytest

from avalon_q_scheduler.config import F2PoolConfig
from avalon_q_scheduler.f2pool_client import (
    credentials_from_env,
    mining_user_from_env_or_config,
    network_context_from_mempool_payloads,
    network_context_from_f2pool_status_page,
    network_context_with_pool_hashrate_estimate,
    reward_settlements_from_transactions_payload,
    snapshot_from_account_details,
    snapshot_with_network_context,
    write_reward_settlement_csv,
)


def test_snapshot_from_account_details_maps_v1_fields():
    snapshot = snapshot_from_account_details(
        {
            "hashrate": 75_000_000_000_000,
            "hashes_last_hour": "270000000000000000",
            "hashes_last_day": 6.4e18,
            "value_today": 0.00001234,
            "balance": "0.001",
            "worker_length": 2,
            "worker_length_online": "1",
            "stale_hashes_rejected_last_hour": 12,
            "stale_hashes_rejected_last_day": 34,
            "pool_hashrate": 7.8e15,
            "global_hashrate": 6.2e20,
            "difficulty": 83_000_000_000_000,
            "next_difficulty": 84_200_000_000_000,
            "difficulty_change_percent": 1.45,
            "next_difficulty_time": "2026-06-30T12:00:00Z",
            "workers": [["avalonq", 75_000_000_000_000]],
            "user_payout": [["2026-01-13", "txid", 0.01, "wallet-address"]],
        },
        currency="bitcoin",
        mining_user="example-miner",
    )

    assert snapshot.currency == "bitcoin"
    assert snapshot.mining_user == "example-miner"
    assert snapshot.hashrate_h_s == 75_000_000_000_000
    assert snapshot.hashrate_th_s == 75
    assert snapshot.value_today == 0.00001234
    assert snapshot.worker_length_online == 1
    assert snapshot.worker_names == ["avalonq"]
    assert snapshot.pool_hashrate_h_s == 7.8e15
    assert snapshot.pool_hashrate_th_s == 7800
    assert snapshot.network_hashrate_h_s == 6.2e20
    assert snapshot.network_hashrate_th_s == 620_000_000
    assert snapshot.current_difficulty == 83_000_000_000_000
    assert snapshot.next_difficulty == 84_200_000_000_000
    assert snapshot.difficulty_change_percent == 1.45
    assert snapshot.difficulty_adjustment_at == "2026-06-30T12:00:00Z"
    assert "user_payout" not in snapshot.raw
    assert "raw" not in snapshot.to_summary_dict()
    assert snapshot.to_summary_dict(include_raw=True)["raw"]["pool_hashrate"] == 7.8e15


def test_credentials_from_env_requires_token(monkeypatch):
    monkeypatch.delenv("F2POOL_API_TOKEN", raising=False)

    with pytest.raises(ValueError, match="F2POOL_API_TOKEN"):
        credentials_from_env(F2PoolConfig())


def test_credentials_and_mining_user_can_come_from_env(monkeypatch):
    monkeypatch.setenv("F2POOL_API_TOKEN", "secret")
    monkeypatch.setenv("F2POOL_MINING_USER", "example-miner")

    assert credentials_from_env(F2PoolConfig()).api_token == "secret"
    assert mining_user_from_env_or_config(F2PoolConfig()) == "example-miner"


def test_config_mining_user_is_fallback(monkeypatch):
    monkeypatch.delenv("F2POOL_MINING_USER", raising=False)

    assert mining_user_from_env_or_config(F2PoolConfig(mining_user="configured")) == "configured"


def test_network_context_from_mempool_payloads_maps_public_fields():
    context = network_context_from_mempool_payloads(
        captured_at="2026-07-03T20:00:00+00:00",
        pool_slug="f2pool",
        difficulty_adjustment={
            "difficultyChange": -1.5,
            "estimatedRetargetDate": 1783753925000,
            "remainingBlocks": 100,
            "nextRetargetHeight": 907200,
        },
        hashrate={
            "currentHashrate": 942_000_000_000_000_000_000,
            "currentDifficulty": 126_000_000_000_000,
        },
        pool={
            "estimatedHashrate": 120_000_000_000_000_000_000,
            "reportedHashrate": None,
            "blockShare": {"24h": 0.12, "1w": 0.134, "all": 0.10},
            "pool": {
                "name": "F2Pool",
                "slug": "f2pool",
                "addresses": ["not persisted"],
                "regexes": ["not persisted"],
            },
        },
    )

    assert context.provider == "mempool.space"
    assert context.pool_hashrate_h_s == 120_000_000_000_000_000_000
    assert context.network_hashrate_h_s == 942_000_000_000_000_000_000
    assert context.current_difficulty == 126_000_000_000_000
    assert context.next_difficulty == pytest.approx(124_110_000_000_000)
    assert context.difficulty_change_percent == -1.5
    assert context.difficulty_adjustment_at == "2026-07-11T07:12:05+00:00"
    assert context.raw["pool"]["name"] == "F2Pool"
    assert "addresses" not in context.raw["pool"]
    assert "regexes" not in context.raw["pool"]


def test_snapshot_with_network_context_persists_dashboard_keys():
    snapshot = snapshot_from_account_details(
        {
            "hashrate": 75_000_000_000_000,
            "value_today": 0.00001234,
            "workers": [],
        },
        currency="bitcoin",
        mining_user="example-miner",
    )
    context = network_context_from_mempool_payloads(
        captured_at="2026-07-03T20:00:00+00:00",
        pool_slug="f2pool",
        difficulty_adjustment={
            "difficultyChange": 2.0,
            "estimatedRetargetDate": 1783753925000,
        },
        hashrate={
            "currentHashrate": 900_000_000_000_000_000_000,
            "currentDifficulty": 100_000_000_000_000,
        },
        pool={"estimatedHashrate": 100_000_000_000_000_000_000},
    )

    enriched = snapshot_with_network_context(snapshot, context)
    data = enriched.to_json_dict()

    assert data["pool_hashrate_h_s"] == 100_000_000_000_000_000_000
    assert data["pool_hashrate_th_s"] == 100_000_000
    assert data["network_hashrate_h_s"] == 900_000_000_000_000_000_000
    assert data["network_hashrate_th_s"] == 900_000_000
    assert data["current_difficulty"] == 100_000_000_000_000
    assert data["next_difficulty"] == 102_000_000_000_000
    assert data["difficulty_change_percent"] == 2.0
    assert data["difficulty_adjustment_at"] == "2026-07-11T07:12:05+00:00"
    assert data["raw"]["network_context"]["provider"] == "mempool.space"


def test_mempool_pool_hashrate_supplements_f2pool_network_status_only():
    snapshot = snapshot_from_account_details(
        {
            "hashrate": 75_000_000_000_000,
            "value_today": 0.00001234,
            "workers": [],
        },
        currency="bitcoin",
        mining_user="example-miner",
    )
    f2pool_context = network_context_from_f2pool_status_page(
        """
        <span class="title">Difficulty</span>
        <span class="format-num" data-rule="formatHashrate" data-origin="133869853540305.39"></span>
        <span class="title">Next Difficulty Change Estimate</span>
        <span class="format-num" data-rule="formatHashrate" data-origin="131840000000000"></span>
        <span style="color: #ff7443;">(-1.51%)</span>
        """,
        chart_payload={"data": [{"hashrate": 9.4395e20}]},
        captured_at="2026-07-03T20:00:00+00:00",
    )
    mempool_context = network_context_from_mempool_payloads(
        captured_at="2026-07-03T20:00:00+00:00",
        pool_slug="f2pool",
        difficulty_adjustment={},
        hashrate={},
        pool={
            "estimatedHashrate": 140_081_717_740_902_290_000,
            "blockShare": {"24h": 0.138, "1w": 0.137},
            "pool": {"name": "F2Pool", "slug": "f2pool"},
        },
    )

    context = network_context_with_pool_hashrate_estimate(f2pool_context, mempool_context)
    enriched = snapshot_with_network_context(snapshot, context)
    data = enriched.to_json_dict()

    assert data["network_hashrate_h_s"] == 9.4395e20
    assert data["current_difficulty"] == 133_869_853_540_305.39
    assert data["pool_hashrate_h_s"] == pytest.approx(140_081_717_740_902_290_000)
    assert data["pool_hashrate_th_s"] == pytest.approx(140_081_717.7409023)
    assert data["raw"]["network_context_source"] == "f2pool_network_status"
    assert data["raw"]["pool_hashrate_source"] == "mempool_space"
    assert data["raw"]["pool_hashrate_label"] == "mempool.space public estimate"
    assert data["raw"]["mempool_pool_block_share_24h"] == 0.138
    assert "f2pool_network_status_pool_hashrate_h_s" not in data["raw"]


def test_reward_settlements_from_transactions_payload_maps_f2pool_v2_rows(tmp_path):
    payload = {
        "code": 0,
        "transactions": [
            {
                "type": "revenue",
                "changed_balance": "0.00002362",
                "created_at": 1783036800,
                "mining_extra": {
                    "settle_date": 1783036800,
                    "mining_date": 1782950400,
                    "hash_rate": 67.66,
                    "difficulty": "133.87T",
                    "comment": "With PPLNS",
                },
            },
            {
                "type": "payout",
                "changed_balance": "0.01",
                "created_at": 1783036800,
            },
        ],
    }

    rows = reward_settlements_from_transactions_payload(payload)

    assert len(rows) == 1
    assert rows[0].amount_btc == 0.00002362
    assert rows[0].avg_hashrate_th_s == 67.66
    assert rows[0].difficulty == "133.87T"
    assert rows[0].description == "With PPLNS"

    csv_path = tmp_path / "f2pool_rewards.csv"
    csv_path.write_text(
        "\n".join(
            [
                "Time of Settlement,Avg. Hashrate Per Day,Unit,Amount (Total),Description",
                "2026-06-21 07:00,55.00,TH/s,0.00002400,With PPLNS",
                "2026-07-02 19:00,1.00,TH/s,0.00000001,bad estimate",
            ]
        ),
        encoding="utf-8",
    )
    write_reward_settlement_csv(csv_path, rows)
    text = csv_path.read_text(encoding="utf-8")
    assert "Settlement Time,Avg. Hashrate Per Day,Unit,Amount,Difficulty,Description" in text
    assert "2026-06-21 07:00,55.00,TH/s,0.00002400" in text
    assert "67.66,TH/s,0.00002362,133.87T,With PPLNS" in text
    assert "bad estimate" not in text


def test_network_context_from_f2pool_status_page_maps_public_fields():
    html = """
    <span class="title">Difficulty</span>
    <span class="format-num" data-rule="formatHashrate" data-origin="133869853540305.39"></span>
    <span class="title">Next Difficulty Change Estimate</span>
    <span class="format-num" data-rule="formatHashrate" data-origin="131840000000000"></span>
    <span style="color: #ff7443;">(-1.51%)</span>
    <span class="title d-inline-block">Time Until Difficulty Change</span>
    <span class="item d-inline-block">7 days and 1 hours</span>
    """

    context = network_context_from_f2pool_status_page(
        html,
        chart_payload={"data": [{"hashrate": 9.4395e20}]},
        captured_at="2026-07-03T20:00:00+00:00",
    )

    assert context.provider == "f2pool_network_status"
    assert context.network_hashrate_h_s == 9.4395e20
    assert context.current_difficulty == 133_869_853_540_305.39
    assert context.next_difficulty == 131_840_000_000_000
    assert context.difficulty_change_percent == -1.51
    assert context.difficulty_adjustment_at is not None
    assert context.raw["network_context_source"] == "f2pool_network_status"
