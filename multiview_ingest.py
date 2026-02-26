"""
Live multiview ingestion: USB (or file) capture → layout → per-input segments.

Use this to get a list of (input_id, crop) per frame so downstream code
can analyze each camera as if it had its own capture device.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import cv2
import numpy as np

try:
    from .multiview_layout import (
        load_profile,
        detect_layout_auto,
        segment_frame,
    )
except ImportError:
    from multiview_layout import (
        load_profile,
        detect_layout_auto,
        segment_frame,
    )


class MultiviewIngest:
    """
    Ingest multiview from a USB capture device or file; resolve layout (profile or auto);
    segment each frame into per-input crops.
    """

    def __init__(
        self,
        source: int | str = 0,
        profile_path: Optional[str | Path] = None,
        inset_px: int = 0,
        inset_ratio: float = 0.02,
        auto_save_profile: Optional[str | Path] = None,
    ):
        """
        Args:
            source: cv2.VideoCapture source (device index or file path).
            profile_path: Optional path to JSON profile (revamp-style). If set and file exists, layout is loaded from it.
            inset_px: Pixel inset per cell to reduce borders/labels.
            inset_ratio: Fraction of cell size to inset (used if inset_px is 0 and ratio > 0).
            auto_save_profile: If set, save auto-detected layout to this path for next run.
        """
        self.source = source
        self.profile_path = Path(profile_path) if profile_path else None
        self.inset_px = inset_px
        self.inset_ratio = inset_ratio
        self.auto_save_profile = Path(auto_save_profile) if auto_save_profile else None

        self._cap: Optional[cv2.VideoCapture] = None
        self._layout: Optional[Dict[int, Tuple[int, int, int, int]]] = None
        self._frame_shape: Optional[Tuple[int, int]] = None
        self._lock = threading.Lock()

    def _ensure_open(self) -> bool:
        if self._cap is not None and self._cap.isOpened():
            return True
        self._cap = cv2.VideoCapture(self.source)
        if not self._cap.isOpened():
            return False
        return True

    def _resolve_layout(self, frame: np.ndarray) -> Optional[Dict[int, Tuple[int, int, int, int]]]:
        """Get layout from profile or auto-detect; cache in self._layout."""
        with self._lock:
            if self._layout is not None:
                h, w = frame.shape[:2]
                if self._frame_shape == (h, w):
                    return self._layout
                self._layout = None
                self._frame_shape = None

        layout = None
        if self.profile_path and self.profile_path.exists():
            layout = load_profile(self.profile_path)
        if not layout:
            layout = detect_layout_auto(frame)
            if layout and self.auto_save_profile:
                self._save_profile(frame.shape[1], frame.shape[0], layout)

        with self._lock:
            self._layout = layout
            if frame is not None:
                self._frame_shape = (frame.shape[0], frame.shape[1])
        return layout

    def _save_profile(self, width: int, height: int, layout: Dict[int, Tuple[int, int, int, int]]) -> None:
        try:
            self.auto_save_profile.parent.mkdir(parents=True, exist_ok=True)
            data = {
                "width": width,
                "height": height,
                "inputs": {str(k): list(v) for k, v in sorted(layout.items())},
            }
            with open(self.auto_save_profile, "w", encoding="utf-8") as f:
                import json
                json.dump(data, f, indent=2)
        except Exception:
            pass

    def read_frame(self) -> Optional[np.ndarray]:
        """Read one frame from capture. Returns BGR frame or None."""
        if not self._ensure_open():
            return None
        ok, frame = self._cap.read()
        if not ok or frame is None:
            return None
        return frame

    def get_segments(self, frame: Optional[np.ndarray] = None) -> List[Tuple[int, np.ndarray]]:
        """
        Get per-input segments for the current layout.
        If frame is None, reads one frame from capture. Returns list of (input_id, crop).
        """
        if frame is None:
            frame = self.read_frame()
        if frame is None:
            return []
        layout = self._resolve_layout(frame)
        if not layout:
            return []
        return segment_frame(
            frame,
            layout,
            inset_px=self.inset_px,
            inset_ratio=self.inset_ratio,
        )

    def get_layout(self, frame: Optional[np.ndarray] = None) -> Optional[Dict[int, Tuple[int, int, int, int]]]:
        """Resolve and return current layout (input_id -> (x,y,w,h)) without segmenting."""
        if frame is None:
            frame = self.read_frame()
        if frame is None:
            return None
        return self._resolve_layout(frame)

    def release(self) -> None:
        if self._cap is not None:
            try:
                self._cap.release()
            except Exception:
                pass
            self._cap = None
        with self._lock:
            self._layout = None
            self._frame_shape = None

    def __enter__(self) -> "MultiviewIngest":
        return self

    def __exit__(self, *args) -> None:
        self.release()


def segments_from_capture(
    source: int | str = 0,
    profile_path: Optional[str | Path] = None,
    inset_ratio: float = 0.02,
) -> List[Tuple[int, np.ndarray]]:
    """
    One-shot: open source, read one frame, resolve layout, return segments.
    Useful for testing or single-frame processing.
    """
    with MultiviewIngest(source=source, profile_path=profile_path, inset_ratio=inset_ratio) as ingest:
        return ingest.get_segments()
