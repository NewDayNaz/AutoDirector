"""
Director core: main loop that drives the ATEM from phase, CV, and external signals.

Each tick: update phase, reconnect adapters, on phase change recall PTZ, gather signals,
recovery (bad program), apply phase rules + pacing, execute transition or backup.
Run modes: running | paused | rehearsal | manual | stopped.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional, Set, Tuple

try:
    from config.load import load_config
    from config.schema import DirectorConfig
except ImportError:
    from .config.load import load_config
    from .config.schema import DirectorConfig

try:
    from rules_plugins import load_candidate_plugins
except ImportError:
    from .rules_plugins import load_candidate_plugins

try:
    from atem_control import ATEMController
    from phase_machine import PhaseMachine
    from roamer_stability import RoamerStability
    from safety import BackupTimer, is_black_or_frozen
except ImportError:
    from .atem_control import ATEMController
    from .phase_machine import PhaseMachine
    from .roamer_stability import RoamerStability
    from .safety import BackupTimer, is_black_or_frozen

logger = logging.getLogger(__name__)


# Run modes
RUN_MODE_RUNNING = "running"
RUN_MODE_PAUSED = "paused"
RUN_MODE_REHEARSAL = "rehearsal"
RUN_MODE_MANUAL = "manual"
RUN_MODE_STOPPED = "stopped"


class DirectorCore:
    """
    Main director loop. Initialize with config (and optional ingest/detector/adapters),
    then call run() or tick() from an external loop.
    """

    def __init__(
        self,
        config: DirectorConfig,
        ingest=None,
        detector=None,
        atem_controller: Optional[ATEMController] = None,
        phase_machine: Optional[PhaseMachine] = None,
        x32_adapter=None,
        propresenter_adapter=None,
        ptz_adapter=None,
    ):
        self.config = config
        self.ingest = ingest
        self.detector = detector
        self._atem = atem_controller or ATEMController(
            ip=config.atem.ip,
            transition_duration_sec=config.transition_duration,
            use_preview=config.atem.use_preview,
        )
        self._phase_machine = phase_machine or PhaseMachine(
            phase_ids=config.phases.phase_ids,
            default_phase=config.phases.phase_ids[0] if config.phases.phase_ids else "Intro",
        )
        self._x32 = x32_adapter
        self._pp = propresenter_adapter
        self._ptz = ptz_adapter
        self._roamer_stability: Optional[RoamerStability] = None
        if config.roamer.enabled:
            self._roamer_stability = RoamerStability(
                stability_window_seconds=config.roamer.stability_window_seconds,
                motion_threshold=config.roamer.motion_threshold,
                fps=config.loop_rate_hz,
            )
        self._backup_timer = BackupTimer(config.backup_timeout_seconds)
        self._run_mode = RUN_MODE_STOPPED
        self._last_cut_time = 0.0
        self._last_cut_input: Optional[int] = None
        self._dwell_start: Optional[float] = None
        self._dwell_target_input: Optional[int] = None
        self._program_frame_for_safety: Optional[Any] = None
        self._prev_program_frame: Optional[Any] = None
        # Decision state for UI: signals and outcome of last tick (updated each tick())
        self._decision_state: Dict[str, Any] = {}
        # Inputs recently detected as "bad" while on program (for recovery suppression)
        self._recent_bad_inputs: Dict[int, float] = {}
        # Degraded modes: populated each tick based on missing/unhealthy dependencies.
        # Examples: "no_ingest", "no_detector", "no_propresenter", "no_x32", "x32_unhealthy", "atem_disconnected".
        self._degraded_modes: Set[str] = set()
        # Lightweight metrics for observability (exposed via web API).
        self._metrics: Dict[str, Any] = {
            "cut_count": 0,
            "backup_cut_count": 0,
            "bad_program_events": 0,
        }
        # Last chosen candidate per phase (for optional rotation rules).
        self._last_phase_candidate: Dict[str, int] = {}
        # Optional candidate plugins loaded from config.rules_plugins.
        self._candidate_plugins = load_candidate_plugins(getattr(config, "rules_plugins", []) or [])
        # Rolling log of recent decisions for debug/UX (each entry is a shallow dict snapshot).
        self._decision_log: List[Dict[str, Any]] = []
        self._decision_log_max = getattr(config, "decision_log_max_entries", 100)
        # Lock-to-input emergency mode: keep program on a specific input for a window.
        self._lock_input_id: Optional[int] = None
        self._lock_expires_at: Optional[float] = None
        # Audio-driven band/speaking bias (with hysteresis) derived from X32/ProPresenter.
        self._audio_mode: Optional[str] = None
        self._audio_mode_candidate: Optional[str] = None
        self._audio_mode_candidate_since: float = 0.0
        # Sermon bumper detection (embedded bumper video inside Sermon playlist item).
        self._sermon_bumper_active: bool = False
        self._sermon_bumper_candidate: Optional[bool] = None
        self._sermon_bumper_candidate_since: float = 0.0
        self._sermon_bumper_slide_index: Optional[int] = None
        self._sermon_bumper_finished_once: bool = False
        # Hint to cut off CG immediately when leaving BumperSermon into Sermon.
        self._just_exited_bumpersermon: bool = False
        # Wire phase change -> PTZ recall (except in rehearsal)
        self._phase_machine.on_phase_changed = self._on_phase_changed
        # Optional: record/replay state capture (called each tick with state snapshot).
        self._state_capture_callback: Optional[Any] = None
        # Replay mode: when set, use this phase instead of updating from ProPresenter/X32.
        self._replay_phase_override: Optional[str] = None

    def _update_decision(self, **kwargs: Any) -> None:
        """Merge kwargs into _decision_state for UI (only scalar/list/dict values)."""
        changed: Dict[str, Any] = {}
        for k, v in kwargs.items():
            if v is None or isinstance(v, (bool, int, float, str, list, dict)):
                self._decision_state[k] = v
                changed[k] = v
        if changed:
            # Append a lightweight snapshot into the decision log for later inspection.
            entry = dict(self._decision_state)
            entry.setdefault("timestamp_monotonic", time.monotonic())
            self._decision_log.append(entry)
            if len(self._decision_log) > self._decision_log_max:
                # Drop oldest entries to keep memory bounded.
                self._decision_log = self._decision_log[-self._decision_log_max :]

    def _set_degraded_modes(self) -> None:
        """
        Compute degraded modes for this tick based on adapter health and ingest/detector presence.
        This is intentionally simple and conservative: it never blocks decisions, only annotates state.
        """
        modes: Set[str] = set()
        if not self.ingest:
            modes.add("no_ingest")
        if self.detector is None:
            modes.add("no_detector")
        if not self._pp:
            modes.add("no_propresenter")
        elif not getattr(self._pp, "is_connected", False):
            modes.add("propresenter_disconnected")
        if self._x32 is None:
            modes.add("no_x32")
        elif not getattr(self._x32, "has_recent_response", False):
            modes.add("x32_unhealthy")
        if self._atem is None:
            modes.add("no_atem")
        elif not getattr(self._atem, "is_connected", False):
            modes.add("atem_disconnected")
        self._degraded_modes = modes

    def get_degraded_modes(self) -> List[str]:
        """Return a stable, sorted list of current degraded modes for UI and health endpoints."""
        return sorted(self._degraded_modes)

    def get_metrics(self) -> Dict[str, Any]:
        """
        Snapshot of director metrics for observability.
        Returned dict is safe for JSON (scalars only); callers get a copy.
        """
        out = dict(self._metrics)
        out["loop_rate_hz"] = self.config.loop_rate_hz
        return out

    def set_state_capture_callback(self, callback: Optional[Any]) -> None:
        """Set a callback invoked each tick with a state snapshot for record/replay. Signature: (state: dict) -> None."""
        self._state_capture_callback = callback

    def set_replay_phase_override(self, phase_id: Optional[str]) -> None:
        """In replay mode, use this phase for the next tick instead of updating from adapters."""
        self._replay_phase_override = phase_id

    def get_decision_log(self) -> List[Dict[str, Any]]:
        """
        Return a copy of the recent decision log (bounded length).
        Each entry is safe for JSON serialization.
        """
        now = time.monotonic()
        out: List[Dict[str, Any]] = []
        for entry in self._decision_log:
            e = dict(entry)
            ts = e.get("timestamp_monotonic")
            if isinstance(ts, (int, float)):
                e["age_sec"] = max(0.0, now - float(ts))
            out.append(e)
        return out

    # --- Emergency controls -------------------------------------------------

    def _safe_panic_input_id(self) -> int:
        """Return the configured panic-safe input id (fall back to backup_input_id)."""
        safe_id = getattr(self.config, "panic_safe_input_id", None)
        try:
            safe_id_int = int(safe_id) if safe_id is not None else int(self.config.backup_input_id)
        except (TypeError, ValueError):
            safe_id_int = int(self.config.backup_input_id)
        return max(1, safe_id_int)

    def set_lock_input(self, input_id: Optional[int], timeout_seconds: Optional[float] = None) -> None:
        """
        Lock-to-input emergency mode.

        When input_id is set, the director will prefer to cut to and remain on that input
        (subject to run_mode and global rate limiting) until the lock expires or is cleared.
        """
        if input_id is None:
            self._lock_input_id = None
            self._lock_expires_at = None
            return
        try:
            input_int = int(input_id)
        except (TypeError, ValueError):
            return
        if input_int <= 0:
            return
        self._lock_input_id = input_int
        cfg_timeout = getattr(self.config, "lock_to_input_timeout_seconds", 30.0)
        ttl = timeout_seconds if timeout_seconds is not None else cfg_timeout
        if ttl is not None and ttl > 0:
            self._lock_expires_at = time.monotonic() + float(ttl)
        else:
            self._lock_expires_at = None

    def get_lock_state(self) -> Dict[str, Any]:
        """Return current lock-to-input state for UI/API."""
        now = time.monotonic()
        remaining: Optional[float] = None
        if self._lock_expires_at is not None:
            remaining = max(0.0, self._lock_expires_at - now)
        return {
            "locked_input": self._lock_input_id,
            "seconds_remaining": remaining,
        }

    def panic(self) -> Optional[int]:
        """
        Emergency: cut immediately to the configured safe input and lock to it.

        Returns the input id we cut to (or None if cut not performed).
        """
        target = self._safe_panic_input_id()
        self.set_lock_input(target, timeout_seconds=0.0)
        now = time.monotonic()
        can_cut_now = self._can_cut_now(now)
        phase = self._phase_machine.current_phase
        program_input = self._atem.get_program_input() if self._atem else None
        if self._run_mode != RUN_MODE_RUNNING or not self._atem:
            self._update_decision(
                phase=phase,
                program_input=program_input,
                candidate=target,
                locked_input=target,
                lock_seconds_remaining=None,
                block_reason="panic_run_mode",
                cut_performed=False,
                panic=True,
            )
            return None
        if not can_cut_now:
            self._update_decision(
                phase=phase,
                program_input=program_input,
                candidate=target,
                locked_input=target,
                lock_seconds_remaining=None,
                block_reason="panic_rate_limit",
                cut_performed=False,
                panic=True,
            )
            return None
        if self._atem.cut_to_input(target, self.config.transition_duration):
            self._last_cut_time = now
            self._last_cut_input = target
            self._dwell_start = None
            self._dwell_target_input = None
            try:
                self._metrics["cut_count"] = int(self._metrics.get("cut_count", 0)) + 1
            except Exception:
                self._metrics["cut_count"] = 1
            self._update_decision(
                phase=phase,
                program_input=program_input,
                candidate=target,
                locked_input=target,
                lock_seconds_remaining=None,
                block_reason="panic",
                cut_performed=True,
                panic=True,
            )
            logger.warning("Panic: cut to safe input %s", target)
            return target
        self._update_decision(
            phase=phase,
            program_input=program_input,
            candidate=target,
            locked_input=target,
            lock_seconds_remaining=None,
            block_reason="panic_atem_cut_failed",
            cut_performed=False,
            panic=True,
        )
        return None

    def _on_phase_changed(self, previous: str, current: str):
        if self._run_mode == RUN_MODE_REHEARSAL:
            return
        if self._ptz and self._ptz.enabled:
            preset = self._ptz.get_preset_for_phase(current)
            if preset:
                self._ptz.recall_preset(preset)

    def _mark_input_bad(self, input_id: Optional[int]) -> None:
        """Remember that an input was recently bad while on program."""
        if input_id is None:
            return
        try:
            if int(input_id) <= 0:
                return
        except (TypeError, ValueError):
            return
        self._recent_bad_inputs[int(input_id)] = time.monotonic()

    def _is_recently_bad(self, input_id: int) -> bool:
        """
        True if this input was recently detected as bad while on program.
        Entries expire automatically after a short window so inputs can recover.
        """
        ts = self._recent_bad_inputs.get(input_id)
        if ts is None:
            return False
        # Allow recovery after roughly half of backup_timeout_seconds, minimum 2s.
        ttl = max(2.0, self.config.backup_timeout_seconds / 2.0)
        now = time.monotonic()
        if now - ts > ttl:
            # Expired; drop from cache.
            del self._recent_bad_inputs[input_id]
            return False
        return True

    def _can_cut_now(self, now: Optional[float] = None) -> bool:
        """
        Global rate limiter for cuts/fades: allow at most one transition
        within the configured dwell_seconds window.
        """
        if now is None:
            now = time.monotonic()
        if self._last_cut_time <= 0:
            return True
        return (now - self._last_cut_time) >= self.config.dwell_seconds

    def set_run_mode(self, mode: str):
        if mode in (RUN_MODE_RUNNING, RUN_MODE_PAUSED, RUN_MODE_REHEARSAL, RUN_MODE_MANUAL, RUN_MODE_STOPPED):
            self._run_mode = mode

    def get_run_mode(self) -> str:
        return self._run_mode

    def force_phase(self, phase_id: Optional[str]) -> None:
        """Set or clear manual phase override."""
        self._phase_machine.set_manual_override(phase_id)

    def force_transition(self, input_id: int) -> bool:
        """Immediately cut to the given ATEM input (same fade as config). Returns True on success."""
        return bool(self._atem and self._atem.cut_to_input(input_id, self.config.transition_duration))

    def get_state(self) -> Dict[str, Any]:
        """Current phase, program, last cut, run mode, connection status, and decision state."""
        program = self._atem.get_program_input() if self._atem else None
        out: Dict[str, Any] = {
            "run_mode": self._run_mode,
            "current_phase": self._phase_machine.current_phase,
            "program_input": program,
            "last_cut_time": self._last_cut_time,
            "last_cut_input": self._last_cut_input,
            "atem_connected": self._atem.is_connected if self._atem else False,
            "x32_has_response": self._x32.has_recent_response if self._x32 else False,
            "pp_connected": self._pp.is_connected if self._pp else False,
            "phases": self.config.phases.phase_ids,
        }
        if self._degraded_modes:
            out["degraded_modes"] = self.get_degraded_modes()
        if self._metrics:
            out["metrics"] = self.get_metrics()
        if self._decision_state:
            out["decision"] = dict(self._decision_state)
        return out

    def tick(self) -> Optional[int]:
        """
        One director tick: update phase, gather signals, maybe transition.
        Returns the input we cut to (if any), else None.
        """
        cfg = self.config
        now = time.monotonic()
        # Update degraded modes at the very start of the tick so the UI/control plane
        # can reflect missing or unhealthy dependencies even if we bail out early.
        self._set_degraded_modes()
        # Expire any active lock if its timeout has passed.
        if self._lock_expires_at is not None and now >= self._lock_expires_at:
            self._lock_input_id = None
            self._lock_expires_at = None
        # 1) Update phase
        previous_phase = self._phase_machine.current_phase
        pp_phase = self._pp.get_current_phase() if self._pp else None
        if self._pp:
            self._pp.poll()

        pp_item_name = (
            self._pp.get_current_item_name()
            if self._pp and hasattr(self._pp, "get_current_item_name")
            else None
        )
        pp_slide_type = (
            self._pp.get_slide_type()
            if self._pp and hasattr(self._pp, "get_slide_type")
            else None
        )
        pp_slide_index = (
            self._pp.get_slide_index()
            if self._pp and hasattr(self._pp, "get_slide_index")
            else None
        )
        stage_layout = (
            self._pp.get_stage_display_layout()
            if self._pp and hasattr(self._pp, "get_stage_display_layout")
            else None
        )
        pp_level = self._x32.get_propresenter_level() if self._x32 else 0.0

        pastor_muted: Optional[bool] = None
        if self._x32 and hasattr(self._x32, "is_pastor_muted"):
            try:
                pastor_muted = self._x32.is_pastor_muted()
            except Exception:
                pastor_muted = None

        # Replay mode: use recorded phase for this tick and skip phase machine update.
        if self._replay_phase_override is not None:
            phase = self._replay_phase_override
            self._phase_machine.set_current_phase(phase)
            self._replay_phase_override = None
        else:
            # Optional: treat pastor DCA unmute and sermon bumper as phase overrides.
            try:
                # Sermon bumper detection: embedded bumper video at start of Sermon playlist item.
                bumper_cfg = getattr(cfg, "sermon_bumper", None)
                bumper_enabled = bool(getattr(bumper_cfg, "enabled", True)) if bumper_cfg is not None else True
                bumper_phase_id = getattr(bumper_cfg, "bumper_phase_id", "BumperSermon") if bumper_cfg else "BumperSermon"
                sermon_phase_id = getattr(bumper_cfg, "sermon_phase_id", "Sermon") if bumper_cfg else "Sermon"
                bumper_supported = bumper_enabled and bumper_phase_id in cfg.phases.phase_ids
                bumper_candidate: bool = False
                if self._pp and bumper_supported:
                    name = (pp_item_name or "").strip()
                    slide_type = (pp_slide_type or "").strip().lower() if isinstance(pp_slide_type, str) else None
                    layout_upper = stage_layout.upper() if isinstance(stage_layout, str) else ""
                    # Treat playlist items mapped to Sermon (or configured sermon phase) as eligible for embedded bumper.
                    mapped_phase = cfg.playlist_item_to_phase.get(name)
                    is_sermon_item = name == sermon_phase_id or mapped_phase == sermon_phase_id
                    if is_sermon_item and not self._sermon_bumper_finished_once:
                        layout_keywords = [
                            str(k).upper()
                            for k in (getattr(bumper_cfg, "layout_keywords", None) or ["VIDEO", "BUMPER"])
                        ]
                        layout_videoish = any(k in layout_upper for k in layout_keywords)
                        # Prefer explicit video slide type from ProPresenter adapter; if we don't
                        # have it yet (slide_type is None), fall back to layout name once,
                        # before we've ever seen the bumper complete.
                        is_video_like = bool(slide_type == "video" or (slide_type is None and layout_videoish))
                        if is_video_like:
                            audio_cfg = getattr(cfg, "audio_bias", None)
                            level = float(pp_level or 0.0)
                            band_threshold = float(getattr(audio_cfg, "band_threshold", 0.3)) if audio_cfg else 0.3
                            audio_factor = float(getattr(bumper_cfg, "audio_min_factor", 0.6)) if bumper_cfg else 0.6
                            # Require ProPresenter audio to be clearly present to treat the slide as an active bumper.
                            audio_hot = level >= max(0.0, band_threshold * audio_factor)
                            bumper_candidate = bool(audio_hot)

                # If bumper was active and we've advanced to a different slide index,
                # treat the bumper as finished immediately, regardless of audio/layout.
                if (
                    self._sermon_bumper_active
                    and self._sermon_bumper_slide_index is not None
                    and pp_slide_index is not None
                    and pp_slide_index != self._sermon_bumper_slide_index
                ):
                    if self._sermon_bumper_active:
                        self._sermon_bumper_active = False
                        self._sermon_bumper_candidate = None
                        self._sermon_bumper_finished_once = True
                        self._sermon_bumper_slide_index = None
                        logger.info(
                            "Sermon bumper active -> False (slide index advanced: %s -> %s)",
                            self._sermon_bumper_slide_index,
                            pp_slide_index,
                        )
                    bumper_candidate = False

                # Hysteresis for sermon bumper activation to avoid rapid toggling.
                if bumper_candidate == self._sermon_bumper_active:
                    self._sermon_bumper_candidate = None
                else:
                    if bumper_candidate != self._sermon_bumper_candidate:
                        self._sermon_bumper_candidate = bumper_candidate
                        self._sermon_bumper_candidate_since = now
                    hysteresis_sec = float(getattr(bumper_cfg, "hysteresis_seconds", 0.6)) if bumper_cfg else 0.6
                    if (
                        self._sermon_bumper_candidate is not None
                        and (now - self._sermon_bumper_candidate_since) >= hysteresis_sec
                    ):
                        self._sermon_bumper_active = self._sermon_bumper_candidate
                        self._sermon_bumper_candidate = None
                        if self._sermon_bumper_active:
                            self._sermon_bumper_slide_index = pp_slide_index
                        else:
                            self._sermon_bumper_slide_index = None
                        logger.info(
                            "Sermon bumper active -> %s (item=%r slide_type=%r layout=%r level=%.3f)",
                            self._sermon_bumper_active,
                            pp_item_name,
                            pp_slide_type,
                            stage_layout,
                            pp_level,
                        )

                # Manual override remains highest precedence; external override is advisory.
                external_phase = None
                external_reason = None
                if self._sermon_bumper_active and bumper_supported:
                    external_phase = bumper_phase_id
                    external_reason = "sermon_bumper_video"
                elif pastor_muted is False and sermon_phase_id in cfg.phases.phase_ids:
                    external_phase = sermon_phase_id
                    external_reason = "pastor_dca_unmuted"
                else:
                    external_phase = None
                    external_reason = None
                self._phase_machine.set_external_override(external_phase, reason=external_reason)  # type: ignore[attr-defined]
            except AttributeError:
                pass

            self._phase_machine.update(propresenter_phase=pp_phase)
            phase = self._phase_machine.current_phase

        # Remember when we have just transitioned from bumper_phase_id to sermon_phase_id so we
        # can immediately cut away from CG to a safe sermon camera.
        bumper_cfg_for_transition = getattr(self.config, "sermon_bumper", None)
        bumper_phase_for_transition = (
            getattr(bumper_cfg_for_transition, "bumper_phase_id", "BumperSermon")
            if bumper_cfg_for_transition
            else "BumperSermon"
        )
        sermon_phase_for_transition = (
            getattr(bumper_cfg_for_transition, "sermon_phase_id", "Sermon")
            if bumper_cfg_for_transition
            else "Sermon"
        )
        if previous_phase == bumper_phase_for_transition and phase == sermon_phase_for_transition:
            self._just_exited_bumpersermon = True
        elif phase != sermon_phase_for_transition:
            self._just_exited_bumpersermon = False

        # 2) Reconnect ATEM (reconnect with backoff is inside atem.connect())
        self._atem.connect()
        # Recompute ATEM-related degraded mode after attempting connect.
        self._set_degraded_modes()

        # 3) PTZ on phase change is handled by _on_phase_changed

        # 4) Gather signals
        segments: List[Tuple[int, Any]] = []
        if self.ingest:
            segments = self.ingest.get_segments()
        inputs_with_people: Set[int] = set()
        all_results: List[Dict] = []
        if segments and self.detector:
            res = self.detector.process_segments(
                segments, confidence_threshold=getattr(cfg, "detector_confidence_threshold", 0.5)
            )
            inputs_with_people = set(res.get("inputs_with_people", []))
            all_results = res.get("all_results", [])
        roamer_stable = False
        if self._roamer_stability and cfg.roamer.enabled:
            for input_id, crop in segments:
                if input_id == cfg.roamer.atem_input_id:
                    self._roamer_stability.push(crop)
                    roamer_stable = self._roamer_stability.is_stable()
                    break
        band_muted = self._x32.is_band_muted() if self._x32 else None
        program_input = self._atem.get_program_input()
        roles = cfg.input_roles

        # State capture for record/replay: snapshot of inputs used this tick.
        if self._state_capture_callback:
            try:
                self._state_capture_callback({
                    "phase": phase,
                    "pp_phase": pp_phase,
                    "pp_item_name": pp_item_name,
                    "pp_slide_type": pp_slide_type,
                    "pp_slide_index": pp_slide_index,
                    "stage_layout": stage_layout,
                    "pp_level": pp_level,
                    "pastor_muted": pastor_muted,
                    "band_muted": band_muted,
                    "program_input": program_input,
                    "inputs_with_people": inputs_with_people,
                    "roamer_stable": roamer_stable,
                    "sermon_bumper_active": self._sermon_bumper_active,
                    "external_phase_override": getattr(
                        self._phase_machine, "get_external_override", lambda: None
                    )(),
                    "external_phase_reason": getattr(
                        self._phase_machine, "get_external_override_reason", lambda: None
                    )(),
                })
            except Exception as e:
                logger.debug("State capture callback failed: %s", e)

        # Audio-mode inference (band / speaking / neutral) with basic hysteresis.
        raw_audio_mode: Optional[str] = None
        audio_mode: Optional[str] = None
        audio_cfg = getattr(cfg, "audio_bias", None)
        if audio_cfg and getattr(audio_cfg, "enabled", False):
            band_active = band_muted is False
            pastor_speaking = pastor_muted is False
            pp_loud_for_band = pp_level >= float(getattr(audio_cfg, "band_threshold", 0.3))
            layout_upper = stage_layout.upper() if isinstance(stage_layout, str) else ""
            band_keywords = [
                str(k).upper()
                for k in (getattr(audio_cfg, "band_layout_keywords", None) or ["LYRICS", "WORSHIP"])
            ]
            speaking_keywords = [
                str(k).upper()
                for k in (getattr(audio_cfg, "speaking_layout_keywords", None) or ["TEACH", "PREACH", "LIVE"])
            ]
            layout_band = bool(
                getattr(audio_cfg, "use_stage_layout", True)
                and any(k in layout_upper for k in band_keywords)
            )
            layout_speaking = bool(
                getattr(audio_cfg, "use_stage_layout", True)
                and any(k in layout_upper for k in speaking_keywords)
            )
            if pastor_speaking and not band_active:
                raw_audio_mode = "speaking"
            elif band_active and (pp_loud_for_band or layout_band):
                raw_audio_mode = "band"
            elif pastor_speaking or layout_speaking or pp_level >= float(
                getattr(audio_cfg, "speaking_threshold", 0.1)
            ):
                raw_audio_mode = "speaking"
            else:
                raw_audio_mode = "neutral"

            if raw_audio_mode == self._audio_mode:
                # Stable; clear any pending candidate.
                self._audio_mode_candidate = None
            else:
                if raw_audio_mode != self._audio_mode_candidate:
                    self._audio_mode_candidate = raw_audio_mode
                    self._audio_mode_candidate_since = now
                hysteresis = float(getattr(audio_cfg, "hysteresis_seconds", 1.5))
                if (
                    self._audio_mode_candidate is not None
                    and hysteresis >= 0.0
                    and (now - self._audio_mode_candidate_since) >= hysteresis
                ):
                    self._audio_mode = self._audio_mode_candidate
                    self._audio_mode_candidate = None
            audio_mode = self._audio_mode or raw_audio_mode
        # Treat a cut as "in transition" for at least transition_duration so we don't
        # immediately trigger another recovery while the switcher is still fading.
        transition_in_progress = (
            self._last_cut_time > 0
            and (now - self._last_cut_time) < cfg.transition_duration
        )

        # 5) Recovery: if current program is bad, cut away immediately (unless in transition).
        # Prefer a "safe" (non-black) recovery camera that is not the current program;
        # if none are found, fall back to the configured backup input.
        program_segment = None
        for input_id, crop in segments:
            if input_id == program_input:
                program_segment = crop
                break
        program_role = roles.role_for_input(program_input) if program_input is not None else None
        # Never treat CG program as "bad" via black/freeze detection; CG may intentionally
        # go to black or static and should not trigger emergency recovery.
        if program_segment is not None and not transition_in_progress and program_role != "cg":
            is_bad, reason = is_black_or_frozen(
                program_segment,
                prev_frame=self._prev_program_frame,
            )
            if is_bad:
                # Count bad-program events for observability/metrics.
                try:
                    self._metrics["bad_program_events"] = int(self._metrics.get("bad_program_events", 0)) + 1
                except Exception:
                    self._metrics["bad_program_events"] = 1
                # Remember that this input was bad while on program so we don't
                # immediately select it again as a "safe" recovery target.
                self._mark_input_bad(program_input)

                # Scan all segments to find safe (non-black) inputs other than the current
                # program and any that were recently detected as bad.
                segment_ids = [i for i, _ in segments]
                safe_inputs: List[int] = []
                # input_id -> "safe" | "black" | "recently_bad" | "program" | "cg"
                safe_why: Dict[int, str] = {}
                for input_id, crop in segments:
                    if input_id == program_input:
                        safe_why[input_id] = "program"
                        continue
                    role = roles.role_for_input(input_id)
                    # Never run black/freeze heuristics on CG; treat it as eligible-safe here
                    # and let phase rules decide when to use it.
                    if role == "cg":
                        safe_why[input_id] = "cg"
                        safe_inputs.append(input_id)
                        continue
                    if self._is_recently_bad(input_id):
                        safe_why[input_id] = "recently_bad"
                        continue
                    other_bad, bad_reason = is_black_or_frozen(crop)
                    if other_bad:
                        safe_why[input_id] = f"bad({bad_reason})"
                        continue
                    safe_why[input_id] = "safe"
                    safe_inputs.append(input_id)

                logger.debug(
                    "Recovery rubric: program=%s phase=%s segment_ids=%s safe_why=%s safe_inputs=%s",
                    program_input, phase, segment_ids, safe_why, safe_inputs,
                )

                if safe_inputs:
                    # Restrict candidate selection to safe inputs only.
                    safe_segments = [(i, c) for i, c in segments if i in safe_inputs]
                    next_best, eligible = self._choose_candidate(
                        phase,
                        safe_segments,
                        inputs_with_people,
                        roamer_stable,
                        band_muted,
                        pp_level,
                        audio_mode,
                        program_input,
                        _recovery_context="safe_only",
                    )
                    # Ensure we don't recover to a bad or unknown input; if candidate is not
                    # in the safe set, fall back to the first safe input.
                    if next_best not in safe_inputs:
                        next_best = safe_inputs[0]
                    target = next_best
                    target_reason = "first_safe" if next_best == safe_inputs[0] else "chosen_from_eligible"
                else:
                    # No safe inputs detected; fall back to existing candidate/backup logic.
                    next_best, eligible = self._choose_candidate(
                        phase,
                        segments,
                        inputs_with_people,
                        roamer_stable,
                        band_muted,
                        pp_level,
                        audio_mode,
                        program_input,
                        _recovery_context="fallback",
                    )
                    target = next_best or cfg.backup_input_id
                    target_reason = "backup_fallback" if not next_best else "chosen_from_all"

                logger.info(
                    "Recovery rubric: program=%s safe_inputs=%s eligible=%s target=%s reason=%s",
                    program_input, safe_inputs, eligible, target, target_reason,
                )

                can_cut_now = self._can_cut_now(now)

                self._update_decision(
                    phase=phase, pp_phase=pp_phase,
                    pp_item_name=pp_item_name,
                    segment_input_ids=segment_ids, inputs_with_people=list(inputs_with_people),
                    roamer_stable=roamer_stable,
                    band_muted=band_muted,
                    pp_level=pp_level,
                    program_input=program_input,
                    audio_mode=audio_mode,
                    raw_audio_mode=raw_audio_mode,
                    pp_slide_type=pp_slide_type,
                    sermon_bumper_active=self._sermon_bumper_active,
                    program_ok=False, program_bad_reason=reason, candidate=target, eligible=eligible,
                    block_reason="recovery", cut_performed=(self._run_mode == RUN_MODE_RUNNING and can_cut_now),
                    recovery_safe_why=safe_why, recovery_safe_inputs=safe_inputs, recovery_target_reason=target_reason,
                )
                if self._run_mode == RUN_MODE_RUNNING and can_cut_now:
                    if self._atem.cut_to_input(target, cfg.transition_duration):
                        self._last_cut_time = now
                        self._last_cut_input = target
                        try:
                            self._metrics["cut_count"] = int(self._metrics.get("cut_count", 0)) + 1
                        except Exception:
                            self._metrics["cut_count"] = 1
                        logger.warning("Recovery: program %s, cut to input %s", reason, target)
                        self._backup_timer.mark_good()
                        self._prev_program_frame = program_segment
                        return target
                self._prev_program_frame = program_segment
                return None
            self._backup_timer.mark_good()
        self._prev_program_frame = program_segment

        # 6) Phase rules + pacing: build candidates, dwell, min/max on shot
        candidate, eligible = self._choose_candidate(
            phase,
            segments,
            inputs_with_people,
            roamer_stable,
            band_muted,
            pp_level,
            audio_mode,
            program_input,
        )
        self._update_decision(
            phase=phase,
            pp_phase=pp_phase,
            pp_item_name=pp_item_name,
            pp_slide_index=pp_slide_index,
            segment_input_ids=[i for i, _ in segments],
            inputs_with_people=list(inputs_with_people),
            roamer_stable=roamer_stable,
            band_muted=band_muted,
            pp_level=pp_level,
            program_input=program_input,
            audio_mode=audio_mode,
            raw_audio_mode=raw_audio_mode,
            pp_slide_type=pp_slide_type,
            sermon_bumper_active=self._sermon_bumper_active,
            program_ok=True,
            program_bad_reason=None,
            candidate=candidate,
            eligible=eligible,
            min_seconds_on_shot=self._min_seconds_on_shot(phase),
            backup_seconds_since_good=self._backup_timer.seconds_since_good(),
            backup_timeout_seconds=cfg.backup_timeout_seconds,
            seconds_on_shot=(now - self._last_cut_time) if self._last_cut_time > 0 else 0.0,
            dwell_target_input=self._dwell_target_input,
            dwell_elapsed_sec=(now - self._dwell_start) if self._dwell_start is not None else None,
            dwell_required_sec=cfg.dwell_seconds,
            phase_source=getattr(self._phase_machine, "phase_source", None),
            manual_phase_override=self._phase_machine.get_manual_override(),
            external_phase_override=getattr(self._phase_machine, "get_external_override", lambda: None)(),
            external_phase_reason=getattr(self._phase_machine, "get_external_override_reason", lambda: None)(),
            degraded_modes=self.get_degraded_modes(),
            phase_rule=getattr(self.config.phases, "rule_for", lambda _p: None)(phase),
        )
        if candidate is None:
            self._update_decision(block_reason="no_candidate" if not self._backup_timer.should_trigger_backup() else "backup_triggered")
            if self._backup_timer.should_trigger_backup():
                # If this phase is locked to a role, use that role's input as backup
                locked_role = cfg.phases_locked_to_role.get(phase)
                if locked_role:
                    locked_inputs = self.config.input_roles.inputs_for_role(locked_role)
                    target = locked_inputs[0] if locked_inputs else cfg.backup_input_id
                else:
                    target = cfg.backup_input_id
                can_cut_now = self._can_cut_now(now)
                if self._run_mode == RUN_MODE_RUNNING and can_cut_now:
                    if self._atem.cut_to_input(target, cfg.transition_duration):
                        self._last_cut_time = now
                        self._last_cut_input = target
                        try:
                            self._metrics["cut_count"] = int(self._metrics.get("cut_count", 0)) + 1
                            self._metrics["backup_cut_count"] = int(self._metrics.get("backup_cut_count", 0)) + 1
                        except Exception:
                            self._metrics["cut_count"] = int(self._metrics.get("cut_count", 0)) + 1
                            self._metrics["backup_cut_count"] = int(self._metrics.get("backup_cut_count", 0)) + 1
                        logger.info("Backup: no candidate for %.0fs, cut to input %s", cfg.backup_timeout_seconds, target)
                        return target
            return None
        self._backup_timer.mark_good()

        # Lock-to-input emergency mode: override normal candidate selection and pacing.
        if self._lock_input_id is not None:
            lock_remaining: Optional[float] = None
            if self._lock_expires_at is not None:
                lock_remaining = max(0.0, self._lock_expires_at - now)
            target = self._lock_input_id
            if program_input == target:
                # Already on the locked input; hold shot.
                self._update_decision(
                    block_reason="lock_to_input",
                    locked_input=target,
                    lock_seconds_remaining=lock_remaining,
                )
                return None
            can_cut_now = self._can_cut_now(now)
            if self._run_mode != RUN_MODE_RUNNING or not can_cut_now:
                self._update_decision(
                    block_reason="lock_to_input_run_mode" if self._run_mode != RUN_MODE_RUNNING else "lock_to_input_rate_limit",
                    locked_input=target,
                    lock_seconds_remaining=lock_remaining,
                    cut_performed=False,
                )
                return None
            if self._atem.cut_to_input(target, cfg.transition_duration):
                self._last_cut_time = now
                self._last_cut_input = target
                self._dwell_start = None
                self._dwell_target_input = None
                try:
                    self._metrics["cut_count"] = int(self._metrics.get("cut_count", 0)) + 1
                except Exception:
                    self._metrics["cut_count"] = 1
                self._update_decision(
                    block_reason="lock_to_input",
                    locked_input=target,
                    lock_seconds_remaining=lock_remaining,
                    cut_performed=True,
                    candidate=target,
                )
                logger.warning("Lock-to-input: cut to input %s (phase=%s)", target, phase)
                return target
            self._update_decision(
                block_reason="lock_to_input_atem_cut_failed",
                locked_input=target,
                lock_seconds_remaining=lock_remaining,
                cut_performed=False,
            )
            return None

        # Pacing: phases locked to a role (e.g. Intro/BumperIn/Outro -> cg) bypass dwell/min-time
        # and only block when we're already on the locked input (use last_cut_input for reliability).
        locked_role = cfg.phases_locked_to_role.get(phase)
        if locked_role:
            if self._last_cut_input == candidate:
                self._update_decision(block_reason="same_input")
                return None
        else:
            # After leaving BumperSermon, allow a one-time immediate cut in Sermon
            # without dwell/min-time/same-input blocking so we get off CG quickly.
            bypass_pacing = phase == "Sermon" and self._just_exited_bumpersermon
            if not bypass_pacing:
                # Dwell: require candidate to be stable for dwell_seconds
                if candidate != self._dwell_target_input:
                    self._dwell_target_input = candidate
                    self._dwell_start = now
                if (self._dwell_start is None) or (now - self._dwell_start < cfg.dwell_seconds):
                    self._update_decision(
                        block_reason="dwell",
                        dwell_elapsed_sec=(now - self._dwell_start) if self._dwell_start else None,
                    )
                    return None
                # Min time on shot
                if self._last_cut_time > 0 and (now - self._last_cut_time) < self._min_seconds_on_shot(phase):
                    self._update_decision(block_reason="min_time_on_shot", seconds_on_shot=now - self._last_cut_time)
                    return None
                # Don't cut to same input
                if candidate == program_input:
                    self._update_decision(block_reason="same_input")
                    return None

        # 7) Execute transition (running) or log (rehearsal)
        # Clear the "just exited bumper" hint once we've made it through pacing;
        # even if we don't cut this tick due to run mode / rate limit, we don't
        # want to bypass pacing multiple times.
        if phase == "Sermon" and self._just_exited_bumpersermon:
            self._just_exited_bumpersermon = False
        if self._run_mode == RUN_MODE_MANUAL or self._run_mode == RUN_MODE_PAUSED or self._run_mode == RUN_MODE_STOPPED:
            self._update_decision(block_reason="run_mode")
            return None
        if self._run_mode == RUN_MODE_REHEARSAL:
            self._update_decision(block_reason="rehearsal")
            logger.info("Rehearsal: would have cut to input %s (phase=%s)", candidate, phase)
            return None
        if not self._can_cut_now(now):
            self._update_decision(block_reason="rate_limit", cut_performed=False)
            return None
        if self._atem.cut_to_input(candidate, cfg.transition_duration):
            self._last_cut_time = now
            self._last_cut_input = candidate
            self._dwell_start = None
            self._dwell_target_input = None
            try:
                self._metrics["cut_count"] = int(self._metrics.get("cut_count", 0)) + 1
            except Exception:
                self._metrics["cut_count"] = 1
            self._update_decision(block_reason=None, cut_performed=True)
            logger.info("Cut to input %s (phase=%s)", candidate, phase)
            return candidate
        self._update_decision(block_reason="atem_cut_failed")
        return None

    def _min_seconds_on_shot(self, phase: str) -> float:
        # Allow per-phase override when configured.
        phase_rule = getattr(self.config.phases, "rule_for", lambda _p: None)(phase)
        if phase_rule and getattr(phase_rule, "min_seconds_on_shot", None) is not None:
            return float(phase_rule.min_seconds_on_shot)  # type: ignore[arg-type]
        if phase == "Sermon":
            return self.config.pacing.sermon_min_seconds
        return self.config.pacing.min_seconds_on_shot

    def _max_seconds_on_shot(self, phase: str) -> float:
        if phase == "Sermon":
            return self.config.pacing.sermon_max_seconds
        return self.config.pacing.max_seconds_on_shot

    def _choose_candidate(
        self,
        phase: str,
        segments: List[Tuple[int, Any]],
        inputs_with_people: Set[int],
        roamer_stable: bool,
        band_muted: Optional[bool],
        pp_level: float,
        audio_mode: Optional[str],
        current_program: Optional[int],
        _recovery_context: Optional[str] = None,
    ) -> Tuple[Optional[int], List[int]]:
        """Build eligible candidates for this phase; return (best input or None, list of eligible input ids)."""
        cfg = self.config
        roles = cfg.input_roles
        segment_ids = [i for i, _ in segments]

        # Phases locked to a role: always choose that role's input (e.g. Intro/BumperIn/Outro -> cg).
        locked_role = cfg.phases_locked_to_role.get(phase)
        if locked_role:
            role_inputs = roles.inputs_for_role(locked_role)
            if role_inputs:
                logger.debug(
                    "Recovery _choose_candidate: phase=%s locked_role=%s role_inputs=%s -> %s",
                    phase, locked_role, role_inputs, role_inputs[0],
                )
                return role_inputs[0], list(role_inputs)

        eligible: List[int] = []
        why_skipped: Dict[int, str] = {}
        phase_rule = getattr(cfg.phases, "rule_for", lambda _p: None)(phase)
        allowed_roles: Optional[Set[str]] = None
        if phase_rule and getattr(phase_rule, "allowed_roles", None):
            allowed_roles = set(phase_rule.allowed_roles)  # type: ignore[arg-type]
        for input_id, _ in segments:
            if self._is_recently_bad(input_id):
                why_skipped[input_id] = "recently_bad"
                continue
            role = roles.role_for_input(input_id)
            if not role:
                why_skipped[input_id] = "no_role"
                continue
            if allowed_roles is not None and role not in allowed_roles:
                why_skipped[input_id] = "role_not_allowed"
                continue
            if role == "roamer" and cfg.roamer.enabled and input_id == cfg.roamer.atem_input_id:
                # For normal (non-recovery) cuts we require the roamer to be stable;
                # for recovery we relax this and rely only on \"safe\" checks (non-black).
                if not roamer_stable and not _recovery_context:
                    why_skipped[input_id] = "roamer_unstable"
                    continue
            eligible.append(input_id)
        if _recovery_context:
            logger.debug(
                "Recovery _choose_candidate: phase=%s segment_ids=%s why_skipped=%s eligible=%s",
                phase, segment_ids, why_skipped, eligible,
            )
        if not eligible:
            fallback = cfg.backup_input_id if cfg.backup_input_id else None
            logger.debug(
                "Recovery _choose_candidate: no eligible -> backup_input_id=%s",
                fallback,
            )
            return (fallback, [])
        # Prefer inputs with person
        with_person = [i for i in eligible if i in inputs_with_people]
        candidates = with_person if with_person else eligible
        if phase == "Sermon":
            sermon_inputs = [
                i for i in candidates
                if roles.role_for_input(i) in ("sermon_hero", "sermon_ptz", "sermon_roamer", "ptz", "roamer")
            ]
            if sermon_inputs:
                for i in sermon_inputs:
                    if i != current_program:
                        return (i, eligible)
                return (sermon_inputs[0], eligible)
        # Default: first candidate with person, else first eligible (order = segment order),
        # optionally biased by inferred audio_mode (band vs speaking).
        if audio_mode and candidates:
            preferred_roles: List[str] = []
            audio_cfg = getattr(cfg, "audio_bias", None)
            if audio_mode == "band":
                preferred_roles = list(
                    getattr(
                        audio_cfg,
                        "band_preferred_roles",
                        ["roamer", "ptz", "fixed_1", "fixed_2"],
                    )
                )
            elif audio_mode == "speaking":
                preferred_roles = list(
                    getattr(
                        audio_cfg,
                        "speaking_preferred_roles",
                        ["sermon_hero", "sermon_ptz", "sermon_roamer", "ptz", "roamer"],
                    )
                )

            if preferred_roles:
                def role_rank(input_id: int) -> int:
                    role = roles.role_for_input(input_id)
                    if role in preferred_roles:
                        return preferred_roles.index(role)  # type: ignore[arg-type]
                    return len(preferred_roles)

                # Stable sort by preferred role rank while preserving original ordering within rank.
                original_order = {i: idx for idx, i in enumerate(candidates)}
                candidates = sorted(
                    candidates,
                    key=lambda i: (role_rank(i), original_order.get(i, 0)),
                )

        chosen = candidates[0] if candidates else (eligible[0] if eligible else None)
        # Optional rotation within eligible inputs when phase rule requests it.
        if phase_rule and getattr(phase_rule, "rotate", False) and candidates:
            last = self._last_phase_candidate.get(phase)
            try:
                if last in candidates and len(candidates) > 1:
                    idx = candidates.index(last)
                    chosen = candidates[(idx + 1) % len(candidates)]
            except ValueError:
                pass
            if chosen is not None:
                self._last_phase_candidate[phase] = chosen
        # Apply optional candidate plugins (if any) last so they can see the full context.
        if self._candidate_plugins and eligible:
            context: Dict[str, Any] = {
                "inputs_with_people": list(inputs_with_people),
                "roamer_stable": roamer_stable,
                "band_muted": band_muted,
                "pp_level": pp_level,
                "audio_mode": audio_mode,
                "current_program": current_program,
                "recovery_context": _recovery_context,
                "why_skipped": why_skipped,
            }
            for plugin in self._candidate_plugins:
                try:
                    plugin_choice, plugin_eligible = plugin(phase, list(eligible), context)
                except Exception as e:
                    logger.warning("candidate plugin %s failed: %s", getattr(plugin, "__name__", plugin), e)
                    continue
                if plugin_eligible is not None:
                    eligible = list(plugin_eligible)
                if plugin_choice is not None:
                    chosen = plugin_choice
        logger.debug(
            "Recovery _choose_candidate: with_person=%s candidates=%s chosen=%s (first in list)",
            with_person, candidates, chosen,
        )
        return (chosen, eligible)

    def run(self):
        """Run the director loop until stopped. Call from main thread or script."""
        cfg = self.config
        interval = 1.0 / max(1.0, cfg.loop_rate_hz)
        self.set_run_mode(RUN_MODE_RUNNING)
        logger.info("Director running (phase=%s)", self._phase_machine.current_phase)
        try:
            while self._run_mode not in (RUN_MODE_STOPPED,):
                self.tick()
                time.sleep(interval)
        except KeyboardInterrupt:
            self.set_run_mode(RUN_MODE_STOPPED)
        finally:
            logger.info("Director stopped")
