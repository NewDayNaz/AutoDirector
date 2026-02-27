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
        # Wire phase change -> PTZ recall (except in rehearsal)
        self._phase_machine.on_phase_changed = self._on_phase_changed

    def _update_decision(self, **kwargs: Any) -> None:
        """Merge kwargs into _decision_state for UI (only scalar/list/dict values)."""
        for k, v in kwargs.items():
            if v is None or isinstance(v, (bool, int, float, str, list, dict)):
                self._decision_state[k] = v

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
        # 1) Update phase
        pp_phase = self._pp.get_current_phase() if self._pp else None
        if self._pp:
            self._pp.poll()
        self._phase_machine.update(propresenter_phase=pp_phase)
        phase = self._phase_machine.current_phase

        # 2) Reconnect ATEM (reconnect with backoff is inside atem.connect())
        self._atem.connect()

        # 3) PTZ on phase change is handled by _on_phase_changed

        # 4) Gather signals
        segments: List[Tuple[int, Any]] = []
        if self.ingest:
            segments = self.ingest.get_segments()
        inputs_with_people: Set[int] = set()
        all_results: List[Dict] = []
        if segments and self.detector:
            res = self.detector.process_segments(segments, confidence_threshold=0.5)
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
        pp_level = self._x32.get_propresenter_level() if self._x32 else 0.0
        program_input = self._atem.get_program_input()
        roles = cfg.input_roles
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
                        phase, safe_segments, inputs_with_people, roamer_stable, band_muted, pp_level, program_input,
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
                        phase, segments, inputs_with_people, roamer_stable, band_muted, pp_level, program_input,
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
                    pp_item_name=self._pp.get_current_item_name() if self._pp else None,
                    segment_input_ids=segment_ids, inputs_with_people=list(inputs_with_people),
                    roamer_stable=roamer_stable, band_muted=band_muted, pp_level=pp_level, program_input=program_input,
                    program_ok=False, program_bad_reason=reason, candidate=target, eligible=eligible,
                    block_reason="recovery", cut_performed=(self._run_mode == RUN_MODE_RUNNING and can_cut_now),
                    recovery_safe_why=safe_why, recovery_safe_inputs=safe_inputs, recovery_target_reason=target_reason,
                )
                if self._run_mode == RUN_MODE_RUNNING and can_cut_now:
                    if self._atem.cut_to_input(target, cfg.transition_duration):
                        self._last_cut_time = now
                        self._last_cut_input = target
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
            phase, segments, inputs_with_people, roamer_stable, band_muted, pp_level, program_input,
        )
        self._update_decision(
            phase=phase,
            pp_phase=pp_phase,
            pp_item_name=self._pp.get_current_item_name() if self._pp else None,
            segment_input_ids=[i for i, _ in segments],
            inputs_with_people=list(inputs_with_people),
            roamer_stable=roamer_stable,
            band_muted=band_muted,
            pp_level=pp_level,
            program_input=program_input,
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
                        logger.info("Backup: no candidate for %.0fs, cut to input %s", cfg.backup_timeout_seconds, target)
                        return target
            return None
        self._backup_timer.mark_good()

        # Pacing: phases locked to a role (e.g. Intro/BumperIn/Outro -> cg) bypass dwell/min-time
        # and only block when we're already on the locked input (use last_cut_input for reliability).
        locked_role = cfg.phases_locked_to_role.get(phase)
        if locked_role:
            if self._last_cut_input == candidate:
                self._update_decision(block_reason="same_input")
                return None
        else:
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
            self._update_decision(block_reason=None, cut_performed=True)
            logger.info("Cut to input %s (phase=%s)", candidate, phase)
            return candidate
        self._update_decision(block_reason="atem_cut_failed")
        return None

    def _min_seconds_on_shot(self, phase: str) -> float:
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
        for input_id, _ in segments:
            if self._is_recently_bad(input_id):
                why_skipped[input_id] = "recently_bad"
                continue
            role = roles.role_for_input(input_id)
            if not role:
                why_skipped[input_id] = "no_role"
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
        # Default: first candidate with person, else first eligible (order = segment order)
        chosen = candidates[0] if candidates else (eligible[0] if eligible else None)
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
