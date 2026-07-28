from avalon_q_scheduler.config import F2PoolConfig
from avalon_q_scheduler.f2pool_client import F2PoolAccountSnapshot
from avalon_q_scheduler.f2pool_profitability import (
    build_profitability_record,
    import_settlement_csv_baselines,
    load_profitability_records,
)
from avalon_q_scheduler.state_store import SchedulerState
from avalon_q_scheduler.f2pool_profitability import miner_mode_from_state


def snapshot(
    *,
    captured_at,
    value_today,
    hashrate_th_s=50,
    raw=None,
    pool_hashrate_h_s=None,
    network_hashrate_h_s=None,
    current_difficulty=None,
    next_difficulty=None,
    difficulty_change_percent=None,
    difficulty_adjustment_at=None,
):
    return F2PoolAccountSnapshot(
        captured_at=captured_at,
        currency="bitcoin",
        mining_user="example-miner",
        hashrate_h_s=hashrate_th_s * 1_000_000_000_000,
        hashes_last_hour=None,
        hashes_last_day=None,
        value_today=value_today,
        balance=None,
        worker_length=1,
        worker_length_online=1,
        stale_hashes_rejected_last_hour=0,
        stale_hashes_rejected_last_day=0,
        worker_names=["001"],
        raw=raw or {},
        pool_hashrate_h_s=pool_hashrate_h_s,
        network_hashrate_h_s=network_hashrate_h_s,
        current_difficulty=current_difficulty,
        next_difficulty=next_difficulty,
        difficulty_change_percent=difficulty_change_percent,
        difficulty_adjustment_at=difficulty_adjustment_at,
    )


def baseline_row(*, captured_at, mode, btc_per_th_hour):
    return {
        "captured_at": captured_at,
        "miner_mode": mode,
        "snapshot": {
            "captured_at": captured_at,
            "currency": "bitcoin",
            "mining_user": "example-miner",
            "value_today": 0.0,
        },
        "rate": {
            "btc_per_th_hour": btc_per_th_hour,
            "sample_usable_for_baseline": True,
        },
    }


def previous_row(*, captured_at, value_today):
    return {
        "captured_at": captured_at,
        "currency": "bitcoin",
        "mining_user": "example-miner",
        "value_today": value_today,
    }


def test_profitability_record_calculates_value_today_derivative():
    record = build_profitability_record(
        snapshot=snapshot(
            captured_at="2026-06-29T10:10:00+00:00",
            value_today=0.000020,
            hashrate_th_s=50,
            raw={"pool_hashrate": 7.8e15},
            pool_hashrate_h_s=7.8e15,
        ),
        history=[
            previous_row(
                captured_at="2026-06-29T10:00:00+00:00",
                value_today=0.000010,
            )
        ],
        config=F2PoolConfig(baseline_min_samples_per_mode=2),
        miner_mode="eco",
        mode_source="test",
        last_commanded_at=None,
    )

    assert round(record.rate["elapsed_minutes"], 3) == 10
    assert round(record.rate["delta_value_today_btc"], 8) == 0.000010
    assert round(record.rate["btc_per_hour"], 8) == 0.000060
    assert round(record.rate["btc_per_th_hour"], 10) == 0.0000012
    assert record.rate["sample_usable_for_baseline"] is True
    assert record.snapshot["pool_hashrate_th_s"] == 7800
    assert record.snapshot["raw"]["pool_hashrate"] == 7.8e15
    assert record.shadow_decision["signal"] == "collecting_baseline"


def test_profitability_record_keeps_network_context_fields_in_snapshot():
    record = build_profitability_record(
        snapshot=snapshot(
            captured_at="2026-06-29T10:10:00+00:00",
            value_today=0.000020,
            hashrate_th_s=50,
            raw={"network_context": {"provider": "mempool.space"}},
            pool_hashrate_h_s=1.2e20,
            network_hashrate_h_s=9.4e20,
            current_difficulty=126_000_000_000_000,
            next_difficulty=124_110_000_000_000,
            difficulty_change_percent=-1.5,
            difficulty_adjustment_at="2026-07-11T03:12:05+00:00",
        ),
        history=[
            previous_row(
                captured_at="2026-06-29T10:00:00+00:00",
                value_today=0.000010,
            )
        ],
        config=F2PoolConfig(baseline_min_samples_per_mode=2),
        miner_mode="eco",
        mode_source="test",
        last_commanded_at=None,
    )

    assert record.snapshot["pool_hashrate_h_s"] == 1.2e20
    assert record.snapshot["pool_hashrate_th_s"] == 120_000_000
    assert record.snapshot["network_hashrate_h_s"] == 9.4e20
    assert record.snapshot["network_hashrate_th_s"] == 940_000_000
    assert record.snapshot["current_difficulty"] == 126_000_000_000_000
    assert record.snapshot["next_difficulty"] == 124_110_000_000_000
    assert record.snapshot["difficulty_change_percent"] == -1.5
    assert record.snapshot["difficulty_adjustment_at"] == "2026-07-11T03:12:05+00:00"
    assert record.snapshot["raw"]["network_context"]["provider"] == "mempool.space"


def test_profitability_shadow_signal_would_enter_super_above_eco_baseline():
    config = F2PoolConfig(
        baseline_min_samples_per_mode=2,
        shadow_enter_super_ratio=2.0,
    )
    record = build_profitability_record(
        snapshot=snapshot(
            captured_at="2026-06-29T10:00:00+00:00",
            value_today=4.0,
            hashrate_th_s=1,
        ),
        history=[
            baseline_row(
                captured_at="2026-06-29T08:00:00+00:00",
                mode="eco",
                btc_per_th_hour=1.0,
            ),
            baseline_row(
                captured_at="2026-06-29T08:10:00+00:00",
                mode="eco",
                btc_per_th_hour=1.2,
            ),
            previous_row(
                captured_at="2026-06-29T09:00:00+00:00",
                value_today=1.0,
            ),
        ],
        config=config,
        miner_mode="eco",
        mode_source="test",
        last_commanded_at=None,
    )

    assert record.baseline["sample_count"] == 2
    assert record.baseline["btc_per_th_hour_median"] == 1.1
    assert record.shadow_decision["signal"] == "would_enter_super"
    assert record.shadow_decision["profitability_ratio"] > 2


def test_super_shadow_signal_would_return_to_eco_below_exit_ratio():
    record = build_profitability_record(
        snapshot=snapshot(
            captured_at="2026-06-29T10:00:00+00:00",
            value_today=2.0,
            hashrate_th_s=1,
        ),
        history=[
            baseline_row(
                captured_at="2026-06-29T08:00:00+00:00",
                mode="super",
                btc_per_th_hour=1.0,
            ),
            previous_row(
                captured_at="2026-06-29T09:00:00+00:00",
                value_today=1.0,
            ),
        ],
        config=F2PoolConfig(baseline_min_samples_per_mode=1, shadow_exit_super_ratio=1.3),
        miner_mode="super",
        mode_source="test",
        last_commanded_at=None,
    )

    assert record.shadow_decision["signal"] == "would_return_to_eco"


def test_rate_sample_crossing_mode_change_is_not_added_to_baseline():
    record = build_profitability_record(
        snapshot=snapshot(
            captured_at="2026-06-29T10:10:00+00:00",
            value_today=0.000020,
            hashrate_th_s=50,
        ),
        history=[
            previous_row(
                captured_at="2026-06-29T10:00:00+00:00",
                value_today=0.000010,
            )
        ],
        config=F2PoolConfig(),
        miner_mode="super",
        mode_source="test",
        last_commanded_at="2026-06-29T10:05:00+00:00",
    )

    assert record.rate["sample_usable_for_baseline"] is False
    assert record.rate["sample_rejection_reason"] == "sample crosses a mode change"


def test_miner_mode_from_state_prefers_softoff_then_current_mode():
    assert miner_mode_from_state(SchedulerState(softoff_active=True))[0] == "off"
    assert miner_mode_from_state(SchedulerState(current_known_mode="eco"))[0] == "eco"


def test_import_settlement_csv_seeds_conservative_mode_baselines(tmp_path):
    csv_path = tmp_path / "settlements.csv"
    csv_path.write_text(
        "\n".join(
            [
                "Time of Settlement,Avg. Hashrate Per Day,Unit,Amount (PPS),Amount (Bonus),Amount (PPLNS),Amount (Total),Description",
                "2026-06-21 07:00,55.00,TH/s,0.000024,0,0,0.000024,With PPLNS",
                "2026-06-20 07:00,94.00,TH/s,0.000041,0,0,0.000041,With PPLNS",
                "2026-06-19 07:00,75.00,TH/s,0.000030,0,0,0.000030,With PPLNS",
                "2026-06-18 07:00,3.00,TH/s,0.000001,0,0,0.000001,With PPLNS",
            ]
        ),
        encoding="utf-8",
    )
    output_path = tmp_path / "profitability.jsonl"

    summary = import_settlement_csv_baselines(
        csv_path=csv_path,
        output_path=output_path,
        config=F2PoolConfig(),
        currency="bitcoin",
        mining_user="example-miner",
    )

    assert summary["rows_read"] == 4
    assert summary["rows_imported"] == 2
    assert summary["eco_rows_imported"] == 1
    assert summary["super_rows_imported"] == 1
    assert summary["rows_skipped_mixed_or_partial"] == 2
    assert summary["imported_baselines"]["eco"]["sats_per_day_median"] == 2400
    assert summary["imported_baselines"]["super"]["sats_per_day_median"] == 4100

    records = load_profitability_records(output_path)
    assert [record["miner_mode"] for record in records] == ["eco", "super"]
    assert records[0]["source"]["kind"] == "f2pool_settlement_csv"
    assert records[0]["rate"]["sample_usable_for_baseline"] is True


def test_import_settlement_csv_skips_duplicate_seed_rows(tmp_path):
    csv_path = tmp_path / "settlements.csv"
    csv_path.write_text(
        "\n".join(
            [
                "Time of Settlement,Avg. Hashrate Per Day,Unit,Amount (PPS),Amount (Bonus),Amount (PPLNS),Amount (Total),Description",
                "2026-06-21 07:00,55.00,TH/s,0.000024,0,0,0.000024,With PPLNS",
            ]
        ),
        encoding="utf-8",
    )
    output_path = tmp_path / "profitability.jsonl"

    first = import_settlement_csv_baselines(
        csv_path=csv_path,
        output_path=output_path,
        config=F2PoolConfig(),
        currency="bitcoin",
        mining_user="example-miner",
    )
    second = import_settlement_csv_baselines(
        csv_path=csv_path,
        output_path=output_path,
        config=F2PoolConfig(),
        currency="bitcoin",
        mining_user="example-miner",
    )

    assert first["rows_imported"] == 1
    assert second["rows_imported"] == 0
    assert second["rows_skipped_duplicate"] == 1
    assert len(output_path.read_text(encoding="utf-8").splitlines()) == 1
