from avalon_q_scheduler.state_store import SchedulerState, StateStore


def test_state_store_returns_default_state_when_missing(tmp_path):
    store = StateStore(tmp_path / "state.json")

    state = store.load()

    assert state.current_known_mode is None
    assert state.automation_paused is False


def test_state_store_round_trips_state(tmp_path):
    store = StateStore(tmp_path / "state.json")
    state = SchedulerState(
        current_known_mode="eco",
        previous_mode="off",
        last_commanded_mode="eco",
        last_reason="cheap power window ended",
        softoff_active=False,
    )

    store.save(state)
    loaded = store.load()

    assert loaded.current_known_mode == "eco"
    assert loaded.previous_mode == "off"
    assert loaded.last_commanded_mode == "eco"
    assert loaded.last_reason == "cheap power window ended"
    assert loaded.softoff_active is False
