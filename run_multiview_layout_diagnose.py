#!/usr/bin/env python3
"""
Multiview layout diagnosis and configuration helper.

Goals:
- See what the layout detector thinks the grid is (auto vs profile).
- See section indices for each cell so you can build/update section_to_input_id.
- Optionally save the current layout to a profile JSON.

Typical usage:
  # Use capture + mapping from config.json and your live/OBS multiview source
  python run_multiview_layout_diagnose.py --config config.json

  # Override source (e.g. OBS Virtual Camera at index 0)
  python run_multiview_layout_diagnose.py --config config.json --source 0

  # No config: just point at a device or file and optional profile
  python run_multiview_layout_diagnose.py --source 0 --profile multiview_profiles/default.json

Keys:
  q / ESC   Quit
  n         Grab a new frame from the source and re-run detection
  t         Toggle view mode: auto layout / profile layout / both
  s         Save the CURRENT layout (according to view mode) to --output-profile

Notes:
- This script does NOT touch config.json; it only reads it (if provided) and
  optionally writes a profile JSON you can then reference from capture.profile_path.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Dict, Optional, Tuple

import cv2
import numpy as np

# Project root on path
_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from multiview_layout import (
    load_profile,
    detect_layout_auto,
)


def _load_config_capture_and_mapping(config_path: Path):
    """
    Load capture/source, section_to_input_id, and input_roles from config.json
    using the existing config.load helper.
    """
    try:
        from config.load import load_config_path

        cfg = load_config_path(config_path)
        capture_cfg = cfg.capture
        section_to_input_id = dict(getattr(capture_cfg, "section_to_input_id", {}) or {})
        input_roles = dict(getattr(cfg.input_roles, "by_input", {}) or {})
        return capture_cfg, section_to_input_id, input_roles
    except Exception as e:
        logging.warning("Could not load config %s: %s", config_path, e)
        return None, {}, {}


def _open_source(source: int | str, width: Optional[int] = None, height: Optional[int] = None) -> cv2.VideoCapture:
    cap = cv2.VideoCapture(source)
    if not cap.isOpened():
        raise RuntimeError(f"Could not open source {source!r}")
    # Best-effort: request specific resolution if provided.
    try:
        if width and width > 0:
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, float(width))
        if height and height > 0:
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, float(height))
    except Exception:
        pass
    return cap


def _read_frame(cap: cv2.VideoCapture) -> Optional[np.ndarray]:
    ok, frame = cap.read()
    if not ok or frame is None:
        return None
    return frame


def _save_profile(path: Path, width: int, height: int, layout: Dict[int, Tuple[int, int, int, int]]) -> None:
    """
    Save layout to JSON in the same format expected by load_profile:
    {"width": W, "height": H, "inputs": {"1": [x,y,w,h], ...}}.
    """
    data = {
        "width": int(width),
        "height": int(height),
        "inputs": {str(k): [int(v[0]), int(v[1]), int(v[2]), int(v[3])] for k, v in sorted(layout.items())},
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    logging.info("Saved layout profile with %d cells to %s", len(layout), path)


def _draw_layout(
    frame: np.ndarray,
    layout: Dict[int, Tuple[int, int, int, int]],
    *,
    label_prefix: str,
    color: Tuple[int, int, int],
    section_to_input_id: Dict[int, int],
    input_roles: Dict[int, str],
) -> None:
    """
    Draw rectangles for each layout cell and annotate with section index and mapping:
      e.g. "S3 → IN1 sermon_hero"
    """
    h_frame, w_frame = frame.shape[:2]
    for section_id, rect in layout.items():
        x, y, w, h = rect
        if w <= 0 or h <= 0:
            continue
        cv2.rectangle(frame, (x, y), (x + w, y + h), color, 2)

        atem_input = section_to_input_id.get(section_id)
        role = input_roles.get(atem_input) if atem_input is not None else None

        if atem_input is None:
            text = f"{label_prefix}{section_id}"
        elif role:
            text = f"{label_prefix}{section_id} → IN{atem_input} {role}"
        else:
            text = f"{label_prefix}{section_id} → IN{atem_input}"

        (tw, th), _ = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, 0.55, 1)
        # Center label roughly in the cell.
        cx = x + w // 2
        cy = y + h // 2
        x0 = max(0, min(cx - tw // 2 - 2, w_frame - tw - 4))
        y0 = max(0, min(cy - th // 2 - 2, h_frame - th - 4))
        cv2.rectangle(frame, (x0, y0), (x0 + tw + 4, y0 + th + 6), color, -1)
        cv2.putText(
            frame,
            text,
            (x0 + 2, y0 + th + 1),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.55,
            (0, 0, 0),
            1,
            cv2.LINE_AA,
        )


def run(
    source: int | str,
    profile_path: Optional[Path],
    output_profile: Optional[Path],
    section_to_input_id: Dict[int, int],
    input_roles: Dict[int, str],
    capture_width: Optional[int] = None,
    capture_height: Optional[int] = None,
    window_name: str = "Multiview layout diagnose",
) -> None:
    cap = _open_source(source, width=capture_width, height=capture_height)
    logging.info("Opened source %r for layout diagnosis", source)

    # View modes: 0=auto, 1=profile, 2=both
    view_mode = 0

    auto_layout: Optional[Dict[int, Tuple[int, int, int, int]]] = None
    profile_layout: Optional[Dict[int, Tuple[int, int, int, int]]] = None

    if profile_path is not None:
        loaded = load_profile(profile_path)
        if loaded:
            profile_layout = loaded
            logging.info("Loaded profile %s with %d cells", profile_path, len(profile_layout))
        else:
            logging.warning("Profile %s not found or invalid; ignoring.", profile_path)

    cv2.namedWindow(window_name, cv2.WINDOW_AUTOSIZE)

    try:
        frame = _read_frame(cap)
        if frame is None:
            logging.error("Could not grab initial frame from %r", source)
            return

        while True:
            # Recompute auto layout every time we grab a new frame.
            auto_layout = detect_layout_auto(frame) or {}

            display = frame.copy()

            # Draw according to view mode
            if view_mode == 0 and auto_layout:
                _draw_layout(
                    display,
                    auto_layout,
                    label_prefix="A",
                    color=(0, 255, 255),  # yellow
                    section_to_input_id=section_to_input_id,
                    input_roles=input_roles,
                )
                mode_text = f"auto layout ({len(auto_layout)} cells)"
            elif view_mode == 1 and profile_layout:
                _draw_layout(
                    display,
                    profile_layout,
                    label_prefix="P",
                    color=(0, 255, 0),  # green
                    section_to_input_id=section_to_input_id,
                    input_roles=input_roles,
                )
                mode_text = f"profile layout ({len(profile_layout)} cells)"
            else:
                # both (where present)
                mode_text = "both layouts"
                if auto_layout:
                    _draw_layout(
                        display,
                        auto_layout,
                        label_prefix="A",
                        color=(0, 255, 255),
                        section_to_input_id=section_to_input_id,
                        input_roles=input_roles,
                    )
                if profile_layout:
                    _draw_layout(
                        display,
                        profile_layout,
                        label_prefix="P",
                        color=(0, 255, 0),
                        section_to_input_id=section_to_input_id,
                        input_roles=input_roles,
                    )

            # Top bar: instructions + mode + frame geometry
            fh, fw = frame.shape[:2]
            aspect = fw / float(fh) if fh > 0 else 0.0
            bar = f"{fw}x{fh} AR={aspect:.2f} | {mode_text} | [t] toggle view  [n] new frame  [s] save profile  [q/ESC] quit"
            cv2.putText(display, bar, (8, 24), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (255, 255, 255), 2, cv2.LINE_AA)
            cv2.putText(display, bar, (8, 24), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 0, 0), 1, cv2.LINE_AA)

            cv2.imshow(window_name, display)
            key = cv2.waitKey(0) & 0xFF

            if key in (ord("q"), 27):
                break
            if key == ord("t"):
                view_mode = (view_mode + 1) % 3
                logging.info("View mode set to %s", {0: "auto", 1: "profile", 2: "both"}[view_mode])
                continue
            if key == ord("n"):
                new = _read_frame(cap)
                if new is not None:
                    frame = new
                else:
                    logging.warning("Could not grab new frame; reusing previous frame.")
                continue
            if key == ord("s") and output_profile is not None:
                # Decide which layout to save based on view mode, preferring profile when in that mode.
                layout_to_save: Dict[int, Tuple[int, int, int, int]] = {}
                if view_mode == 1 and profile_layout:
                    layout_to_save = profile_layout
                elif view_mode == 0 and auto_layout:
                    layout_to_save = auto_layout
                elif view_mode == 2:
                    # In "both" mode, prefer auto if present; fall back to profile.
                    layout_to_save = auto_layout or profile_layout or {}

                if not layout_to_save:
                    logging.warning("No layout to save in current mode.")
                else:
                    h, w = frame.shape[:2]
                    _save_profile(output_profile, w, h, layout_to_save)

    finally:
        cap.release()
        cv2.destroyAllWindows()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    ap = argparse.ArgumentParser(
        description="Diagnose multiview layout detection (auto vs profile) and help configure section_to_input_id.",
    )
    ap.add_argument(
        "--config",
        type=Path,
        default=None,
        help="Use capture.source, capture.section_to_input_id, and input_roles from this config file.",
    )
    ap.add_argument(
        "--source",
        type=str,
        default=None,
        help="Override capture source: device index (e.g. 0 for OBS Virtual Camera) or path to video file.",
    )
    ap.add_argument(
        "--profile",
        type=Path,
        default=None,
        help="Profile JSON to compare against auto layout (if omitted, only auto layout is used).",
    )
    ap.add_argument(
        "--output-profile",
        type=Path,
        default=Path("multiview_profiles/diagnosed_layout.json"),
        help="Where to write a profile when you press 's'.",
    )
    args = ap.parse_args()

    source: int | str = 0
    section_to_input_id: Dict[int, int] = {}
    input_roles: Dict[int, str] = {}
    capture_width: Optional[int] = None
    capture_height: Optional[int] = None

    if args.config and args.config.exists():
        capture_cfg, section_to_input_id, input_roles = _load_config_capture_and_mapping(args.config)
        if capture_cfg:
            source = getattr(capture_cfg, "source", 0)
            capture_width = getattr(capture_cfg, "width", None)
            capture_height = getattr(capture_cfg, "height", None)

    if args.source is not None:
        try:
            source = int(args.source)
        except ValueError:
            source = args.source

    profile_path = args.profile
    run(
        source=source,
        profile_path=profile_path,
        output_profile=args.output_profile,
        section_to_input_id=section_to_input_id,
        input_roles=input_roles,
        capture_width=capture_width,
        capture_height=capture_height,
    )


if __name__ == "__main__":
    main()

