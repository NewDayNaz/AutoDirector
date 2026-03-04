#!/usr/bin/env python3
"""
Standalone multiview diagnosis: run layout + person detection + optional roamer stability
from a camera (e.g. OBS Virtual Camera) or video file, without the full director stack.

Use this to tune layout profile, section_to_input_id, detector confidence, and roamer
stability while watching a recording or live multiview. No ATEM, X32, ProPresenter, or
phase machine required.

Usage:
  # Use config.json for capture, layout, roles, roamer; override source to OBS Virtual Camera
  python run_multiview_diagnose.py --config config.json --source 0

  # Use config and default capture source from config
  python run_multiview_diagnose.py --config config.json

  # No config: CLI only (e.g. camera index 0, profile path)
  python run_multiview_diagnose.py --source 0 --profile multiview_profiles/default.json

  # Video file instead of camera
  python run_multiview_diagnose.py --config config.json --source path/to/multiview.mp4

Keys:
  q / ESC  Quit
  c        Cycle confidence threshold (0.3, 0.5, 0.7) for person detection
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import cv2
import numpy as np

# Project root on path
_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from multiview_ingest import MultiviewIngest

try:
    from atem_director import ATEMMultiviewDetector
except ImportError:
    ATEMMultiviewDetector = None

try:
    from roamer_stability import RoamerStability
except ImportError:
    RoamerStability = None


def _load_config_capture_and_roles(config_path: Path):
    """Load capture config, input_roles, and roamer from config.json. Returns (capture, input_roles, roamer_cfg)."""
    try:
        from config.load import load_config_path
        cfg = load_config_path(config_path)
        return cfg.capture, cfg.input_roles, getattr(cfg, "roamer", None)
    except Exception as e:
        logging.warning("Could not load config %s: %s", config_path, e)
        return None, None, None


def _draw_cell(
    frame: np.ndarray,
    x: int, y: int, w: int, h: int,
    input_id: int,
    role: Optional[str],
    has_person: bool,
    confidence: float,
    roamer_stable: Optional[bool],
    color: Tuple[int, int, int],
) -> None:
    cv2.rectangle(frame, (x, y), (x + w, y + h), color, 2)
    # Label: input_id [role] person conf [roamer]
    parts = [f"IN{input_id}"]
    if role:
        parts.append(role)
    parts.append("P" if has_person else "-")
    parts.append(f"{confidence:.2f}")
    if roamer_stable is not None:
        parts.append("stable" if roamer_stable else "moving")
    label = " ".join(parts)
    (tw, th), _ = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, 0.45, 1)
    # Place label INSIDE the cell near the bottom so it is never cut off by the
    # frame edges (bottom row especially).
    h_frame, w_frame = frame.shape[:2]
    # Clamp x so the label box stays on-screen.
    x0 = max(0, min(x, w_frame - tw - 4))
    # Target the bottom of the cell, but keep within the frame.
    text_baseline = min(y + h - 4, h_frame - 4)
    box_top = max(0, text_baseline - th - 4)
    # Background for text
    cv2.rectangle(frame, (x0, box_top), (x0 + tw + 4, text_baseline + 2), color, -1)
    cv2.putText(
        frame,
        label,
        (x0 + 2, text_baseline - 2),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.45,
        (0, 0, 0),
        1,
        cv2.LINE_AA,
    )


def run(
    source: int | str,
    profile_path: Optional[Path] = None,
    capture_width: Optional[int] = None,
    capture_height: Optional[int] = None,
    section_to_input_id: Optional[Dict[int, int]] = None,
    input_roles: Optional[Dict[int, str]] = None,
    roamer_input_id: Optional[int] = None,
    roamer_window_sec: float = 1.0,
    roamer_motion_threshold: float = 10.0,
    confidence: float = 0.5,
    window_name: str = "Multiview diagnose",
) -> None:
    section_to_input_id = section_to_input_id or {}
    input_roles = input_roles or {}

    ingest = MultiviewIngest(
        source=source,
        profile_path=str(profile_path) if profile_path else None,
        width=capture_width,
        height=capture_height,
        inset_ratio=0.02,
        section_to_input_id=section_to_input_id,
    )
    if ATEMMultiviewDetector is None:
        logging.error("ATEMMultiviewDetector not available (atem_director). Install ultralytics.")
        return
    detector = ATEMMultiviewDetector()
    roamer: Optional[Any] = None
    if roamer_input_id is not None and RoamerStability is not None:
        roamer = RoamerStability(
            stability_window_seconds=roamer_window_sec,
            motion_threshold=roamer_motion_threshold,
        )

    confidence_levels = [0.3, 0.5, 0.7]
    conf_index = max(0, min(len(confidence_levels) - 1, next((i for i, c in enumerate(confidence_levels) if c >= confidence), 0)))
    if confidence not in confidence_levels:
        confidence_levels = sorted(confidence_levels + [confidence])
        conf_index = confidence_levels.index(confidence)

    # Window size follows input resolution (frame size); create once we have first frame.
    window_created = False

    try:
        while True:
            frame = ingest.read_frame()
            if frame is None:
                cv2.waitKey(50)
                continue
            if not window_created:
                cv2.namedWindow(window_name, cv2.WINDOW_AUTOSIZE)
                window_created = True
            layout = ingest.get_layout(frame)
            if not layout:
                cv2.imshow(window_name, frame)
                if cv2.waitKey(30) & 0xFF in (ord("q"), 27):
                    break
                continue
            segments = ingest.get_segments(frame)
            if not segments:
                cv2.imshow(window_name, frame)
                if cv2.waitKey(30) & 0xFF in (ord("q"), 27):
                    break
                continue

            res = detector.process_segments(segments, confidence_threshold=confidence)
            results_by_input: Dict[int, Dict] = {r["input"]: r for r in res.get("all_results", [])}

            roamer_stable = False
            if roamer is not None and roamer_input_id is not None:
                for input_id, crop in segments:
                    if input_id == roamer_input_id:
                        roamer.push(crop)
                        roamer_stable = roamer.is_stable()
                        break

            # Build section_id -> display input_id (atem input id for labeled cells)
            for section_id, rect in layout.items():
                display_input_id = section_to_input_id.get(section_id, section_id)
                r = results_by_input.get(display_input_id)
                if r is None:
                    color = (128, 128, 128)
                    has_person, confidence_val = False, 0.0
                else:
                    has_person = r.get("has_person", False)
                    confidence_val = r.get("confidence", 0.0)
                    color = (0, 255, 0) if has_person else (0, 165, 255)
                role = input_roles.get(display_input_id)
                rs = roamer_stable if (roamer is not None and display_input_id == roamer_input_id) else None
                _draw_cell(
                    frame,
                    rect[0], rect[1], rect[2], rect[3],
                    display_input_id,
                    role,
                    has_person,
                    confidence_val,
                    rs,
                    color,
                )

            # Top bar: frame geometry + confidence hint
            h, w = frame.shape[:2]
            aspect = w / float(h) if h > 0 else 0.0
            bar = f"{w}x{h} AR={aspect:.2f} | confidence={confidence} [c] cycle | q/ESC quit"
            cv2.putText(frame, bar, (8, 24), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 2, cv2.LINE_AA)
            cv2.putText(frame, bar, (8, 24), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 0, 0), 1, cv2.LINE_AA)

            cv2.imshow(window_name, frame)
            key = cv2.waitKey(30) & 0xFF
            if key in (ord("q"), 27):
                break
            if key == ord("c"):
                conf_index = (conf_index + 1) % len(confidence_levels)
                confidence = confidence_levels[conf_index]
                logging.info("Confidence set to %.2f", confidence)
    finally:
        ingest.release()
        cv2.destroyAllWindows()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    ap = argparse.ArgumentParser(
        description="Run multiview analysis (layout + person detection + roamer) from camera/file without the full director.",
    )
    ap.add_argument("--config", type=Path, default=None, help="Use capture, input_roles, roamer from this config file.")
    ap.add_argument("--source", type=str, default=None, help="Override capture source: device index (e.g. 0 for OBS Virtual Camera) or path to video file.")
    ap.add_argument("--profile", type=Path, default=None, help="Multiview layout profile JSON (used if no --config or config has no profile).")
    ap.add_argument("--confidence", type=float, default=0.5, help="Person detection confidence threshold.")
    args = ap.parse_args()

    source: int | str = 0
    profile_path: Optional[Path] = None
    section_to_input_id: Dict[int, int] = {}
    input_roles: Dict[int, str] = {}
    roamer_input_id: Optional[int] = None
    roamer_window_sec = 1.0
    roamer_motion_threshold = 10.0
    capture_width: Optional[int] = None
    capture_height: Optional[int] = None

    if args.config and args.config.exists():
        capture_cfg, roles_cfg, roamer_cfg = _load_config_capture_and_roles(args.config)
        if capture_cfg:
            source = getattr(capture_cfg, "source", 0)
            if getattr(capture_cfg, "profile_path", None):
                profile_path = Path(capture_cfg.profile_path)
            section_to_input_id = getattr(capture_cfg, "section_to_input_id", None) or {}
            capture_width = getattr(capture_cfg, "width", None)
            capture_height = getattr(capture_cfg, "height", None)
        if roles_cfg:
            input_roles = getattr(roles_cfg, "by_input", None) or {}
        if roamer_cfg and getattr(roamer_cfg, "enabled", True):
            aid = getattr(roamer_cfg, "atem_input_id", 0)
            roamer_input_id = aid if (aid and aid > 0) else None
            roamer_window_sec = getattr(roamer_cfg, "stability_window_seconds", 1.0)
            roamer_motion_threshold = getattr(roamer_cfg, "motion_threshold", 10.0)

    if args.source is not None:
        try:
            source = int(args.source)
        except ValueError:
            source = args.source
    if args.profile is not None:
        profile_path = args.profile

    if not profile_path and not section_to_input_id:
        logging.info("No profile or section_to_input_id: layout will be auto-detected if possible.")

    run(
        source=source,
        profile_path=profile_path,
        capture_width=capture_width,
        capture_height=capture_height,
        section_to_input_id=section_to_input_id,
        input_roles=input_roles,
        roamer_input_id=roamer_input_id,
        roamer_window_sec=roamer_window_sec,
        roamer_motion_threshold=roamer_motion_threshold,
        confidence=args.confidence,
    )


if __name__ == "__main__":
    main()
