from src import state


def test_no_unsynced_changes_initially(tmp_path):
    state.configure(tmp_path / "state")
    assert state.has_unsynced_changes() is False


def test_change_marks_unsynced(tmp_path):
    state.configure(tmp_path / "state")
    state.mark_change()
    assert state.has_unsynced_changes() is True


def test_cloud_sync_clears_unsynced(tmp_path):
    state.configure(tmp_path / "state")
    state.mark_change()
    assert state.has_unsynced_changes() is True
    state.mark_cloud_sync()
    assert state.has_unsynced_changes() is False


def test_change_after_sync_is_unsynced_again(tmp_path):
    state.configure(tmp_path / "state")
    state.mark_change()
    state.mark_cloud_sync()
    assert state.has_unsynced_changes() is False
    state.mark_change()
    assert state.has_unsynced_changes() is True


def test_change_state_persists_across_reconfigure(tmp_path):
    state_dir = tmp_path / "state"
    state.configure(state_dir)
    state.mark_change()
    # Simulate a daemon restart re-loading the same state dir.
    state.configure(state_dir)
    assert state.has_unsynced_changes() is True
