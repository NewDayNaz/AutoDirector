"""
Roamer stability: motion analysis on one segment (roamer crop).

Computes per-frame motion (frame-to-frame mean absolute difference or Laplacian variance),
maintains a short history (stability_window_seconds). is_stable() is True only when
motion has been below threshold for the full window.
"""

from __future__ import annotations

import logging
from collections import deque
from typing import Deque, Optional

import cv2
import numpy as np

logger = logging.getLogger(__name__)


class RoamerStability:
    """
    Feed roamer segment frames via push(frame); call is_stable() to know if the
    shot has been stable for the configured window.
    """

    def __init__(
        self,
        stability_window_seconds: float = 1.0,
        motion_threshold: float = 10.0,
        fps: float = 10.0,
        use_laplacian: bool = True,
    ):
        self.stability_window_seconds = stability_window_seconds
        self.motion_threshold = motion_threshold
        self.fps = max(1.0, fps)
        self.use_laplacian = use_laplacian
        self._max_samples = max(1, int(stability_window_seconds * self.fps))
        self._motion_history: Deque[float] = deque(maxlen=self._max_samples)
        self._prev_frame: Optional[np.ndarray] = None

    def push(self, frame: np.ndarray) -> float:
        """
        Add one frame (roamer crop, BGR or grayscale). Returns current motion value for this frame.
        """
        if frame is None or frame.size == 0:
            self._motion_history.append(self.motion_threshold + 1.0)
            return self.motion_threshold + 1.0
        gray = frame if len(frame.shape) == 2 else cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        motion = self._compute_motion(gray)
        self._motion_history.append(motion)
        self._prev_frame = gray
        return motion

    def _compute_motion(self, gray: np.ndarray) -> float:
        if self.use_laplacian:
            lap = cv2.Laplacian(gray, cv2.CV_64F)
            return float(np.var(lap))
        if self._prev_frame is None:
            return 0.0
        if self._prev_frame.shape != gray.shape:
            return 0.0
        diff = cv2.absdiff(self._prev_frame, gray)
        return float(np.mean(diff))

    def is_stable(self) -> bool:
        """
        True only when motion has been below motion_threshold for the entire
        stability window (enough samples and all below threshold).
        """
        if len(self._motion_history) < self._max_samples:
            return False
        return all(m <= self.motion_threshold for m in self._motion_history)

    def reset(self) -> None:
        """Clear history (e.g. after input change)."""
        self._motion_history.clear()
        self._prev_frame = None
