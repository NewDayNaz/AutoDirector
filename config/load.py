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
    AudioBiasConfig,
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
    SermonBumperConfig,
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
    rules: Dict[str, Any] = {}
    if isinstance(raw, dict):
        phase_ids = raw.get("phase_ids", phase_ids)
        labels = raw.get("labels") or {}
        rules = raw.get("rules") or {}
    phase_rules: Dict[str, "PhaseRuleConfig"] = {}
    if isinstance(rules, dict):
        from .schema import PhaseRuleConfig  # local import to avoid cycles

        for key, value in rules.items():
            if not isinstance(value, dict):
                continue
            phase_rules[str(key)] = PhaseRuleConfig(
                dwell_seconds=value.get("dwell_seconds"),
                min_seconds_on_shot=value.get("min_seconds_on_shot"),
                allowed_roles=list(value.get("allowed_roles", []) or []),
                rotate=bool(value.get("rotate", False)),
            )
    return PhaseConfig(phase_ids=phase_ids, labels=labels, rules=phase_rules)


def _load_x32(raw: Any) -> Optional[X32Config]:
    if raw is None:
        return None
    if isinstance(raw, X32Config):
        return raw
    if not isinstance(raw, dict):
        return None
    pastor_dca_index = raw.get("pastor_dca_index")
    try:
        pastor_dca_index = int(pastor_dca_index) if pastor_dca_index is not None else None
    except (ValueError, TypeError):
        pastor_dca_index = None
    return X32Config(
        host=raw.get("host", "192.168.1.1"),
        port=int(raw.get("port", 10023)),
        band_dca_index=int(raw.get("band_dca_index", 1)),
        pastor_dca_index=pastor_dca_index,
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


def _load_audio_bias(raw: Any) -> AudioBiasConfig:
    if isinstance(raw, AudioBiasConfig):
        return raw
    if not isinstance(raw, dict):
        return AudioBiasConfig()
    return AudioBiasConfig(
        enabled=bool(raw.get("enabled", True)),
        band_threshold=float(raw.get("band_threshold", 0.3)),
        speaking_threshold=float(raw.get("speaking_threshold", 0.1)),
        use_stage_layout=bool(raw.get("use_stage_layout", True)),
        hysteresis_seconds=float(raw.get("hysteresis_seconds", 1.5)),
        band_layout_keywords=list(raw.get("band_layout_keywords", ["LYRICS", "WORSHIP"])),
        speaking_layout_keywords=list(raw.get("speaking_layout_keywords", ["TEACH", "PREACH", "LIVE"])),
        band_preferred_roles=list(
            raw.get("band_preferred_roles", ["roamer", "ptz", "fixed_1", "fixed_2"])
        ),
        speaking_preferred_roles=list(
            raw.get("speaking_preferred_roles", ["sermon_hero", "sermon_ptz", "sermon_roamer", "ptz", "roamer"])
        ),
    )


def _load_sermon_bumper(raw: Any) -> SermonBumperConfig:
    if isinstance(raw, SermonBumperConfig):
        return raw
    if not isinstance(raw, dict):
        return SermonBumperConfig()
    return SermonBumperConfig(
        enabled=bool(raw.get("enabled", True)),
        bumper_phase_id=str(raw.get("bumper_phase_id", "BumperSermon")),
        sermon_phase_id=str(raw.get("sermon_phase_id", "Sermon")),
        layout_keywords=list(raw.get("layout_keywords", ["VIDEO", "BUMPER"])),
        audio_min_factor=float(raw.get("audio_min_factor", 0.6)),
        hysteresis_seconds=float(raw.get("hysteresis_seconds", 0.6)),
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
    raw_fallback_phase = _get(data, "unmapped_playlist_item_fallback_phase", None)
    fallback_phase = str(raw_fallback_phase) if raw_fallback_phase is not None else None
    panic_safe_input_default = int(_get(data, "panic_safe_input_id", _get(data, "backup_input_id", 1)))
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
        audio_bias=_load_audio_bias(_get(data, "audio_bias", {})),
        sermon_bumper=_load_sermon_bumper(_get(data, "sermon_bumper", {})),
        unmapped_playlist_item_fallback_phase=fallback_phase,
        transition_duration=float(_get(data, "transition_duration", 0.25)),
        backup_input_id=int(_get(data, "backup_input_id", 1)),
        backup_timeout_seconds=float(_get(data, "backup_timeout_seconds", 10.0)),
        panic_safe_input_id=panic_safe_input_default,
        panic_label=_get(data, "panic_label"),
        lock_to_input_timeout_seconds=float(_get(data, "lock_to_input_timeout_seconds", 30.0)),
        roamer=_load_roamer(_get(data, "roamer", {})),
        loop_rate_hz=float(_get(data, "loop_rate_hz", 10.0)),
        run_sheet=_load_run_sheet(_get(data, "run_sheet")),
        dwell_seconds=float(_get(data, "dwell_seconds", 0.75)),
        detector_confidence_threshold=float(_get(data, "detector_confidence_threshold", 0.5)),
        # rules_plugins and log_level are left to their dataclass defaults unless explicitly provided.
        rules_plugins=list(_get(data, "rules_plugins", [])),
        log_level=str(_get(data, "log_level", "INFO")),
        decision_log_max_entries=int(_get(data, "decision_log_max_entries", 100)),
    )


def load_config_path(path: str | Path) -> DirectorConfig:
    """Load config from a JSON file (optionally with a local override).

    If a sibling file with suffix `.local.json` exists (e.g. config.local.json),
    its keys are shallow-merged into the base JSON before building DirectorConfig.
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"Config file not found: {path}")
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError("Config JSON must be an object")
    # Optional local override: same name with `.local.json` suffix.
    local_path = path.with_name(path.stem + ".local.json")
    if local_path.exists():
        with open(local_path, "r", encoding="utf-8") as f:
            local = json.load(f)
        if isinstance(local, dict):
            data.update(local)
    return load_config(data)


def load_config_path_optional(path: Optional[str | Path]) -> Optional[DirectorConfig]:
    """Load config from path if provided; return None otherwise."""
    if not path:
        return None
    return load_config_path(path)
