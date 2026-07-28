from avalon_q_scheduler.ac_baseline import summarize_live_readings


def test_summarize_live_readings_counts_short_cooling_intervals():
    readings = [
        {
            "captured_at": "2026-06-29T10:00:00-05:00",
            "hvac_status": "COOLING",
            "ambient_temp_c": 24.1,
            "ambient_humidity_pct": 55,
        },
        {
            "captured_at": "2026-06-29T10:01:00-05:00",
            "hvac_status": "COOLING",
            "ambient_temp_c": 24.2,
            "ambient_humidity_pct": 55,
        },
        {
            "captured_at": "2026-06-29T10:02:00-05:00",
            "hvac_status": "OFF",
            "ambient_temp_c": 24.0,
            "ambient_humidity_pct": 54,
        },
        {
            "captured_at": "2026-06-29T10:03:00-05:00",
            "hvac_status": "OFF",
            "ambient_temp_c": 24.0,
            "ambient_humidity_pct": 54,
        },
    ]

    summary = summarize_live_readings(readings)

    assert summary["readings_count"] == 4
    assert summary["cooling_runtime_minutes_by_day"] == {"2026-06-29": 2.0}
    assert summary["cooling_runtime_minutes_by_month"] == {"2026-06": 2.0}
    assert summary["latest_hvac_status"] == "OFF"
