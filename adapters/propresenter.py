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
from typing import Any, Dict, Optional

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
    ):
        if not _WS_AVAILABLE:
            raise ImportError("websocket-client is required for ProPresenter adapter. pip install websocket-client")
        self.host = host.strip()
        self.port = port
        self._password: Optional[str] = (password or "").strip() or None
        self.playlist_item_to_phase = playlist_item_to_phase or {}
        self.poll_interval_sec = poll_interval_sec
        self._ws: Optional[websocket.WebSocketApp] = None
        self._thread: Optional[threading.Thread] = None
        self._running = False
        self._connected = False
        self._current_item_name: Optional[str] = None
        self._current_presentation_path: Optional[str] = None
        self._stage_display_layout_name: Optional[str] = None
        self._slide_index: Optional[int] = None
        self._slide_type: Optional[str] = None  # "video", "image", "text" if available
        self._lock = threading.Lock()
        self._last_connect_attempt = 0.0
        self._backoff_sec = 1.0
        self._max_backoff_sec = 60.0
        self._logged_refused = False
        self._refused = False
        self._keepalive_interval_sec = 25.0
        self._last_keepalive = 0.0
        self._last_poll_send = 0.0  # throttle: only send state requests at most every poll_interval_sec

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
                if "slideIndex" in data:
                    self._slide_index = int(data["slideIndex"]) if str(data["slideIndex"]).isdigit() else None
                # Log when playlist item changes (for debugging phase detection)
                if self._current_item_name != prev_name:
                    phase = self.playlist_item_to_phase.get(self._current_item_name)
                    if not phase and self._current_item_name:
                        for key, value in self.playlist_item_to_phase.items():
                            if key in self._current_item_name or self._current_item_name in key:
                                phase = value
                                break
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
                idx = data.get("slideIndex")
                self._slide_index = int(idx) if idx is not None and str(idx).isdigit() else None
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
