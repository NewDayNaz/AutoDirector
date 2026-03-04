"""
Canonical schema for Church Auto-Director configuration.

Defines input roles, phases, playlist→phase mapping, X32, PTZ, pacing,
transition, backup, roamer stability, and loop rate.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


# Phase IDs matching the plan (Intro, BumperIn, Band1, ...).
PHASE_IDS = (
    "Intro", 
    "BumperIn",
    "Band",
    "Band1",
    "Welcome",
    "Greeting",
    "Announce",
    "BibleTime",
    "Band2",
    "PrayerTime",
    "Band3",
    "Band4",
    "Band5",
    "Acoustic",
    "BumperSermon",
    "Sermon",
    "BandOrDismiss",
    "Outro"
)

# Input role names used in phase rules.
INPUT_ROLE_NAMES = (
    "cg",
    "ptz",
    "roamer",
    "sermon_hero",
    "sermon_ptz",
    "sermon_roamer",
    "fixed_1",
    "fixed_2",
    "playback",
    "bumper",
)


@dataclass
class ATEMConfig:
    """ATEM switcher connection and behavior."""
    ip: str
    # Preview-before-take: if True, set preview then transition; else cut directly.
    use_preview: bool = True
    # When False, do not connect to ATEM (no-op mode for testing without hardware).
    enabled: bool = True


@dataclass
class CaptureConfig:
    """Multiview capture source and layout."""
    source: int | str = 0  # Device index or file path
    profile_path: Optional[str] = None
    # Optional requested capture resolution. When set, MultiviewIngest will
    # attempt to configure the capture device to this width/height via
    # cv2.CAP_PROP_FRAME_WIDTH/HEIGHT. Drivers may clamp or ignore these.
    width: Optional[int] = None
    height: Optional[int] = None
    inset_ratio: float = 0.02
    # Optional path to save the first captured multiview frame for debugging.
    debug_frame_path: Optional[str] = None
    # Optional path to save an annotated multiview image with detected sections numbered.
    debug_multiview_sections_path: Optional[str] = None
    # Optional mapping from multiview section index (1-based, as numbered in the
    # annotated debug image) to ATEM input id. Sections not present in this
    # mapping are ignored by the director.
    section_to_input_id: Dict[int, int] = field(default_factory=dict)


@dataclass
class InputRolesConfig:
    """Maps ATEM input index (1-based) to role name."""
    # input_id (int) -> role (str), e.g. {1: "cg", 2: "ptz", 3: "roamer"}
    by_input: Dict[int, str] = field(default_factory=dict)

    def role_for_input(self, input_id: int) -> Optional[str]:
        return self.by_input.get(input_id)

    def inputs_for_role(self, role: str) -> List[int]:
        return [i for i, r in self.by_input.items() if r == role]


@dataclass
class PhaseRuleConfig:
    """
    Optional per-phase rule overrides.
    - dwell_seconds: override global DirectorConfig.dwell_seconds for this phase.
    - min_seconds_on_shot: override PacingConfig.min_seconds_on_shot / sermon_min_seconds.
    - allowed_roles: if set, restrict eligible inputs to these roles only.
    - rotate: when true, rotate through eligible inputs instead of always picking the first.
    """
    dwell_seconds: Optional[float] = None
    min_seconds_on_shot: Optional[float] = None
    allowed_roles: List[str] = field(default_factory=list)
    rotate: bool = False


@dataclass
class PhaseConfig:
    """Phase list with optional labels (for UI) and per-phase rule overrides."""
    phase_ids: List[str] = field(default_factory=lambda: list(PHASE_IDS))
    # Optional label per phase for display
    labels: Dict[str, str] = field(default_factory=dict)
    # Optional per-phase rule overrides keyed by phase id.
    rules: Dict[str, PhaseRuleConfig] = field(default_factory=dict)

    def label_for(self, phase_id: str) -> str:
        return self.labels.get(phase_id, phase_id)

    def rule_for(self, phase_id: str) -> Optional[PhaseRuleConfig]:
        return self.rules.get(phase_id)


@dataclass
class X32Config:
    """Behringer X32 OSC: band/pastor DCAs and ProPresenter channel."""
    host: str = "192.168.1.1"
    port: int = 10023
    band_dca_index: int = 1  # DCA 1 = band
    pastor_dca_index: Optional[int] = None  # Optional DCA index for pastor mic
    propresenter_channel: Optional[int] = None  # Channel index for PP computer level

    @property
    def enabled(self) -> bool:
        return bool(self.host and self.port > 0)


@dataclass
class ProPresenterConfig:
    """ProPresenter 7 WebSocket API connection (for phase from playlist)."""
    host: str = "127.0.0.1"
    port: int = 50001
    # Optional: set when ProPresenter Network > Remote has a password
    password: Optional[str] = None


@dataclass
class PTZConfig:
    """PTZ preset recall per phase. If empty, PTZ is disabled."""
    preset_per_phase: Dict[str, str] = field(default_factory=dict)  # phase_id -> preset name/id
    # Optional: connection params (vendor-specific); adapter may use env or separate config
    host: Optional[str] = None
    port: Optional[int] = None

    @property
    def enabled(self) -> bool:
        return bool(self.preset_per_phase)


@dataclass
class PacingConfig:
    """Min/max time on shot (seconds) for cut variety."""
    min_seconds_on_shot: float = 5.0
    max_seconds_on_shot: float = 20.0
    # Sermon can have different pacing (15–45 s)
    sermon_min_seconds: float = 15.0
    sermon_max_seconds: float = 45.0


@dataclass
class AudioBiasConfig:
    """
    Optional audio-driven biasing between band and speaking.
    Used to hint candidate selection based on X32 and ProPresenter signals.
    """
    enabled: bool = True
    # Minimum ProPresenter channel level (0–1) to consider visuals/lyrics \"up\" for band.
    band_threshold: float = 0.3
    # Lower threshold used when inferring speaking from low but present content.
    speaking_threshold: float = 0.1
    # When true, use ProPresenter stage display layout name as an additional hint
    # (e.g. layouts containing \"LYRICS\" or \"WORSHIP\" imply band; \"TEACH\"/\"LIVE\" imply speaking).
    use_stage_layout: bool = True
    # Require a new inferred audio mode (band/speaking/neutral) to be stable this long
    # before adopting it, to avoid rapid toggling.
    hysteresis_seconds: float = 1.5
    # Layout keywords used to infer band vs speaking from the ProPresenter stage display layout name.
    # Defaults match existing hardcoded behavior: layouts containing \"LYRICS\"/\"WORSHIP\"
    # imply band; \"TEACH\"/\"PREACH\"/\"LIVE\" imply speaking.
    band_layout_keywords: List[str] = field(default_factory=lambda: ["LYRICS", "WORSHIP"])
    speaking_layout_keywords: List[str] = field(default_factory=lambda: ["TEACH", "PREACH", "LIVE"])
    # Preferred input roles per inferred audio mode (used to bias candidate selection).
    # Defaults match existing hardcoded behavior in DirectorCore._choose_candidate.
    band_preferred_roles: List[str] = field(
        default_factory=lambda: ["roamer", "ptz", "fixed_1", "fixed_2"]
    )
    speaking_preferred_roles: List[str] = field(
        default_factory=lambda: ["sermon_hero", "sermon_ptz", "sermon_roamer", "ptz", "roamer"]
    )


@dataclass
class SermonBumperConfig:
    """
    Configuration for detecting an embedded sermon bumper video inside the Sermon playlist item.
    """
    # Global on/off switch for sermon bumper detection.
    enabled: bool = True
    # Phase ids to use when bumper is active / when sermon is active.
    bumper_phase_id: str = "BumperSermon"
    sermon_phase_id: str = "Sermon"
    # Layout keywords that imply a bumper/video layout when slide_type is not yet known.
    # Defaults match existing behavior: stage display layouts containing \"VIDEO\" or \"BUMPER\".
    layout_keywords: List[str] = field(default_factory=lambda: ["VIDEO", "BUMPER"])
    # Factor applied to audio_bias.band_threshold when requiring ProPresenter audio to be
    # \"hot enough\" to treat the current slide as an active bumper.
    # Default 0.6 matches the prior hardcoded band_threshold * 0.6 heuristic.
    audio_min_factor: float = 0.6
    # Hysteresis window for bumper activation/deactivation (seconds).
    # Default 0.6 matches the prior hardcoded hysteresis window in DirectorCore.tick.
    hysteresis_seconds: float = 0.6


@dataclass
class RoamerConfig:
    """Roamer input and stability detection."""
    atem_input_id: int = 0  # 0 = roamer not in use
    stability_window_seconds: float = 1.0
    motion_threshold: float = 10.0  # Frame-diff or Laplacian variance threshold

    @property
    def enabled(self) -> bool:
        return self.atem_input_id > 0


@dataclass
class RunSheetConfig:
    """Optional time-based phase fallback (e.g. start time + phase durations)."""
    start_time_iso: Optional[str] = None  # e.g. "2025-02-25T10:00:00"
    phase_durations_seconds: Optional[List[float]] = None  # One per phase in phase order


@dataclass
class DirectorConfig:
    """Top-level director configuration."""
    atem: ATEMConfig = field(default_factory=ATEMConfig)
    capture: CaptureConfig = field(default_factory=CaptureConfig)
    input_roles: InputRolesConfig = field(default_factory=InputRolesConfig)
    phases: PhaseConfig = field(default_factory=PhaseConfig)
    # ProPresenter playlist item name (or index as string) -> phase id
    playlist_item_to_phase: Dict[str, str] = field(default_factory=dict)
    # Optional fallback phase id used when a ProPresenter playlist item is not
    # mapped in playlist_item_to_phase or via automatic song → band-phase mapping.
    unmapped_playlist_item_fallback_phase: Optional[str] = None
    # Phase lock: phase_id -> input role name; director stays on that role's input only (e.g. Intro/BumperIn/Outro -> cg)
    phases_locked_to_role: Dict[str, str] = field(default_factory=dict)
    propresenter: ProPresenterConfig = field(default_factory=ProPresenterConfig)
    x32: Optional[X32Config] = None
    ptz: Optional[PTZConfig] = None
    pacing: PacingConfig = field(default_factory=PacingConfig)
    # Optional audio-driven bias between band vs speaking dominance.
    audio_bias: AudioBiasConfig = field(default_factory=AudioBiasConfig)
    # Optional sermon bumper detection config (embedded bumper video inside Sermon item).
    sermon_bumper: SermonBumperConfig = field(default_factory=SermonBumperConfig)
    transition_duration: float = 0.25  # 0.25 or 0.5 seconds (fade/mix)
    backup_input_id: int = 1  # Last-resort input (e.g. CG)
    backup_timeout_seconds: float = 10.0
    # Panic: configured safe shot for emergency cut/lock.
    panic_safe_input_id: int = 1
    panic_label: Optional[str] = None
    # Lock-to-input emergency mode: default timeout before auto-clear (seconds).
    # 0 or negative means \"no timeout\".
    lock_to_input_timeout_seconds: float = 30.0
    roamer: RoamerConfig = field(default_factory=RoamerConfig)
    loop_rate_hz: float = 10.0  # 5–15 typical
    run_sheet: Optional[RunSheetConfig] = None
    # Dwell: require "best" input to be stable this long before cutting (seconds)
    dwell_seconds: float = 0.75
    # Minimum detector confidence threshold (0–1) for considering a detection valid.
    detector_confidence_threshold: float = 0.5
    # Optional list of dotted paths to candidate plugins (see rules_plugins.load_candidate_plugins).
    rules_plugins: List[str] = field(default_factory=list)
    # Optional log level for root logger (e.g. INFO, DEBUG); applied at startup.
    log_level: str = "INFO"
    # Maximum number of recent decision-log entries to retain in memory/UI.
    decision_log_max_entries: int = 100


def _coerce_input_roles(raw: Any) -> InputRolesConfig:
    if isinstance(raw, InputRolesConfig):
        return raw
    by_input: Dict[int, str] = {}
    if isinstance(raw, dict):
        for k, v in raw.items():
            try:
                idx = int(k)
                if isinstance(v, str) and v in INPUT_ROLE_NAMES:
                    by_input[idx] = v
            except (ValueError, TypeError):
                continue
    return InputRolesConfig(by_input=by_input)


def _coerce_phases(raw: Any) -> PhaseConfig:
    if isinstance(raw, PhaseConfig):
        return raw
    phase_ids = list(PHASE_IDS)
    labels: Dict[str, str] = {}
    if isinstance(raw, dict):
        phase_ids = raw.get("phase_ids", phase_ids)
        labels = raw.get("labels", labels) or {}
    if isinstance(phase_ids, list):
        phase_ids = [str(p) for p in phase_ids]
    else:
        phase_ids = list(PHASE_IDS)
    return PhaseConfig(phase_ids=phase_ids, labels=labels)


def validate_config(cfg: DirectorConfig) -> List[str]:
    """Validate config; return list of error messages (empty if valid)."""
    errors: List[str] = []
    if not cfg.atem.ip or not cfg.atem.ip.strip():
        errors.append("atem.ip is required")
    if cfg.transition_duration not in (0.25, 0.5):
        errors.append("transition_duration must be 0.25 or 0.5")
    if cfg.backup_input_id < 1:
        errors.append("backup_input_id must be >= 1")
    if cfg.loop_rate_hz < 1 or cfg.loop_rate_hz > 30:
        errors.append("loop_rate_hz should be between 1 and 30")
    if cfg.pacing.min_seconds_on_shot < 0 or cfg.pacing.max_seconds_on_shot < cfg.pacing.min_seconds_on_shot:
        errors.append("pacing: invalid min/max seconds on shot")
    if cfg.roamer.enabled and cfg.roamer.stability_window_seconds <= 0:
        errors.append("roamer.stability_window_seconds must be positive")
    fallback_phase = getattr(cfg, "unmapped_playlist_item_fallback_phase", None)
    if fallback_phase:
        if fallback_phase not in cfg.phases.phase_ids:
            errors.append(
                f"unmapped_playlist_item_fallback_phase must be one of phases.phase_ids (got {fallback_phase!r})"
            )
    if cfg.log_level.upper() not in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
        errors.append(f"log_level must be one of DEBUG, INFO, WARNING, ERROR, CRITICAL (got {cfg.log_level!r})")
    if cfg.panic_safe_input_id < 1:
        errors.append("panic_safe_input_id must be >= 1")
    if cfg.lock_to_input_timeout_seconds < 0:
        errors.append("lock_to_input_timeout_seconds must be >= 0")
    if cfg.decision_log_max_entries <= 0:
        errors.append("decision_log_max_entries must be >= 1")
    # Audio bias sanity checks (non-fatal if misconfigured, but help catch obvious issues).
    if cfg.audio_bias.band_threshold < 0 or cfg.audio_bias.speaking_threshold < 0:
        errors.append("audio_bias.band_threshold and audio_bias.speaking_threshold must be >= 0")
    if cfg.audio_bias.hysteresis_seconds < 0:
        errors.append("audio_bias.hysteresis_seconds must be >= 0")
    return errors
