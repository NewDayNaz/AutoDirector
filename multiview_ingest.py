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
        width: Optional[int] = None,
        height: Optional[int] = None,
        inset_px: int = 0,
        inset_ratio: float = 0.02,
        auto_save_profile: Optional[str | Path] = None,
        debug_frame_path: Optional[str | Path] = None,
        debug_multiview_sections_path: Optional[str | Path] = None,
        section_to_input_id: Optional[Dict[int, int]] = None,
    ):
        """
        Args:
            source: cv2.VideoCapture source (device index or file path).
            profile_path: Optional path to JSON profile (revamp-style). If set and file exists, layout is loaded from it.
            width/height: Optional requested capture resolution. When set, we
                call CAP_PROP_FRAME_WIDTH/HEIGHT on the capture device. Drivers
                may clamp or ignore these values.
            inset_px: Pixel inset per cell to reduce borders/labels.
            inset_ratio: Fraction of cell size to inset (used if inset_px is 0 and ratio > 0).
            auto_save_profile: If set, save auto-detected layout to this path for next run.
        """
        self.source = source
        self.profile_path = Path(profile_path) if profile_path else None
        self.width: Optional[int] = width
        self.height: Optional[int] = height
        self.inset_px = inset_px
        self.inset_ratio = inset_ratio
        self.auto_save_profile = Path(auto_save_profile) if auto_save_profile else None

        self._cap: Optional[cv2.VideoCapture] = None
        self._layout: Optional[Dict[int, Tuple[int, int, int, int]]] = None
        self._frame_shape: Optional[Tuple[int, int]] = None
        self._lock = threading.Lock()
        self._debug_frame_path = Path(debug_frame_path) if debug_frame_path else None
        self._debug_frame_saved = False
        self._debug_multiview_sections_path = (
            Path(debug_multiview_sections_path) if debug_multiview_sections_path else None
        )
        self._debug_multiview_sections_saved = False
        # Optional mapping from multiview section index (1-based) to ATEM input id.
        # When provided, segments returned by get_segments() will use ATEM input ids
        # instead of raw section indices; sections not present in the mapping are
        # dropped.
        self._section_to_input_id: Dict[int, int] = dict(section_to_input_id or {})

    def _ensure_open(self) -> bool:
        if self._cap is not None and self._cap.isOpened():
            return True
        self._cap = cv2.VideoCapture(self.source)
        if not self._cap.isOpened():
            return False
        # If requested, try to configure capture resolution. This is best-effort:
        # many drivers will clamp or ignore these, but when supported it ensures
        # we see the true 16:9 multiview instead of a low-res default.
        try:
            if self.width and self.width > 0:
                self._cap.set(cv2.CAP_PROP_FRAME_WIDTH, float(self.width))
            if self.height and self.height > 0:
                self._cap.set(cv2.CAP_PROP_FRAME_HEIGHT, float(self.height))
        except Exception:
            # Never fail ingestion just because resolution hints are unsupported.
            pass
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
        if layout is not None:
            self._maybe_save_multiview_sections_debug(frame, layout)
        return layout

    def _maybe_save_multiview_sections_debug(
        self,
        frame: np.ndarray,
        layout: Dict[int, Tuple[int, int, int, int]],
    ) -> None:
        """
        Optionally save an annotated multiview image with detected sections numbered.
        This is intended for one-time debugging of the layout; it only runs once.
        """
        if (
            self._debug_multiview_sections_path is None
            or self._debug_multiview_sections_saved
            or frame is None
            or not layout
        ):
            return
        try:
            annotated = frame.copy()
            for input_id, (x, y, w, h) in sorted(layout.items()):
                x2 = x + max(0, w - 1)
                y2 = y + max(0, h - 1)
                cv2.rectangle(annotated, (x, y), (x2, y2), (0, 0, 255), 2)
                label = str(input_id)
                # Place label near top-left inside the rectangle.
                cv2.putText(
                    annotated,
                    label,
                    (x + 5, y + 25),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    0.8,
                    (0, 255, 0),
                    2,
                    cv2.LINE_AA,
                )
            self._debug_multiview_sections_path.parent.mkdir(parents=True, exist_ok=True)
            cv2.imwrite(str(self._debug_multiview_sections_path), annotated)
        except Exception:
            # Debug output should never break ingestion.
            pass
        finally:
            self._debug_multiview_sections_saved = True

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

    def seek_to_time(self, t_sec: float) -> bool:
        """
        Seek to a position in the stream (seconds). Only supported for file sources.
        Returns True if seek was attempted and succeeded (or source is live and no-op).
        """
        if self._cap is None or not self._cap.isOpened():
            return False
        # Only file sources support seek; device indices do not.
        if isinstance(self.source, (int, float)):
            return False
        try:
            self._cap.set(cv2.CAP_PROP_POS_MSEC, t_sec * 1000.0)
            return True
        except Exception:
            return False

    def read_frame(self) -> Optional[np.ndarray]:
        """Read one frame from capture. Returns BGR frame or None."""
        if not self._ensure_open():
            return None
        ok, frame = self._cap.read()
        if not ok or frame is None:
            return None
        # Optionally save the first successfully read frame to disk so the caller
        # can verify that the capture source is correct.
        if self._debug_frame_path is not None and not self._debug_frame_saved:
            try:
                self._debug_frame_path.parent.mkdir(parents=True, exist_ok=True)
                cv2.imwrite(str(self._debug_frame_path), frame)
                self._debug_frame_saved = True
            except Exception:
                # If debug saving fails, it should not break ingestion.
                self._debug_frame_saved = True
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
        segments = segment_frame(
            frame,
            layout,
            inset_px=self.inset_px,
            inset_ratio=self.inset_ratio,
        )
        # If no mapping is provided, treat layout keys as the final input ids.
        if not self._section_to_input_id:
            return segments
        # Otherwise, remap from section index (layout/input_id) to ATEM input id.
        remapped: List[Tuple[int, np.ndarray]] = []
        for section_id, crop in segments:
            atem_input = self._section_to_input_id.get(section_id)
            if atem_input is None:
                continue
            remapped.append((atem_input, crop))
        return remapped

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
    debug_frame_path: Optional[str | Path] = None,
    debug_multiview_sections_path: Optional[str | Path] = None,
    section_to_input_id: Optional[Dict[int, int]] = None,
) -> List[Tuple[int, np.ndarray]]:
    """
    One-shot: open source, read one frame, resolve layout, return segments.
    Useful for testing or single-frame processing.
    """
    with MultiviewIngest(
        source=source,
        profile_path=profile_path,
        inset_ratio=inset_ratio,
        debug_frame_path=debug_frame_path,
        debug_multiview_sections_path=debug_multiview_sections_path,
        section_to_input_id=section_to_input_id,
    ) as ingest:
        return ingest.get_segments()
