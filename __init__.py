# ATEM multiview ingestion and person detection.
# Use MultiviewIngest for live capture + layout + segments; use ATEMMultiviewDetector for per-segment analysis.

from .multiview_layout import load_profile, segment_frame, detect_layout_auto
from .multiview_ingest import MultiviewIngest, segments_from_capture

__all__ = [
    "load_profile",
    "segment_frame",
    "detect_layout_auto",
    "MultiviewIngest",
    "segments_from_capture",
]
