import numpy as np

from safety import BackupTimer, is_black_or_frozen


def test_is_black_detects_dark_frame():
    frame = np.zeros((10, 10, 3), dtype=np.uint8)
    bad, reason = is_black_or_frozen(frame)
    assert bad is True
    assert reason == "black"


def test_backup_timer_triggers_after_timeout(monkeypatch):
    timer = BackupTimer(backup_timeout_seconds=1.0)

    class _T:
        now = 0.0

    def fake_monotonic():
        return _T.now

    import time as _time

    monkeypatch.setattr(_time, "monotonic", fake_monotonic)
    _T.now = 0.0
    timer.reset()
    _T.now = 0.5
    assert timer.should_trigger_backup() is False
    _T.now = 2.0
    assert timer.should_trigger_backup() is True

