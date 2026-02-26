#!/usr/bin/env python3
"""
Entrypoint to run the Church Auto-Director.

Loads config, creates MultiviewIngest, detector, ATEM control, adapters (X32, ProPresenter, PTZ),
phase machine, and director core. Runs the director loop; optionally start web server with --web.

Usage:
  python run_director.py [--config config.json] [--web] [--web-port 8000]
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
from pathlib import Path

# Ensure ATEM directory is on path when run as script
_ATEM_ROOT = Path(__file__).resolve().parent
if str(_ATEM_ROOT) not in sys.path:
    sys.path.insert(0, str(_ATEM_ROOT))

from config.load import load_config_path
from config.schema import DirectorConfig
from director_core import DirectorCore, RUN_MODE_RUNNING, RUN_MODE_STOPPED
from atem_control import ATEMController, ATEMControllerStub
from phase_machine import PhaseMachine


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    parser = argparse.ArgumentParser(description="Run Church Auto-Director")
    parser.add_argument("--config", "-c", default="config.json", help="Path to config JSON")
    parser.add_argument("--web", action="store_true", help="Start web API for control/debug")
    parser.add_argument("--web-port", type=int, default=8000, help="Web server port")
    args = parser.parse_args()

    config_path = Path(args.config)
    if not config_path.is_absolute():
        config_path = _ATEM_ROOT / config_path
    if not config_path.exists():
        print(f"Config not found: {config_path}", file=sys.stderr)
        sys.exit(1)
    try:
        config = load_config_path(config_path)
    except Exception as e:
        print(f"Invalid config: {e}", file=sys.stderr)
        sys.exit(1)

    # Ingest and detector (optional: if capture source not available, director still runs with no CV)
    ingest = None
    detector = None
    try:
        from multiview_ingest import MultiviewIngest
        from atem_director import ATEMMultiviewDetector
        ingest = MultiviewIngest(
            source=config.capture.source,
            profile_path=config.capture.profile_path,
            inset_ratio=config.capture.inset_ratio,
        )
        detector = ATEMMultiviewDetector()
    except Exception as e:
        logging.warning("Multiview/detector unavailable: %s", e)

    # Adapters
    x32 = None
    if config.x32 and config.x32.enabled:
        try:
            from adapters.x32 import X32Adapter
            x32 = X32Adapter(
                host=config.x32.host,
                port=config.x32.port,
                band_dca_index=config.x32.band_dca_index,
                propresenter_channel=config.x32.propresenter_channel,
            )
            x32.start()
        except Exception as e:
            logging.warning("X32 adapter unavailable: %s", e)
    pp = None
    try:
        from adapters.propresenter import ProPresenterAdapter
        pp = ProPresenterAdapter(
            host=config.propresenter.host,
            port=config.propresenter.port,
            password=config.propresenter.password,
            playlist_item_to_phase=config.playlist_item_to_phase,
        )
        pp.start()
    except Exception as e:
        logging.warning("ProPresenter adapter unavailable: %s", e)
    ptz = None
    if config.ptz and config.ptz.enabled:
        try:
            from adapters.ptz import PTZAdapter
            ptz = PTZAdapter(
                preset_per_phase=config.ptz.preset_per_phase,
                host=config.ptz.host,
                port=config.ptz.port,
            )
        except Exception as e:
            logging.warning("PTZ adapter unavailable: %s", e)

    phase_machine = PhaseMachine(
        phase_ids=config.phases.phase_ids,
        default_phase=config.phases.phase_ids[0] if config.phases.phase_ids else "Intro",
    )
    atem_enabled = getattr(config.atem, "enabled", True) and bool(config.atem.ip and config.atem.ip.strip())
    if atem_enabled:
        atem = ATEMController(
            ip=config.atem.ip,
            transition_duration_sec=config.transition_duration,
            use_preview=config.atem.use_preview,
        )
    else:
        atem = ATEMControllerStub(
            ip=config.atem.ip or "(disabled)",
            transition_duration_sec=config.transition_duration,
            use_preview=config.atem.use_preview,
        )
        logging.info("ATEM disabled (enabled=false or no IP); using stub - no switcher control")
    director = DirectorCore(
        config=config,
        ingest=ingest,
        detector=detector,
        atem_controller=atem,
        phase_machine=phase_machine,
        x32_adapter=x32,
        propresenter_adapter=pp,
        ptz_adapter=ptz,
    )

    if args.web:
        try:
            import threading
            import time
            from web.main import create_app
            app = create_app(director)
            director.set_run_mode(RUN_MODE_RUNNING)
            interval = 1.0 / max(1.0, config.loop_rate_hz)

            def director_loop():
                while director.get_run_mode() != RUN_MODE_STOPPED:
                    director.tick()
                    time.sleep(interval)

            def shutdown(_signum=None, _frame=None):
                os._exit(0)

            # Windows: SIGINT in a multithreaded app often doesn't exit; use console Ctrl handler
            if sys.platform == "win32":
                try:
                    import ctypes
                    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
                    PHANDLER_ROUTINE = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_ulong)
                    def console_handler(ctrl_type):
                        if ctrl_type in (0, 2):  # CTRL_C_EVENT, CTRL_BREAK_EVENT
                            os._exit(0)
                        return False
                    kernel32.SetConsoleCtrlHandler(PHANDLER_ROUTINE(console_handler), True)
                except Exception:
                    pass
            signal.signal(signal.SIGINT, shutdown)
            if hasattr(signal, "SIGTERM"):
                signal.signal(signal.SIGTERM, shutdown)

            thread = threading.Thread(target=director_loop, daemon=True)
            thread.start()
            import uvicorn
            try:
                uvicorn.run(app, host="0.0.0.0", port=args.web_port)
            except KeyboardInterrupt:
                pass
            # Force exit: adapter/websocket threads can keep the process alive on Windows
            os._exit(0)
        except ImportError as e:
            logging.warning("Web server unavailable: %s. Run without --web.", e)

    director.set_run_mode(RUN_MODE_RUNNING)
    interval = 1.0 / max(1.0, config.loop_rate_hz)
    try:
        while director.get_run_mode() != RUN_MODE_STOPPED:
            director.tick()
            import time
            time.sleep(interval)
    except KeyboardInterrupt:
        director.set_run_mode(RUN_MODE_STOPPED)
    if x32:
        x32.stop()
    if pp:
        pp.stop()


if __name__ == "__main__":
    main()
