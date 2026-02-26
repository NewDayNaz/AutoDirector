"""
PTZ camera preset recall. Vendor-agnostic wrapper; implement recall_preset for your camera.

Supported backends can be added (e.g. ONVIF, VISCA over serial/IP). When not configured
or when backend is unavailable, recall is a no-op and returns False.
"""

from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)


class PTZAdapter:
    """
    PTZ preset recall. Call recall_preset(preset_name_or_id) on phase change.
    Subclass and override _do_recall() to integrate with your camera (ONVIF, VISCA, etc.).
    """

    def __init__(
        self,
        preset_per_phase: Optional[dict] = None,
        host: Optional[str] = None,
        port: Optional[int] = None,
    ):
        self.preset_per_phase = preset_per_phase or {}
        self.host = host
        self.port = port
        self._connected = False

    @property
    def enabled(self) -> bool:
        return bool(self.preset_per_phase)

    def recall_preset(self, preset_name_or_id: str) -> bool:
        """
        Recall the given preset by name or id. Returns True if recall was sent successfully.
        Override _do_recall() in a subclass to implement the actual camera protocol.
        """
        if not preset_name_or_id:
            return False
        try:
            return self._do_recall(preset_name_or_id)
        except Exception as e:
            logger.warning("PTZ recall_preset(%s): %s", preset_name_or_id, e)
            return False

    def _do_recall(self, preset_name_or_id: str) -> bool:
        """
        Override in subclass to send preset recall to the camera.
        Default: log only (no-op), return False so director knows no hardware was driven.
        """
        logger.debug("PTZ recall (no backend): %s", preset_name_or_id)
        return False

    def get_preset_for_phase(self, phase_id: str) -> Optional[str]:
        """Return preset name/id for the given phase, or None."""
        return self.preset_per_phase.get(phase_id)
