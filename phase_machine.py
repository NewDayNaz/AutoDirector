"""
Service phase state machine for the auto-director.

Inputs: ProPresenter (playlist item → phase from config), optional time/run-sheet,
optional manual override. Output: current phase; invokes on_phase_changed when phase changes.
"""

from __future__ import annotations

import logging
from typing import Callable, List, Optional

try:
    from config.schema import PHASE_IDS
except ImportError:
    from .config.schema import PHASE_IDS

logger = logging.getLogger(__name__)


class PhaseMachine:
    """
    Resolves current phase from ProPresenter, time/run-sheet, or manual override.
    No switcher logic—only phase resolution. Call update() each tick.
    """

    def __init__(
        self,
        phase_ids: Optional[List[str]] = None,
        default_phase: str = "Intro",
        on_phase_changed: Optional[Callable[[str, str], None]] = None,
    ):
        self.phase_ids = phase_ids or list(PHASE_IDS)
        self.default_phase = default_phase if default_phase in self.phase_ids else self.phase_ids[0]
        self.on_phase_changed = on_phase_changed
        self._current_phase = self.default_phase
        self._manual_override: Optional[str] = None
        # External override is used for non-manual signals (e.g. pastor DCA).
        self._external_override: Optional[str] = None
        self._external_reason: Optional[str] = None
        self._run_sheet_start: Optional[float] = None  # time.monotonic() when run started
        self._run_sheet_durations: Optional[List[float]] = None  # seconds per phase in order
        # Last source that produced the current phase, for UI/debug (manual/external/propresenter/run_sheet/previous).
        self._last_source: str = "initial"

    @property
    def current_phase(self) -> str:
        return self._current_phase

    @property
    def phase_source(self) -> str:
        """Return the last source that set the current phase (for debug/UX)."""
        return self._last_source

    def set_manual_override(self, phase_id: Optional[str]) -> None:
        """Set or clear manual phase override (e.g. from API/UI)."""
        self._manual_override = phase_id if phase_id in self.phase_ids else phase_id

    def get_manual_override(self) -> Optional[str]:
        return self._manual_override

    def set_external_override(self, phase_id: Optional[str], reason: Optional[str] = None) -> None:
        """
        Set or clear external phase override (e.g. from audio/X32 or other adapters).
        Manual override (set_manual_override) always takes precedence.
        """
        if phase_id is None:
            self._external_override = None
            self._external_reason = None
            return
        self._external_override = phase_id if phase_id in self.phase_ids else phase_id
        self._external_reason = reason

    def get_external_override(self) -> Optional[str]:
        return self._external_override

    def get_external_override_reason(self) -> Optional[str]:
        return self._external_reason

    def set_current_phase(self, phase_id: str) -> None:
        """Set current phase directly (for replay mode). phase_id must be in phase_ids."""
        if phase_id in self.phase_ids:
            self._current_phase = phase_id
            self._last_source = "replay"

    def set_run_sheet(self, start_time_monotonic: float, phase_durations_seconds: List[float]) -> None:
        """Optional: set run sheet for time-based phase fallback."""
        self._run_sheet_start = start_time_monotonic
        self._run_sheet_durations = phase_durations_seconds

    def update(
        self,
        propresenter_phase: Optional[str] = None,
    ) -> str:
        """
        Update current phase from ProPresenter (and optional run-sheet/manual).
        Returns new current phase. Fires on_phase_changed(previous, current) when phase changes.
        """
        previous = self._current_phase
        source = "previous"
        if self._manual_override is not None:
            self._current_phase = self._manual_override
            source = "manual_override"
        elif self._external_override is not None and self._external_override in self.phase_ids:
            self._current_phase = self._external_override
            source = "external_override"
        elif propresenter_phase is not None and propresenter_phase in self.phase_ids:
            self._current_phase = propresenter_phase
            source = "propresenter"
        elif self._run_sheet_start is not None and self._run_sheet_durations:
            phase_index = self._phase_index_from_run_sheet()
            if phase_index is not None and 0 <= phase_index < len(self.phase_ids):
                self._current_phase = self.phase_ids[phase_index]
                source = "run_sheet"
            # else keep previous
        # else keep previous
        self._last_source = source
        if self._current_phase != previous:
            extra = ""
            if source == "external_override" and self._external_reason:
                extra = f" (reason={self._external_reason})"
            logger.info("Phase changed: %s -> %s [%s]%s", previous, self._current_phase, source, extra)
            if self.on_phase_changed:
                try:
                    self.on_phase_changed(previous, self._current_phase)
                except Exception as e:
                    logger.warning("on_phase_changed: %s", e)
        return self._current_phase

    def _phase_index_from_run_sheet(self) -> Optional[int]:
        import time
        if self._run_sheet_start is None or not self._run_sheet_durations:
            return None
        elapsed = time.monotonic() - self._run_sheet_start
        total = 0.0
        for i, dur in enumerate(self._run_sheet_durations):
            total += dur
            if elapsed < total:
                return i
        return len(self._run_sheet_durations) - 1
