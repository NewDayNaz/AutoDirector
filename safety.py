"""
Safety and fallbacks: black/freeze detection, backup timeout, bad-program recovery.

Used by director core to exclude bad candidates and to trigger immediate cut-away
when program feed is bad, or fade to backup when no good candidate for N seconds.
"""

from __future__ import annotations

import logging
import time
from typing import Optional, Tuple

import numpy as np

logger = logging.getLogger(__name__)


def is_black_or_frozen(
    frame: np.ndarray,
    *,
    black_mean_threshold: float = 5.0,
    black_ratio_threshold: float = 0.98,
    freeze_variance_threshold: float = 1.0,
    prev_frame: Optional[np.ndarray] = None,
) -> Tuple[bool, str]:
    """
    Return (is_bad, reason). True if frame appears black or frozen.
    - Black: mean level below black_mean_threshold or ratio of dark pixels > black_ratio_threshold.
    - Frozen: if prev_frame given, variance of frame difference below freeze_variance_threshold.
    """
    if frame is None or frame.size == 0:
        return True, "empty"
    mean = float(np.mean(frame))
    if mean < black_mean_threshold:
        return True, "black"
    if frame.ndim == 3:
        gray = np.mean(frame, axis=2)
    else:
        gray = frame
    dark_ratio = np.sum(gray < 20) / gray.size
    if dark_ratio >= black_ratio_threshold:
        return True, "black"
    if prev_frame is not None and prev_frame.shape == gray.shape:
        diff = np.abs(gray.astype(float) - prev_frame.astype(float))
        if np.var(diff) < freeze_variance_threshold:
            return True, "frozen"
    return False, ""


class BackupTimer:
    """
    Tracks time since last "good" candidate. When no good candidate for
    backup_timeout_seconds, director should fade to backup input.
    """

    def __init__(self, backup_timeout_seconds: float = 10.0):
        self.backup_timeout_seconds = backup_timeout_seconds
        self._last_good_time: Optional[float] = None
        self._reset_time = time.monotonic()

    def mark_good(self) -> None:
        self._last_good_time = time.monotonic()

    def mark_no_candidate(self) -> None:
        pass  # Only good resets the clock

    def should_trigger_backup(self) -> bool:
        """True if we have had no good candidate for backup_timeout_seconds."""
        if self.backup_timeout_seconds <= 0:
            return False
        return self.seconds_since_good() >= self.backup_timeout_seconds

    def seconds_since_good(self) -> float:
        """Seconds since last mark_good(); 0 if never marked or timeout disabled."""
        if self.backup_timeout_seconds <= 0:
            return 0.0
        now = time.monotonic()
        if self._last_good_time is None:
            return now - self._reset_time
        return now - self._last_good_time

    def reset(self) -> None:
        self._last_good_time = time.monotonic()
        self._reset_time = time.monotonic()
