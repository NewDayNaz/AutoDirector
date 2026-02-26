"""
ATEM multiview layout detection and segmentation.

Supports:
- Loading a saved profile (JSON, same format as revamp crop mapper).
- Automatic line-based grid detection when no profile is available.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import cv2
import numpy as np


def load_profile(profile_path: str | Path) -> Optional[Dict[str, List[Tuple[int, int, int, int]]]]:
    """
    Load layout from JSON profile. Format: {"width": W, "height": H, "inputs": {"1": [x,y,w,h], ...}}.
    Returns inputs as input_id -> (x, y, w, h), or None if file missing/invalid.
    """
    path = Path(profile_path)
    if not path.exists():
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return None
    inputs = data.get("inputs")
    if not inputs or not isinstance(inputs, dict):
        return None
    out = {}
    for k, v in inputs.items():
        try:
            input_id = int(k)
            rect = tuple(int(x) for x in v)
            if len(rect) == 4:
                out[input_id] = rect
        except (ValueError, TypeError):
            continue
    return out if out else None


def _cluster_lines(positions: np.ndarray, min_gap_ratio: float = 0.01) -> np.ndarray:
    """Cluster line positions (e.g. from Hough) into distinct grid lines; return sorted unique positions."""
    if positions.size == 0:
        return positions
    positions = np.sort(np.unique(positions))
    gap = np.diff(positions)
    min_gap = max(1, positions.max() * min_gap_ratio)
    # Merge lines that are too close
    keep = np.ones(len(positions), dtype=bool)
    for i in range(1, len(positions)):
        if positions[i] - positions[i - 1] < min_gap:
            keep[i] = False
    positions = positions[keep]
    return positions


def detect_grid_lines(
    image: np.ndarray,
    *,
    canny_low: int = 50,
    canny_high: int = 150,
    hough_threshold: int = 80,
    min_line_length_ratio: float = 0.05,
    min_gap_ratio: float = 0.015,
) -> Tuple[np.ndarray, np.ndarray]:
    """
    Detect horizontal and vertical grid lines via Canny + Hough.
    Returns (horizontal_positions (y), vertical_positions (x)) as 1D arrays of line positions.
    """
    h, w = image.shape[:2]
    gray = image if len(image.shape) == 2 else cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    edges = cv2.Canny(gray, canny_low, canny_high)
    min_len = int(min(h, w) * min_line_length_ratio)

    # Horizontal lines (theta = 0)
    horz = cv2.HoughLinesP(
        edges, 1, np.pi / 180, threshold=hough_threshold,
        minLineLength=min_len, maxLineGap=max(2, min_len // 2),
    )
    hy = np.array([], dtype=np.int64)
    if horz is not None:
        for line in horz.reshape(-1, 4):
            y0, y1 = line[1], line[3]
            hy = np.append(hy, (y0 + y1) // 2)
    hy = _cluster_lines(hy, min_gap_ratio)

    # Vertical lines (theta = pi/2)
    vert = cv2.HoughLinesP(
        edges, 1, np.pi / 180, threshold=hough_threshold,
        minLineLength=min_len, maxLineGap=max(2, min_len // 2),
    )
    vx = np.array([], dtype=np.int64)
    if vert is not None:
        for line in vert.reshape(-1, 4):
            x0, x1 = line[0], line[2]
            vx = np.append(vx, (x0 + x1) // 2)
    vx = _cluster_lines(vx, min_gap_ratio)

    return hy, vx


def grid_lines_to_rectangles(
    hy: np.ndarray,
    vx: np.ndarray,
    frame_height: int,
    frame_width: int,
    min_cell_ratio: float = 0.02,
) -> List[Tuple[int, int, int, int]]:
    """
    Convert horizontal and vertical line positions to a list of (x, y, w, h) rectangles.
    Orders left-to-right, top-to-bottom. Filters out very small cells.
    """
    # Add frame boundaries so we have full grid
    y_pos = np.concatenate([[0], np.sort(hy), [frame_height]])
    x_pos = np.concatenate([[0], np.sort(vx), [frame_width]])
    min_area = frame_height * frame_width * min_cell_ratio * min_cell_ratio
    rects = []
    for i in range(len(y_pos) - 1):
        for j in range(len(x_pos) - 1):
            x, y = int(x_pos[j]), int(y_pos[i])
            w = int(x_pos[j + 1] - x_pos[j])
            h = int(y_pos[i + 1] - y_pos[i])
            if w * h >= min_area and w >= 4 and h >= 4:
                rects.append((x, y, w, h))
    return rects


def detect_layout_auto(
    frame: np.ndarray,
    *,
    max_cells: int = 16,
    min_cells: int = 2,
) -> Optional[Dict[int, Tuple[int, int, int, int]]]:
    """
    Automatically detect multiview grid from one frame using line detection.
    Returns input_id -> (x, y, w, h) for each cell, or None if detection fails.
    """
    h, w = frame.shape[:2]
    hy, vx = detect_grid_lines(frame)
    rects = grid_lines_to_rectangles(hy, vx, h, w)
    if not (min_cells <= len(rects) <= max_cells):
        return None
    return {i + 1: r for i, r in enumerate(rects)}


def apply_inset(
    rect: Tuple[int, int, int, int],
    inset_px: int = 0,
    inset_ratio: float = 0.0,
) -> Tuple[int, int, int, int]:
    """Apply border inset to a rectangle (x, y, w, h). Returns (x, y, w, h) with inset applied."""
    x, y, w, h = rect
    if inset_ratio > 0:
        inset_px = max(inset_px, int(min(w, h) * inset_ratio))
    if inset_px <= 0:
        return rect
    ix = min(inset_px, w // 3)
    iy = min(inset_px, h // 3)
    return (x + ix, y + iy, w - 2 * ix, h - 2 * iy)


def segment_frame(
    frame: np.ndarray,
    layout: Dict[int, Tuple[int, int, int, int]],
    inset_px: int = 0,
    inset_ratio: float = 0.02,
) -> List[Tuple[int, np.ndarray]]:
    """
    Crop frame into per-input segments using layout. Optionally apply inset to reduce borders/labels.
    Returns list of (input_id, crop) in ascending input_id order.
    """
    segments = []
    for input_id in sorted(layout.keys()):
        rect = layout[input_id]
        rect = apply_inset(rect, inset_px=inset_px, inset_ratio=inset_ratio)
        x, y, w, h = rect
        if w <= 0 or h <= 0:
            continue
        crop = frame[y : y + h, x : x + w]
        if crop.size > 0:
            segments.append((input_id, crop))
    return segments
