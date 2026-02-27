"""
Load and validate director config from JSON (and optional env overrides).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, Optional

from .schema import (
    ATEMConfig,
    CaptureConfig,
    DirectorConfig,
    InputRolesConfig,
    PacingConfig,
    PHASE_IDS,
    PhaseConfig,
    ProPresenterConfig,
    PTZConfig,
    RoamerConfig,
    RunSheetConfig,
    X32Config,
    validate_config,
)


def _get(obj: Dict[str, Any], key: str, default: Any = None) -> Any:
    return obj.get(key, default)


def _load_atem(raw: Any) -> ATEMConfig:
    if isinstance(raw, ATEMConfig):
        return raw
    if not isinstance(raw, dict):
        return ATEMConfig(ip=os.environ.get("ATEM_IP", "192.168.1.240"))
    return ATEMConfig(
        ip=raw.get("ip") or os.environ.get("ATEM_IP", "192.168.1.240"),
        use_preview=raw.get("use_preview", True),
        enabled=raw.get("enabled", True),
    )


def _load_capture(raw: Any) -> CaptureConfig:
    if isinstance(raw, CaptureConfig):
        return raw
    if not isinstance(raw, dict):
        return CaptureConfig()
    # Optional mapping from multiview section index (1-based) to ATEM input id.
    mapping_raw = (
        raw.get("section_to_input_id")
        or raw.get("multiview_section_to_input_id")
        or {}
    )
    section_to_input_id: Dict[int, int] = {}
    if isinstance(mapping_raw, dict):
        for k, v in mapping_raw.items():
            try:
                section = int(k)
                atem_input = int(v)
            except (ValueError, TypeError):
                continue
            if atem_input > 0:
                section_to_input_id[section] = atem_input
    return CaptureConfig(
        source=raw.get("source", 0),
        profile_path=raw.get("profile_path"),
        inset_ratio=float(raw.get("inset_ratio", 0.02)),
        debug_frame_path=raw.get("debug_frame_path"),
        debug_multiview_sections_path=raw.get("debug_multiview_sections_path"),
        section_to_input_id=section_to_input_id,
    )


def _load_input_roles(raw: Any) -> InputRolesConfig:
    if isinstance(raw, InputRolesConfig):
        return raw
    by_input: Dict[int, str] = {}
    if isinstance(raw, dict):
        for k, v in raw.items():
            try:
                idx = int(k)
                if isinstance(v, str):
                    by_input[idx] = v
            except (ValueError, TypeError):
                continue
    return InputRolesConfig(by_input=by_input)


def _load_phases(raw: Any) -> PhaseConfig:
    if isinstance(raw, PhaseConfig):
        return raw
    phase_ids = list(PHASE_IDS)
    labels: Dict[str, str] = {}
    if isinstance(raw, dict):
        phase_ids = raw.get("phase_ids", phase_ids)
        labels = raw.get("labels") or {}
    return PhaseConfig(phase_ids=phase_ids, labels=labels)


def _load_x32(raw: Any) -> Optional[X32Config]:
    if raw is None:
        return None
    if isinstance(raw, X32Config):
        return raw
    if not isinstance(raw, dict):
        return None
    return X32Config(
        host=raw.get("host", "192.168.1.1"),
        port=int(raw.get("port", 10023)),
        band_dca_index=int(raw.get("band_dca_index", 1)),
        propresenter_channel=raw.get("propresenter_channel"),
    )


def _load_propresenter(raw: Any) -> ProPresenterConfig:
    if isinstance(raw, ProPresenterConfig):
        return raw
    if not isinstance(raw, dict):
        return ProPresenterConfig()
    return ProPresenterConfig(
        host=str(raw.get("host", "127.0.0.1")).strip() or "127.0.0.1",
        port=int(raw.get("port", 50001)),
        password=(raw.get("password") or os.environ.get("PROPRESENTER_PASSWORD") or "").strip() or None,
    )


def _load_ptz(raw: Any) -> Optional[PTZConfig]:
    if raw is None:
        return None
    if isinstance(raw, PTZConfig):
        return raw
    if not isinstance(raw, dict):
        return None
    preset_per_phase = raw.get("preset_per_phase") or raw.get("ptz_preset_per_phase") or {}
    return PTZConfig(
        preset_per_phase={str(k): str(v) for k, v in preset_per_phase.items()},
        host=raw.get("host"),
        port=raw.get("port"),
    )


def _load_pacing(raw: Any) -> PacingConfig:
    if isinstance(raw, PacingConfig):
        return raw
    if not isinstance(raw, dict):
        return PacingConfig()
    return PacingConfig(
        min_seconds_on_shot=float(raw.get("min_seconds_on_shot", 5.0)),
        max_seconds_on_shot=float(raw.get("max_seconds_on_shot", 20.0)),
        sermon_min_seconds=float(raw.get("sermon_min_seconds", 15.0)),
        sermon_max_seconds=float(raw.get("sermon_max_seconds", 45.0)),
    )


def _load_roamer(raw: Any) -> RoamerConfig:
    if isinstance(raw, RoamerConfig):
        return raw
    if not isinstance(raw, dict):
        return RoamerConfig()
    return RoamerConfig(
        atem_input_id=int(raw.get("atem_input_id", 0)),
        stability_window_seconds=float(raw.get("stability_window_seconds", 1.0)),
        motion_threshold=float(raw.get("motion_threshold", 10.0)),
    )


def _load_run_sheet(raw: Any) -> Optional[RunSheetConfig]:
    if raw is None:
        return None
    if isinstance(raw, RunSheetConfig):
        return raw
    if not isinstance(raw, dict):
        return None
    return RunSheetConfig(
        start_time_iso=raw.get("start_time_iso"),
        phase_durations_seconds=raw.get("phase_durations_seconds"),
    )


def load_config(data: Dict[str, Any]) -> DirectorConfig:
    """Build DirectorConfig from a parsed dict (e.g. from JSON)."""
    return DirectorConfig(
        atem=_load_atem(_get(data, "atem", {})),
        capture=_load_capture(_get(data, "capture", {})),
        input_roles=_load_input_roles(_get(data, "input_roles", {})),
        phases=_load_phases(_get(data, "phases", {})),
        playlist_item_to_phase={
            str(k): str(v) for k, v in (_get(data, "playlist_item_to_phase") or {}).items()
        },
        phases_locked_to_role={
            str(k): str(v) for k, v in (_get(data, "phases_locked_to_role") or {}).items()
        },
        propresenter=_load_propresenter(_get(data, "propresenter", {})),
        x32=_load_x32(_get(data, "x32")) or (_load_x32(_get(data, "X32"))),
        ptz=_load_ptz(_get(data, "ptz")) or (_load_ptz(_get(data, "PTZ"))),
        pacing=_load_pacing(_get(data, "pacing", {})),
        transition_duration=float(_get(data, "transition_duration", 0.25)),
        backup_input_id=int(_get(data, "backup_input_id", 1)),
        backup_timeout_seconds=float(_get(data, "backup_timeout_seconds", 10.0)),
        roamer=_load_roamer(_get(data, "roamer", {})),
        loop_rate_hz=float(_get(data, "loop_rate_hz", 10.0)),
        run_sheet=_load_run_sheet(_get(data, "run_sheet")),
        dwell_seconds=float(_get(data, "dwell_seconds", 0.75)),
    )


def load_config_path(path: str | Path) -> DirectorConfig:
    """Load config from a JSON file. Raises on file error or invalid JSON."""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"Config file not found: {path}")
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError("Config JSON must be an object")
    return load_config(data)


def load_config_path_optional(path: Optional[str | Path]) -> Optional[DirectorConfig]:
    """Load config from path if provided; return None otherwise."""
    if not path:
        return None
    return load_config_path(path)
