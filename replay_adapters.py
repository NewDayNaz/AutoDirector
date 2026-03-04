"""
Replay adapters: provide recorded state to the director during replay mode.

Used when running the director with a state recording + multiview video file
so decisions can be replayed and tuned without live ProPresenter/X32/ATEM.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List

from state_capture import get_state_at_t

logger = logging.getLogger(__name__)


class ReplayATEMStub:
    """
    ATEM stub for replay: returns recorded program_input so director logic
    (same_input, pacing) matches the recording. cut_to_input is a no-op.
    """

    def __init__(self, frames: List[Dict[str, Any]], get_t: Callable[[], float]):
        self._frames = frames
        self._get_t = get_t

    def connect(self) -> bool:
        return True

    @property
    def is_connected(self) -> bool:
        return True

    def get_program_input(self) -> Any:
        return get_state_at_t(self._frames, self._get_t()).get("program_input")

    def get_preview_input(self) -> Any:
        return None

    def cut_to_input(self, input_id: int, duration_sec: float = 0.25) -> bool:
        return True


class ReplayProPresenterAdapter:
    """
    ProPresenter adapter that returns values from a loaded state recording
    at the current replay time (get_t()).
    """

    def __init__(
        self,
        frames: List[Dict[str, Any]],
        get_t: Callable[[], float],
        playlist_item_to_phase: Dict[str, str],
        unmapped_fallback_phase: str | None = None,
    ):
        self._frames = frames
        self._get_t = get_t
        self.playlist_item_to_phase = playlist_item_to_phase or {}
        self._unmapped_fallback_phase = unmapped_fallback_phase

    def _state(self) -> Dict[str, Any]:
        return get_state_at_t(self._frames, self._get_t())

    def poll(self) -> None:
        pass

    @property
    def is_connected(self) -> bool:
        return True

    def get_current_phase(self) -> str | None:
        return self._state().get("pp_phase")

    def get_current_item_name(self) -> str | None:
        return self._state().get("pp_item_name")

    def get_stage_display_layout(self) -> str | None:
        return self._state().get("stage_layout")

    def get_slide_index(self) -> int | None:
        return self._state().get("pp_slide_index")

    def get_slide_type(self) -> str | None:
        return self._state().get("pp_slide_type")


class ReplayX32Adapter:
    """
    X32 adapter that returns values from a loaded state recording at the current replay time.
    """

    def __init__(self, frames: List[Dict[str, Any]], get_t: Callable[[], float]):
        self._frames = frames
        self._get_t = get_t

    def start(self) -> bool:
        return True

    def stop(self) -> None:
        pass

    def _state(self) -> Dict[str, Any]:
        return get_state_at_t(self._frames, self._get_t())

    def is_band_muted(self) -> bool | None:
        return self._state().get("band_muted")

    def is_pastor_muted(self) -> bool | None:
        return self._state().get("pastor_muted")

    def get_propresenter_level(self) -> float:
        return float(self._state().get("pp_level", 0.0))

    @property
    def has_recent_response(self) -> bool:
        return True


class ReplayDetector:
    """
    Detector that returns recorded inputs_with_people (and minimal all_results)
    for the current replay time, so replay can run without running CV.
    """

    def __init__(self, frames: List[Dict[str, Any]], get_t: Callable[[], float]):
        self._frames = frames
        self._get_t = get_t

    def process_segments(
        self,
        segments: List[Any],
        confidence_threshold: float = 0.5,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        state = get_state_at_t(self._frames, self._get_t())
        inputs_with_people = list(state.get("inputs_with_people") or [])
        input_set = set(inputs_with_people)
        all_results = []
        for input_id, _ in segments:
            has_person = input_id in input_set
            all_results.append({
                "input": input_id,
                "has_person": has_person,
                "confidence": 1.0 if has_person else 0.0,
                "person_count": 1 if has_person else 0,
            })
        n = len(segments)
        return {
            "inputs_with_people": inputs_with_people,
            "all_results": all_results,
            "summary": {
                "total_inputs": n,
                "inputs_with_people": len(inputs_with_people),
                "inputs_without_people": n - len(inputs_with_people),
                "detection_rate": len(inputs_with_people) / n if n else 0,
            },
        }
