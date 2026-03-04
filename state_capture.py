"""
Record/replay state for the AutoDirector.

Listen/record mode: capture the state that feeds the director (ProPresenter, X32,
detector results, phase, etc.) at a configurable interval and write to a JSON file.

Replay mode: load that file and play it back in sync with a multiview video file,
so the director runs with replayed state + video context for tuning behavior.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

STATE_FORMAT_VERSION = 1


def build_state_frame_from_tick(tick_state: Dict[str, Any], elapsed_sec: float) -> Dict[str, Any]:
    """
    Build a serializable state frame from the director's tick state snapshot.
    tick_state is the dict passed to _state_capture_callback; we add t and ensure JSON-safe types.
    """
    frame: Dict[str, Any] = {
        "t": round(elapsed_sec, 6),
        "phase": tick_state.get("phase"),
        "pp_phase": tick_state.get("pp_phase"),
        "pp_item_name": tick_state.get("pp_item_name"),
        "pp_slide_type": tick_state.get("pp_slide_type"),
        "pp_slide_index": tick_state.get("pp_slide_index"),
        "stage_layout": tick_state.get("stage_layout"),
        "pp_level": float(tick_state["pp_level"]) if tick_state.get("pp_level") is not None else 0.0,
        "pastor_muted": tick_state.get("pastor_muted"),
        "band_muted": tick_state.get("band_muted"),
        "program_input": tick_state.get("program_input"),
        "inputs_with_people": list(tick_state.get("inputs_with_people") or []),
        "roamer_stable": bool(tick_state.get("roamer_stable", False)),
        "sermon_bumper_active": bool(tick_state.get("sermon_bumper_active", False)),
        "external_phase_override": tick_state.get("external_phase_override"),
        "external_phase_reason": tick_state.get("external_phase_reason"),
    }
    return frame


class StateRecorder:
    """
    Records director state frames at a configurable interval.
    Call start() then attach to director via set_state_capture_callback();
    each tick the director will push a state snapshot; we sample at record_interval_sec
    and append. Call stop() and write() to save to file.
    """

    def __init__(
        self,
        output_path: str | Path,
        record_interval_sec: float = 1.0 / 15.0,
        loop_rate_hz: float = 15.0,
    ):
        self.output_path = Path(output_path)
        self.record_interval_sec = max(1.0 / 60.0, record_interval_sec)
        self.loop_rate_hz = loop_rate_hz
        self._frames: List[Dict[str, Any]] = []
        self._start_monotonic: Optional[float] = None
        self._start_wall: Optional[float] = None
        self._last_record_t: float = 0.0
        self._running = False

    def start(self) -> None:
        self._frames = []
        self._start_monotonic = time.monotonic()
        self._start_wall = time.time()
        self._last_record_t = 0.0
        self._running = True
        logger.info(
            "StateRecorder started: output=%s interval=%.3fs",
            self.output_path,
            self.record_interval_sec,
        )

    def stop(self) -> None:
        self._running = False

    def on_tick_state(self, tick_state: Dict[str, Any]) -> None:
        """Called by director each tick with the state used for that tick."""
        if not self._running or self._start_monotonic is None:
            return
        elapsed = time.monotonic() - self._start_monotonic
        if elapsed - self._last_record_t < self.record_interval_sec and self._frames:
            return
        self._last_record_t = elapsed
        frame = build_state_frame_from_tick(tick_state, elapsed)
        self._frames.append(frame)

    def write(self) -> int:
        """Write recorded frames to output_path. Returns number of frames written."""
        self.stop()
        if not self._frames:
            logger.warning("StateRecorder: no frames to write")
            return 0
        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        started_at = (
            time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self._start_wall))
            if self._start_wall is not None
            else None
        )
        payload: Dict[str, Any] = {
            "version": STATE_FORMAT_VERSION,
            "loop_rate_hz": self.loop_rate_hz,
            "record_interval_sec": self.record_interval_sec,
            "started_at": started_at,
            "frame_count": len(self._frames),
            "duration_sec": self._frames[-1]["t"] if self._frames else 0.0,
            "frames": self._frames,
        }
        with open(self.output_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
        logger.info("StateRecorder wrote %d frames to %s", len(self._frames), self.output_path)
        return len(self._frames)


def load_state_recording(path: str | Path) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    """
    Load a state recording file. Returns (metadata, frames).
    metadata includes version, loop_rate_hz, record_interval_sec, duration_sec, frame_count.
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"State recording not found: {path}")
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    frames = data.get("frames", [])
    metadata = {
        "version": data.get("version", 0),
        "loop_rate_hz": data.get("loop_rate_hz", 15.0),
        "record_interval_sec": data.get("record_interval_sec"),
        "started_at": data.get("started_at"),
        "frame_count": len(frames),
        "duration_sec": data.get("duration_sec", frames[-1]["t"] if frames else 0.0),
    }
    return metadata, frames


def get_state_at_t(
    frames: List[Dict[str, Any]],
    t: float,
) -> Dict[str, Any]:
    """
    Return the state frame at time t (elapsed seconds).
    Uses the latest frame with frame["t"] <= t; if t < first frame, returns first frame.
    """
    if not frames:
        return {}
    # Binary search or linear: for typical lengths linear is fine.
    best = frames[0]
    for f in frames:
        if f.get("t", 0) <= t:
            best = f
        else:
            break
    return dict(best)
