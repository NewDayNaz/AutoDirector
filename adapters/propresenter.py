"""
ProPresenter 7 WebSocket adapter: current playlist/item, stage display layout, slide type.

Protocol matches the bitfocus companion module (github.com/bitfocus/companion-module-renewedvision-propresenter):
  1. Connect to ws://host:port/remote. Pro7 requires Sec-WebSocket-Key and Sec-WebSocket-Version (CamelCase).
  2. First message: { "action": "authenticate", "protocol": "701", "password": "<pass or ''>" }.
  3. After authenticate response, request state: presentationCurrent, presentationSlideIndex, stageDisplaySets.
  4. Subsequent requests use {"action": "presentationCurrent", "presentationSlideQuality": 0} so ProPresenter
     omits base64 slide images (we only need presentationPath/name and slide index; images can cause huge
     payloads and "goodbye" disconnects). See Pro7 API: presentationSlideQuality 0 = no previews.

ProPresenter may close with "goodbye"; we reconnect with backoff. In-thread keepalive sends state requests when idle.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import threading
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Diagnostic logging: set to True to log every WS send/recv (password redacted)
LOG_WS_TRAFFIC = False
# Set env PP_LOG_RECV=1 to log every incoming message (action + keys) at INFO so you can see what Pro7 sends
LOG_PP_RECV = False


def _redact_password(obj: Dict[str, Any]) -> Dict[str, Any]:
    """Return a copy safe for logging (password values replaced)."""
    if not isinstance(obj, dict):
        return obj
    out: Dict[str, Any] = {}
    for k, v in obj.items():
        if k.lower() == "password":
            out[k] = "***" if v else "(empty)"
        elif isinstance(v, dict):
            out[k] = _redact_password(v)
        else:
            out[k] = v
    return out


try:
    import websocket
    _WS_AVAILABLE = True
except ImportError:
    _WS_AVAILABLE = False


class ProPresenterAdapter:
    """
    ProPresenter 7 remote WebSocket API. Call start() then get_current_phase(),
    get_current_item_name(), get_stage_display_layout(), get_slide_type().
    """

    def __init__(
        self,
        host: str,
        port: int = 50001,
        password: Optional[str] = None,
        playlist_item_to_phase: Optional[Dict[str, str]] = None,
        poll_interval_sec: float = 1,
        unmapped_fallback_phase: Optional[str] = None,
    ):
        if not _WS_AVAILABLE:
            raise ImportError("websocket-client is required for ProPresenter adapter. pip install websocket-client")
        self.host = host.strip()
        self.port = port
        self._password: Optional[str] = (password or "").strip() or None
        self.playlist_item_to_phase = playlist_item_to_phase or {}
        self.poll_interval_sec = poll_interval_sec
        self._unmapped_fallback_phase: Optional[str] = unmapped_fallback_phase
        self._ws: Optional[websocket.WebSocketApp] = None
        self._thread: Optional[threading.Thread] = None
        self._running = False
        self._connected = False
        self._current_item_name: Optional[str] = None
        self._current_presentation_path: Optional[str] = None
        self._stage_display_layout_name: Optional[str] = None
        self._slide_index: Optional[int] = None
        self._slide_type: Optional[str] = None  # "video", "image", "text" if available
        # Slide index -> True when slide has video media action (built from presentation metadata).
        self._slide_media_is_video: Dict[int, bool] = {}
        self._lock = threading.Lock()
        self._last_connect_attempt = 0.0
        self._backoff_sec = 1.0
        self._max_backoff_sec = 60.0
        self._logged_refused = False
        self._refused = False
        self._keepalive_interval_sec = 25.0
        self._last_keepalive = 0.0
        self._last_poll_send = 0.0  # throttle: only send state requests at most every poll_interval_sec

        # Automatic song → band-phase mapping: playlist item name -> phase id
        self._auto_song_phase: Dict[str, str] = {}
        self._song_phase_sequence: List[str] = self._build_song_phase_sequence()

    def _build_song_phase_sequence(self) -> List[str]:
        """
        Build ordered list of "band-like" phases to use for automatic song mapping,
        based on the canonical PHASE_IDS from config.schema when available.
        """
        try:
            # Local import to avoid any import-time cycles
            from config.schema import PHASE_IDS  # type: ignore

            phase_ids = list(PHASE_IDS)
        except Exception:
            phase_ids = []

        band_like = [
            p
            for p in phase_ids
            if p.startswith("Band")
        ]
        # Fallback sequence if schema import fails or there are no matching phases
        if not band_like:
            band_like = ["Band"]
        return band_like

    def _update_slide_media_map_from_presentation(self, pres: Dict[str, Any]) -> None:
        """
        Build a map of slide_index -> bool indicating whether that slide has a
        video media action, based on the full presentation metadata ProPresenter
        sends in presentationCurrent.
        """
        slide_is_video: Dict[int, bool] = {}
        idx = 0
        groups = pres.get("presentationSlideGroups") or pres.get("groups") or []
        for group in groups:
            if not isinstance(group, dict):
                continue
            slides = group.get("groupSlides") or group.get("slides") or []
            for slide in slides:
                is_video = False
                if isinstance(slide, dict):
                    actions = slide.get("actions") or []
                    for action in actions:
                        if not isinstance(action, dict):
                            continue
                        media_name = str(
                            action.get("actionMediaName")
                            or action.get("actionMediaTarget")
                            or ""
                        ).lower()
                        media_type = str(
                            action.get("actionMediaType")
                            or action.get("actionType")
                            or ""
                        ).lower()
                        if media_type in ("video", "movie"):
                            is_video = True
                        elif media_name:
                            if media_name.endswith(
                                (
                                    ".mp4",
                                    ".mov",
                                    ".m4v",
                                    ".avi",
                                    ".mkv",
                                    ".mpg",
                                    ".mpeg",
                                )
                            ):
                                is_video = True
                        if is_video:
                            break
                slide_is_video[idx] = is_video
                idx += 1
        self._slide_media_is_video = slide_is_video
        # Refresh current slide_type from the new map if we already have an index.
        if self._slide_index is not None:
            if self._slide_media_is_video.get(self._slide_index):
                self._slide_type = "video"
            elif self._slide_type == "video":
                self._slide_type = None

    def _get_or_assign_song_phase(
        self,
        name: str,
        stage_layout_name: Optional[str],
    ) -> Optional[str]:
        """
        For a playlist item name that is not explicitly mapped in playlist_item_to_phase,
        assign it to the next "band-like" phase in order (Band1, Band2, Band3, Acoustic, …),
        using the order of appearance in the playlist as seen from ProPresenter.
        """
        # Respect explicit config: if the item is configured, it is not auto-mapped.
        if name in self.playlist_item_to_phase:
            return None

        # Already auto-assigned for this service.
        if name in self._auto_song_phase:
            return self._auto_song_phase[name]

        # Optional heuristic: only auto-map when the stage display layout looks like lyrics.
        if stage_layout_name:
            layout_upper = stage_layout_name.upper()
            if "LYRICS" not in layout_upper:
                return None

        if not self._song_phase_sequence:
            return None

        # Nth distinct song item -> Nth band phase, clamped to the last if there are extra songs.
        idx = len(self._auto_song_phase)
        if idx >= len(self._song_phase_sequence):
            phase = self._song_phase_sequence[-1]
        else:
            phase = self._song_phase_sequence[idx]

        self._auto_song_phase[name] = phase
        return phase

    def log_playlist_mapping_snapshot(self) -> None:
        """
        Log a one-time snapshot of the playlist item → phase mapping
        (explicit config plus any auto-assigned song phases).
        """
        with self._lock:
            explicit = dict(self.playlist_item_to_phase)
            auto = dict(self._auto_song_phase)
        combined: Dict[str, str] = {}
        combined.update(explicit)
        combined.update(auto)
        if not combined:
            logger.info("ProPresenter playlist_item_to_phase: (no playlist mapping configured)")
            return
        mapping_str = ", ".join(f"{k!r} -> {v}" for k, v in sorted(combined.items()))
        logger.info("ProPresenter playlist → phase mapping (explicit + auto): %s", mapping_str)

    def start(self) -> bool:
        """Start WebSocket connection and receive thread. Returns True if connect initiated."""
        if self._thread is not None:
            return True
        # Reduce websocket-client log noise when ProPresenter is not running
        logging.getLogger("websocket").setLevel(logging.WARNING)
        self._running = True
        self._logged_refused = False
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()
        logger.info("ProPresenter adapter started (connecting to %s:%s)", self.host, self.port)
        return True

    def _run_loop(self):
        while self._running:
            now = time.monotonic()
            if not self._connected and (now - self._last_connect_attempt) >= self._backoff_sec:
                self._last_connect_attempt = now
                self._connect_and_run()
                self._backoff_sec = min(self._backoff_sec * 2, self._max_backoff_sec)
            else:
                if self._connected:
                    self._backoff_sec = 1.0
                    # Keepalive: send state requests periodically so server doesn't close idle connection
                    if (now - self._last_keepalive) >= self._keepalive_interval_sec:
                        self._last_keepalive = now
                        self._request_state()
                time.sleep(0.5)

    def _connect_and_run(self):
        self._refused = False
        url = f"ws://{self.host}:{self.port}/remote"
        if LOG_WS_TRAFFIC:
            logger.info("ProPresenter WS connecting to %s", url)
        # Pro7 expects Sec-WebSocket-* headers in CamelCase; pass explicitly.
        ws_key = base64.b64encode(os.urandom(16)).decode().strip()
        header = {"Sec-WebSocket-Key": ws_key, "Sec-WebSocket-Version": "13"}
        try:
            self._ws = websocket.WebSocketApp(
                url,
                header=header,
                on_open=self._on_open,
                on_message=self._on_message,
                on_error=self._on_error,
                on_close=self._on_close,
            )
            self._ws.run_forever(ping_interval=15, ping_timeout=10)
        except Exception as e:
            logger.debug("ProPresenter WS: %s", e)
        finally:
            with self._lock:
                self._connected = False
                # On connection refused, back off longer to avoid log spam
                if not self._connected and getattr(self, "_refused", False):
                    self._backoff_sec = min(30.0, self._max_backoff_sec)

    def _on_open(self, _ws):
        with self._lock:
            self._connected = True
            self._backoff_sec = 1.0
            self._last_keepalive = time.monotonic()
        logger.info("ProPresenter connected")
        has_password = bool(self._password)
        logger.info("ProPresenter auth: using password from config=%s", has_password)
        # Match bitfocus companion module: action "authenticate", protocol string "701", password always present.
        payload: Dict[str, Any] = {
            "action": "authenticate",
            "protocol": "701",
            "password": self._password if self._password else "",
        }
        self._send(payload)

    def _on_message(self, _ws, message: str):
        try:
            data = json.loads(message)
        except json.JSONDecodeError as e:
            if LOG_WS_TRAFFIC:
                logger.info("ProPresenter WS recv (invalid JSON): %s", message[:200])
            logger.debug("ProPresenter WS JSON decode error: %s", e)
            return
        if LOG_WS_TRAFFIC:
            logger.info("ProPresenter WS recv: %s", json.dumps(_redact_password(data)))
        action = data.get("action") or data.get("acn")
        if LOG_PP_RECV:
            keys = list(data.keys()) if isinstance(data, dict) else []
            logger.info("ProPresenter recv: action=%s keys=%s", action, keys)
            if "presentationPath" in data or "presentation" in data or "presentationName" in data or "name" in data:
                logger.info(
                    "ProPresenter recv (presentation): path=%r name=%r",
                    data.get("presentationPath") or data.get("presentation"),
                    data.get("presentationName") or data.get("name"),
                )
        if action in ("authenticate", "authenticate__sub"):
            # Check for auth failure (e.g. protocol or password)
            if data.get("authenticated") is False or data.get("error"):
                logger.warning("ProPresenter auth failed: %s", data)
            else:
                self._request_state()
            return
        # Pro7 may respond with presentationCurrent__sub, presentationRequest__sub, or other;
        # accept any message that carries presentation path/name so we don't miss due to action string
        has_path = "presentationPath" in data or "presentation" in data
        has_name = (
            data.get("presentationName")
            or data.get("name")
            or (isinstance(data.get("presentation"), dict)
                and (data["presentation"].get("presentationName") or data["presentation"].get("name")))
        )
        is_presentation_state = (
            action in ("presentationCurrent", "presentationCurrent__sub", "presentationRequest", "presentationRequest__sub")
            or (has_path or has_name)
        )
        if is_presentation_state:
            with self._lock:
                self._current_presentation_path = data.get("presentationPath") or data.get("presentation")
                raw_name = data.get("presentationName") or data.get("name")
                # Many Pro7 responses nest the name under the "presentation" object
                pres = data.get("presentation")
                if isinstance(pres, dict):
                    raw_name = pres.get("presentationName") or pres.get("name") or raw_name
                    # Also update per-slide media map from full presentation metadata when available.
                    self._update_slide_media_map_from_presentation(pres)

                def _valid_item_name(v) -> Optional[str]:
                    if v is None:
                        return None
                    s = str(v).strip()
                    # Treat strings that are effectively numeric (indices/paths like "0.0.1:2") as invalid.
                    if not s or s.replace(".", "").replace("-", "").replace(":", "").isdigit():
                        return None
                    return s

                name = _valid_item_name(raw_name)
                # Pro7 often updates path but not name; use last path component as item name (e.g. "Service/Pre Service Slideshow" -> "Pre Service Slideshow")
                if not name and self._current_presentation_path:
                    path_str = (self._current_presentation_path or "").replace("\\", "/")
                    # Last segment, strip extension
                    last = path_str.rstrip("/").split("/")[-1] if path_str else ""
                    stem = last.rsplit(".", 1)[0] if last else last
                    name = _valid_item_name(stem)
                if not name and self._current_presentation_path:
                    stem = os.path.basename(
                        (self._current_presentation_path or "").replace("\\", "/")
                    ).rsplit(".", 1)[0] or self._current_presentation_path
                    name = _valid_item_name(stem)
                prev_name = self._current_item_name
                if name:
                    self._current_item_name = name
                # else leave _current_item_name unchanged (don't overwrite with "0.0" or other junk)

                # Track slide index and attempt to infer slide type (video/image/text) from metadata.
                prev_index = self._slide_index
                if "slideIndex" in data:
                    idx_raw = data.get("slideIndex")
                    self._slide_index = int(idx_raw) if idx_raw is not None and str(idx_raw).isdigit() else None
                    index_changed = self._slide_index is not None and self._slide_index != prev_index
                else:
                    index_changed = False

                # Prefer explicit per-slide media map (video vs non-video) when available.
                slide_type: Optional[str] = None
                if self._slide_index is not None and self._slide_media_is_video.get(self._slide_index):
                    slide_type = "video"

                # Infer slide type from known ProPresenter fields when available.
                candidates = ("slideType", "presentationSlideType", "type", "mediaType")
                if slide_type is None:
                    for key in candidates:
                        value = data.get(key)
                        if isinstance(value, str) and value.strip():
                            slide_type = value.strip().lower()
                            break
                # Some responses may nest slide metadata under a \"slide\" or \"presentation\" object.
                if slide_type is None:
                    slide_obj = data.get("slide") or data.get("presentation")
                    if isinstance(slide_obj, dict):
                        for key in candidates:
                            value = slide_obj.get(key)
                            if isinstance(value, str) and value.strip():
                                slide_type = value.strip().lower()
                                break
                # Fallback: infer from path extension when the current presentation path looks like media.
                if slide_type is None and self._current_presentation_path:
                    path_lower = str(self._current_presentation_path).lower()
                    if path_lower.endswith((".mp4", ".mov", ".m4v", ".avi", ".mkv", ".mpg", ".mpeg")):
                        slide_type = "video"
                    elif path_lower.endswith((".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tiff", ".tif", ".webp")):
                        slide_type = "image"

                if slide_type is not None:
                    self._slide_type = slide_type
                elif index_changed:
                    # When the slide index changes and no type is provided, clear stale type.
                    self._slide_type = None

                # Log when playlist item changes (for debugging phase detection)
                if self._current_item_name != prev_name:
                    phase = self.playlist_item_to_phase.get(self._current_item_name)
                    if not phase and self._current_item_name:
                        for key, value in self.playlist_item_to_phase.items():
                            if key in self._current_item_name or self._current_item_name in key:
                                phase = value
                                break
                    # If still unmapped, attempt automatic song → band-phase mapping.
                    if not phase and self._current_item_name:
                        auto_phase = self._get_or_assign_song_phase(
                            self._current_item_name,
                            self._stage_display_layout_name,
                        )
                        if auto_phase:
                            phase = auto_phase
                    logger.info(
                        "ProPresenter playlist item: %s -> phase %s",
                        self._current_item_name or "(none)",
                        phase if phase else "(unmapped)",
                    )
                else:
                    # Log every time we get presentation state so user sees we're receiving (item may be unchanged)
                    logger.debug(
                        "ProPresenter state: item=%s path=%s (no change)",
                        self._current_item_name or "(none)",
                        (self._current_presentation_path or "")[:60],
                    )
            return
        if action in ("presentationSlideIndex", "presentationSlideIndex__sub"):
            with self._lock:
                prev_index = self._slide_index
                idx = data.get("slideIndex")
                self._slide_index = int(idx) if idx is not None and str(idx).isdigit() else None
                # When ProPresenter only sends a slide index update without metadata,
                # derive slide_type from our per-slide media map (video vs non-video)
                # and clear any stale type when moving to a non-video slide.
                if self._slide_index is not None and self._slide_index != prev_index:
                    if self._slide_media_is_video.get(self._slide_index):
                        self._slide_type = "video"
                    else:
                        self._slide_type = None
            return
        if action in ("stageDisplaySets", "stageDisplaySets__sub", "stageDisplayChangeLayout", "stageDisplayChangeLayout__sub"):
            with self._lock:
                layouts = data.get("stageLayouts") or []
                screens = data.get("stageScreens") or []
                selected = None
                for s in screens:
                    uid = s.get("stageLayoutSelectedLayoutUUID")
                    if uid:
                        for lay in layouts:
                            if lay.get("stageLayoutUUID") == uid:
                                selected = lay.get("stageLayoutName")
                                break
                        break
                if selected:
                    self._stage_display_layout_name = selected
            return
        if action in ("stageDisplaySetIndex", "stageDisplaySetIndex__sub"):
            with self._lock:
                self._stage_display_layout_name = data.get("stageLayoutName") or self._stage_display_layout_name
            return
        # Log unhandled messages so user can see what ProPresenter is sending (e.g. for debugging)
        logger.debug("ProPresenter message (unhandled): action=%s", action)

    def _on_error(self, _ws, error):
        if LOG_WS_TRAFFIC:
            logger.info("ProPresenter WS error: %s", error)
        err_str = str(error).lower()
        if "10061" in err_str or "refused" in err_str or "actively refused" in err_str:
            self._refused = True
            if not getattr(self, "_logged_refused", False):
                self._logged_refused = True
                logger.info("ProPresenter not running at %s:%s (connection refused) - phase will use manual/fallback", self.host, self.port)
        elif not LOG_WS_TRAFFIC:
            logger.debug("ProPresenter WS error: %s", error)

    def _on_close(self, _ws, close_status_code=None, close_msg=None):
        with self._lock:
            self._connected = False
        if LOG_WS_TRAFFIC or close_status_code is not None or close_msg:
            reason = (close_msg or "").strip()
            logger.info("ProPresenter WS close: code=%s reason=%s", close_status_code, reason or "(none)")
            if reason.lower() == "goodbye":
                logger.info(
                    "ProPresenter closed (goodbye). Often when another remote client connects or server shuts down; reconnecting."
                )

    def _send(self, obj: Dict[str, Any]):
        if not self._ws:
            if LOG_WS_TRAFFIC:
                logger.info("ProPresenter WS send (no socket): %s", json.dumps(_redact_password(obj)))
            return
        try:
            raw = json.dumps(obj)
            self._ws.send(raw)
            if LOG_WS_TRAFFIC:
                logger.info("ProPresenter WS send: %s", json.dumps(_redact_password(obj)))
        except Exception as e:
            logger.warning("ProPresenter WS send failed: %s", e)
            if LOG_WS_TRAFFIC:
                logger.info("ProPresenter WS send (failed): %s", json.dumps(_redact_password(obj)))

    def _request_state(self) -> None:
        """Send presentationCurrent (no slide images), presentationSlideIndex, stageDisplaySets. No throttling."""
        # presentationSlideQuality=0: Pro7 API omits base64 slideImage data to avoid huge payloads and disconnects
        self._send({"action": "presentationCurrent", "presentationSlideQuality": 0})
        self._send({"action": "presentationSlideIndex"})
        self._send({"action": "stageDisplaySets"})

    def poll(self) -> None:
        """Request latest state (current presentation, stage layout). Throttled to poll_interval_sec."""
        if not self._ws or not self._connected:
            return
        now = time.monotonic()
        with self._lock:
            if (now - self._last_poll_send) < self.poll_interval_sec:
                return
            self._last_poll_send = now
        self._request_state()

    def stop(self):
        self._running = False
        if self._ws:
            try:
                self._ws.close()
            except Exception:
                pass
            self._ws = None
        self._thread = None
        with self._lock:
            self._connected = False

    def get_current_phase(self) -> Optional[str]:
        """Map current playlist item to phase id using playlist_item_to_phase. None if unknown."""
        with self._lock:
            name = self._current_item_name
            stage_layout_name = self._stage_display_layout_name
        if not name:
            return None
        # Exact match first
        phase = self.playlist_item_to_phase.get(name)
        if phase:
            return phase
        # Partial match (e.g. item "Welcome - Greeting" vs config "Welcome - Greeting")
        for key, value in self.playlist_item_to_phase.items():
            if key in name or name in key:
                return value
        # Automatic song → band-phase mapping for unmapped items, in playlist order.
        auto_phase = self._get_or_assign_song_phase(name, stage_layout_name)
        if auto_phase:
            return auto_phase
        # Final fallback: optional configured phase for any remaining unmapped items.
        if self._unmapped_fallback_phase:
            try:
                from config.schema import PHASE_IDS  # type: ignore

                if self._unmapped_fallback_phase in PHASE_IDS:
                    return self._unmapped_fallback_phase
            except Exception:
                # If schema import fails for any reason, still return the configured fallback.
                return self._unmapped_fallback_phase
        return None

    def get_current_item_name(self) -> Optional[str]:
        """Current presentation/playlist item name."""
        with self._lock:
            return self._current_item_name

    def get_current_presentation_path(self) -> Optional[str]:
        with self._lock:
            return self._current_presentation_path

    def get_stage_display_layout(self) -> Optional[str]:
        """Stage display layout name (e.g. 'NEW DAY LYRICS' vs 'NEW DAY LIVE VIDEO')."""
        with self._lock:
            return self._stage_display_layout_name

    def get_slide_index(self) -> Optional[int]:
        with self._lock:
            return self._slide_index

    def get_slide_type(self) -> Optional[str]:
        """Slide type if provided by API ('video', 'image', 'text'). May be None."""
        with self._lock:
            return self._slide_type

    @property
    def is_connected(self) -> bool:
        with self._lock:
            return self._connected
