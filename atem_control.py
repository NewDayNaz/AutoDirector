"""
ATEM switcher control: connect, read program/preview, perform fade (mix) transitions.

Uses PyATEMMax. Reconnects with exponential backoff on disconnect.
"""

from __future__ import annotations

import logging
import time
from typing import Optional

logger = logging.getLogger(__name__)

# Lazy import so the module loads even if PyATEMMax is not installed
_ATEMMax = None


def _get_atem():
    global _ATEMMax
    if _ATEMMax is None:
        try:
            import PyATEMMax
            _ATEMMax = PyATEMMax.ATEMMax
        except ImportError as e:
            raise ImportError("PyATEMMax is required for atem_control. pip install PyATEMMax") from e
    return _ATEMMax


class ATEMControllerStub:
    """No-op ATEM controller when switcher is disabled (e.g. testing without hardware)."""

    def __init__(self, ip: str = "", transition_duration_sec: float = 0.25, use_preview: bool = True, **_):
        self.ip = ip or "(disabled)"
        self.transition_duration_sec = transition_duration_sec
        self.use_preview = use_preview

    def connect(self) -> bool:
        return False

    def disconnect(self) -> None:
        pass

    @property
    def is_connected(self) -> bool:
        return False

    def get_program_input(self) -> Optional[int]:
        return None

    def get_preview_input(self) -> Optional[int]:
        return None

    def set_preview_input(self, input_index: int) -> bool:
        return False

    def perform_transition(self, duration_sec: Optional[float] = None) -> bool:
        return False

    def cut_to_input(self, input_index: int, duration_sec: Optional[float] = None) -> bool:
        return False


class ATEMController:
    """Thin wrapper over PyATEMMax for program/preview and mix transition."""

    def __init__(
        self,
        ip: str,
        transition_duration_sec: float = 0.25,
        use_preview: bool = True,
        mix_effect_index: int = 0,
    ):
        self.ip = ip.strip()
        self.transition_duration_sec = transition_duration_sec
        self.use_preview = use_preview
        self.mix_effect_index = mix_effect_index
        self._switcher = None
        self._me = None  # ATEMConstant for mix effect
        self._connected = False
        self._last_connect_attempt = 0.0
        self._backoff_sec = 1.0
        self._max_backoff_sec = 60.0

    def _ensure_switcher(self):
        if self._switcher is None:
            ATEMMax = _get_atem()
            self._switcher = ATEMMax()
        return self._switcher

    def _video_source_for_input(self, input_index: int):
        """Map 1-based ATEM input index to PyATEMMax video source constant."""
        switcher = self._ensure_switcher()
        # ATEM protocol: input1 = 1, input2 = 2, ... up to input20 typically
        name = f"input{input_index}"
        if hasattr(switcher.atem.videoSources, name):
            return getattr(switcher.atem.videoSources, name)
        # Fallback: use raw index if the constant exists by number
        return input_index

    def connect(self) -> bool:
        """Connect to the ATEM. Returns True if connected (or already connected)."""
        if self._switcher and getattr(self._switcher, "connected", False):
            return True
        now = time.monotonic()
        if now - self._last_connect_attempt < self._backoff_sec:
            return False
        self._last_connect_attempt = now
        switcher = self._ensure_switcher()
        try:
            switcher.connect(self.ip)
            switcher.waitForConnection(infinite=False)
        except Exception as e:
            logger.debug("ATEM connect: %s", e)
            self._connected = False
            self._backoff_sec = min(self._backoff_sec * 2, self._max_backoff_sec)
            return False
        try:
            if getattr(switcher, "connected", False):
                self._connected = True
                self._backoff_sec = 1.0
                self._me = getattr(switcher.atem.mixEffects, "mixEffect1", None) or (switcher.atem.mixEffects[0] if hasattr(switcher.atem.mixEffects, "__getitem__") else None)
                logger.info("ATEM connected: %s", self.ip)
                return True
        except Exception as e:
            logger.debug("ATEM connect failed: %s", e)
        self._connected = False
        self._backoff_sec = min(self._backoff_sec * 2, self._max_backoff_sec)
        return False

    def disconnect(self) -> None:
        if self._switcher:
            try:
                self._switcher.disconnect()
            except Exception:
                pass
            self._switcher = None
        self._connected = False

    @property
    def is_connected(self) -> bool:
        if not self._switcher:
            return False
        return getattr(self._switcher, "connected", False)

    def get_program_input(self) -> Optional[int]:
        """Return current program input (1-based index) or None if unknown/disconnected."""
        if not self.connect():
            return None
        try:
            me = self._me or (self._switcher.atem.mixEffects[0] if hasattr(self._switcher.atem.mixEffects, "__getitem__") else self._switcher.atem.mixEffects.mixEffect1)
            vs = self._switcher.programInput[me].videoSource
            # videoSource may be an enum with .value or an int
            if hasattr(vs, "value"):
                return int(vs.value)
            return int(vs) if vs is not None else None
        except Exception as e:
            logger.debug("get_program_input: %s", e)
            return None

    def get_preview_input(self) -> Optional[int]:
        """Return current preview input (1-based index) or None."""
        if not self.connect():
            return None
        try:
            me = self._me or self._switcher.atem.mixEffects.mixEffect1
            vs = self._switcher.previewInput[me].videoSource
            if hasattr(vs, "value"):
                return int(vs.value)
            return int(vs) if vs is not None else None
        except Exception as e:
            logger.debug("get_preview_input: %s", e)
            return None

    def set_preview_input(self, input_index: int) -> bool:
        """Set preview bus to the given 1-based input. Returns True on success."""
        if not self.connect():
            return False
        try:
            me = self._me or self._switcher.atem.mixEffects.mixEffect1
            src = self._video_source_for_input(input_index)
            self._switcher.setPreviewInputVideoSource(me, src)
            return True
        except Exception as e:
            logger.warning("set_preview_input(%s): %s", input_index, e)
            return False

    def perform_transition(self, duration_sec: Optional[float] = None) -> bool:
        """
        Perform a mix (fade) transition. If use_preview is True, preview should
        already be set to the desired input. Sets transition rate from duration
        (in frames at 25fps) then executes auto transition.
        """
        if not self.connect():
            return False
        dur = duration_sec if duration_sec is not None else self.transition_duration_sec
        try:
            me = self._me or self._switcher.atem.mixEffects.mixEffect1
            # Ensure mix style is selected (not wipe/dip/stinger)
            if hasattr(self._switcher, "setTransitionStyle"):
                self._switcher.setTransitionStyle(me, self._switcher.atem.transitionStyles.mix)
            # Set rate in frames if the library exposes it (PyATEMMax API varies by version)
            rate_frames = max(1, int(dur * 25))
            if hasattr(self._switcher, "setMixEffectTransitionRate"):
                self._switcher.setMixEffectTransitionRate(me, rate_frames)
            elif hasattr(self._switcher, "setTransitionRate"):
                self._switcher.setTransitionRate(me, rate_frames)
            else:
                logger.debug("Transition rate not set (use switcher panel); executing mix")
            self._switcher.execAutoME(me)
            return True
        except Exception as e:
            logger.debug("perform_transition: %s", e)
            return False

    def cut_to_input(self, input_index: int, duration_sec: Optional[float] = None) -> bool:
        """
        Set preview to input_index (if use_preview) then perform transition.
        Otherwise set program directly (hard cut) if the API supports it.
        """
        if self.use_preview:
            if not self.set_preview_input(input_index):
                return False
            return self.perform_transition(duration_sec)
        # Direct cut: some ATEMs allow setProgramInputVideoSource
        if not self.connect():
            return False
        try:
            me = self._me or self._switcher.atem.mixEffects.mixEffect1
            src = self._video_source_for_input(input_index)
            self._switcher.setProgramInputVideoSource(me, src)
            return True
        except Exception as e:
            logger.warning("cut_to_input(%s): %s", input_index, e)
            return False
