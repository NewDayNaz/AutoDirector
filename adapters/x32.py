"""
Behringer X32 OSC adapter: band DCA mute state and ProPresenter channel level.

Uses a single UDP socket bound to our listen port so X32 replies come back to us.
Polls /dca/N/on and optionally /ch/M/mix/fader. Optional disable when not configured.
"""

from __future__ import annotations

import logging
import socket
import struct
import threading
import time
from typing import Optional

logger = logging.getLogger(__name__)

try:
    from pythonosc import osc_bundle_builder
    from pythonosc import osc_message_builder
    from pythonosc.parsing import osc_types
    _OSC_AVAILABLE = True
except ImportError:
    _OSC_AVAILABLE = False


def _build_osc_message(addr: str, *args: tuple) -> bytes:
    builder = osc_message_builder.OscMessageBuilder(addr)
    for a in args:
        builder.add_arg(a)
    return builder.build().dgram


def _parse_osc_message(data: bytes) -> Optional[tuple]:
    """Return (address, [args]) or None. Minimal parser for X32 replies."""
    try:
        i = 0
        if data[i : i + 1] != b"/":
            return None
        end = data.find(b"\0", i)
        if end == -1:
            return None
        addr = data[i:end].decode("utf-8", errors="replace")
        i = (end + 4) & ~3  # align to 4
        args = []
        if i < len(data) and data[i : i + 1] == b",":
            i += 1
            types_end = data.find(b"\0", i)
            if types_end == -1:
                return (addr, args)
            types = data[i:types_end].decode("utf-8", errors="replace")
            i = (types_end + 4) & ~3
            for t in types:
                if i >= len(data):
                    break
                if t == "i":
                    if i + 4 <= len(data):
                        args.append(struct.unpack(">i", data[i : i + 4])[0])
                    i += 4
                elif t == "f":
                    if i + 4 <= len(data):
                        args.append(struct.unpack(">f", data[i : i + 4])[0])
                    i += 4
        return (addr, args)
    except Exception:
        return None


class X32Adapter:
    """
    X32 OSC: band DCA mute and ProPresenter channel level.
    Call start() to begin background poll; then read is_band_muted() and get_propresenter_level().
    """

    def __init__(
        self,
        host: str,
        port: int = 10023,
        band_dca_index: int = 1,
        pastor_dca_index: Optional[int] = None,
        propresenter_channel: Optional[int] = None,
        listen_port: int = 10024,
        poll_interval_sec: float = 0.25,
    ):
        if not _OSC_AVAILABLE:
            raise ImportError("python-osc is required for X32 adapter. pip install python-osc")
        self.host = host
        self.port = port
        self.band_dca_index = band_dca_index
        self.pastor_dca_index = pastor_dca_index
        self.propresenter_channel = propresenter_channel
        self.listen_port = listen_port
        self.poll_interval_sec = poll_interval_sec
        self._sock: Optional[socket.socket] = None
        self._thread: Optional[threading.Thread] = None
        self._band_muted: Optional[bool] = None
        self._pastor_muted: Optional[bool] = None
        self._pp_level: float = 0.0
        self._lock = threading.Lock()
        self._running = False
        self._last_xremote = 0.0
        self._last_response = 0.0

    def start(self) -> bool:
        """Start background poll thread. Returns True if started."""
        if self._thread is not None:
            return True
        try:
            self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self._sock.bind(("0.0.0.0", self.listen_port))
            self._sock.settimeout(0.5)
        except OSError as e:
            logger.warning("X32 bind failed: %s", e)
            return False
        self._running = True
        self._thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._thread.start()
        logger.info("X32 adapter started (send/receive on port %s)", self.listen_port)
        return True

    def _poll_loop(self):
        while self._running and self._sock:
            now = time.monotonic()
            if now - self._last_xremote > 5.0:
                try:
                    msg = _build_osc_message("/xremote")
                    self._sock.sendto(msg, (self.host, self.port))
                    self._last_xremote = now
                except OSError as e:
                    logger.debug("X32 xremote: %s", e)
            try:
                if self.band_dca_index is not None:
                    msg = _build_osc_message(f"/dca/{self.band_dca_index}/on")
                    self._sock.sendto(msg, (self.host, self.port))
                if self.pastor_dca_index is not None:
                    msg = _build_osc_message(f"/dca/{self.pastor_dca_index}/on")
                    self._sock.sendto(msg, (self.host, self.port))
                if self.propresenter_channel is not None:
                    ch = self.propresenter_channel
                    msg = _build_osc_message(f"/ch/{ch:02d}/mix/fader")
                    self._sock.sendto(msg, (self.host, self.port))
            except OSError as e:
                logger.debug("X32 send: %s", e)
            try:
                data, _ = self._sock.recvfrom(4096)
                parsed = _parse_osc_message(data)
                if parsed:
                    addr, args = parsed
                    with self._lock:
                        self._last_response = time.monotonic()
                    if addr.startswith("/dca/") and "on" in addr:
                        dca_index = None
                        try:
                            parts = addr.split("/")
                            if len(parts) >= 3:
                                dca_index = int(parts[2])
                        except (ValueError, TypeError):
                            dca_index = None
                        muted: Optional[bool] = None
                        if args:
                            try:
                                # X32: /dca/N/on -> 1 = ON (unmuted), 0 = OFF (muted)
                                muted = not bool(int(args[0]))
                            except (ValueError, TypeError):
                                muted = None
                        with self._lock:
                            if dca_index is not None:
                                if dca_index == self.band_dca_index:
                                    self._band_muted = muted
                                if self.pastor_dca_index is not None and dca_index == self.pastor_dca_index:
                                    self._pastor_muted = muted
                    elif "/ch/" in addr and "fader" in addr:
                        with self._lock:
                            self._pp_level = max(0.0, min(1.0, float(args[0]))) if args else 0.0
            except socket.timeout:
                pass
            except OSError:
                if self._running:
                    pass
                break
            time.sleep(self.poll_interval_sec)

    def stop(self):
        self._running = False
        if self._sock:
            try:
                self._sock.close()
            except Exception:
                pass
            self._sock = None
        self._thread = None

    def is_band_muted(self) -> Optional[bool]:
        """True if band DCA is muted, False if unmuted, None if unknown."""
        with self._lock:
            return self._band_muted

    def is_pastor_muted(self) -> Optional[bool]:
        """True if pastor DCA is muted, False if unmuted, None if unknown or not configured."""
        with self._lock:
            return self._pastor_muted

    def get_propresenter_level(self) -> float:
        """ProPresenter channel level 0.0–1.0. Returns 0.0 if not configured."""
        with self._lock:
            return self._pp_level if self.propresenter_channel is not None else 0.0

    def poll(self):
        """No-op when using background thread; kept for API compatibility."""
        pass

    @property
    def has_recent_response(self) -> bool:
        with self._lock:
            return (time.monotonic() - self._last_response) < 15.0
