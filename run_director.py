#!/usr/bin/env python3
"""
Entrypoint to run the Church Auto-Director.

Loads config, creates MultiviewIngest, detector, ATEM control, adapters (X32, ProPresenter, PTZ),
phase machine, and director core. Runs the director loop; optionally start web server with --web.

Record/replay (listen/record mode):
  --record [OUTPUT_JSON]  Capture state (phase, ProPresenter, X32, detector results) at each tick
                          (or --record-interval) and write to JSON. Sync the run with OBS recording
                          of multiview + comms so you get a state file aligned with your video.
  --replay STATE_JSON VIDEO_FILE  Run the director from the state file, synced with the multiview
                          video. No live ATEM/ProPresenter/X32; use this to tweak director behavior
                          and see cuts/decisions against the same video.

Usage:
  python run_director.py [--config config.json] [--web] [--web-port 8000]
  python run_director.py --record [recordings/state.json]
  python run_director.py --replay recordings/state.json path/to/multiview.mp4
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
from config.schema import DirectorConfig, validate_config
from director_core import DirectorCore, RUN_MODE_RUNNING, RUN_MODE_STOPPED
from atem_control import ATEMController, ATEMControllerStub
from phase_machine import PhaseMachine
from state_capture import StateRecorder, load_state_recording, get_state_at_t
from replay_adapters import ReplayProPresenterAdapter, ReplayX32Adapter, ReplayDetector, ReplayATEMStub


def _run_replay_mode(config: DirectorConfig, state_path: Path, video_path: Path) -> None:
    """Run director in replay mode: state from state_path, video from video_path; no ATEM control."""
    import time as _time
    metadata, frames = load_state_recording(state_path)
    duration_sec = metadata.get("duration_sec", 0.0) or (frames[-1]["t"] if frames else 0.0)
    loop_rate_hz = metadata.get("loop_rate_hz", config.loop_rate_hz)
    interval = 1.0 / max(1.0, loop_rate_hz)
    logging.info(
        "Replay: state=%s video=%s duration=%.1fs frames=%s rate=%.1f Hz",
        state_path,
        video_path,
        duration_sec,
        len(frames),
        loop_rate_hz,
    )

    # Replay clock (elapsed seconds).
    replay_t: list = [0.0]

    def get_t() -> float:
        return replay_t[0]

    # Ingest from video file (same layout/capture config as live).
    from multiview_ingest import MultiviewIngest

    ingest = MultiviewIngest(
        source=str(video_path),
        profile_path=config.capture.profile_path,
        width=getattr(config.capture, "width", None),
        height=getattr(config.capture, "height", None),
        inset_ratio=config.capture.inset_ratio,
        debug_frame_path=None,
        debug_multiview_sections_path=None,
        section_to_input_id=config.capture.section_to_input_id,
    )
    detector = ReplayDetector(frames, get_t)
    phase_machine = PhaseMachine(
        phase_ids=config.phases.phase_ids,
        default_phase=config.phases.phase_ids[0] if config.phases.phase_ids else "Intro",
    )
    atem = ReplayATEMStub(frames, get_t)
    pp = ReplayProPresenterAdapter(
        frames,
        get_t,
        playlist_item_to_phase=config.playlist_item_to_phase,
        unmapped_fallback_phase=getattr(config, "unmapped_playlist_item_fallback_phase", None),
    )
    x32 = ReplayX32Adapter(frames, get_t) if config.x32 else None

    director = DirectorCore(
        config=config,
        ingest=ingest,
        detector=detector,
        atem_controller=atem,
        phase_machine=phase_machine,
        x32_adapter=x32,
        propresenter_adapter=pp,
        ptz_adapter=None,
    )
    director.set_run_mode(RUN_MODE_RUNNING)

    try:
        while replay_t[0] <= duration_sec + interval:
            state = get_state_at_t(frames, replay_t[0])
            director.set_replay_phase_override(state.get("phase"))
            ingest.seek_to_time(replay_t[0])
            director.tick()
            replay_t[0] += interval
            _time.sleep(interval)
    except KeyboardInterrupt:
        pass
    finally:
        ingest.release()
    logging.info("Replay finished at t=%.1fs", replay_t[0])


def main():
    parser = argparse.ArgumentParser(description="Run Church Auto-Director")
    parser.add_argument("--config", "-c", default="config.json", help="Path to config JSON")
    parser.add_argument("--web", action="store_true", help="Start web API for control/debug")
    parser.add_argument("--web-port", type=int, default=8000, help="Web server port")
    parser.add_argument(
        "--record",
        metavar="OUTPUT_JSON",
        nargs="?",
        const="",
        default=None,
        help="Record state to OUTPUT_JSON (default: recordings/state_<timestamp>.json). Run with live sources; state is sampled at loop_rate_hz.",
    )
    parser.add_argument(
        "--record-interval",
        type=float,
        default=None,
        help="Seconds between recorded state frames (default: 1/loop_rate_hz).",
    )
    parser.add_argument(
        "--replay",
        nargs=2,
        metavar=("STATE_JSON", "VIDEO_FILE"),
        default=None,
        help="Replay: run director from STATE_JSON synced with VIDEO_FILE (multiview). No ATEM control.",
    )
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

    # Apply configured log level and validate config before starting anything.
    log_level_name = getattr(config, "log_level", "INFO")
    log_level = getattr(logging, str(log_level_name).upper(), logging.INFO)
    logging.basicConfig(level=log_level, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    errors = validate_config(config)
    if errors:
        logging.error("Config validation failed:")
        for err in errors:
            logging.error("  - %s", err)
        sys.exit(1)

    # Replay mode: run director from state file + multiview video (no live ATEM/PP/X32).
    if args.replay is not None:
        state_path = Path(args.replay[0])
        video_path = Path(args.replay[1])
        if not state_path.exists():
            logging.error("Replay state file not found: %s", state_path)
            sys.exit(1)
        if not video_path.exists():
            logging.error("Replay video file not found: %s", video_path)
            sys.exit(1)
        _run_replay_mode(config, state_path, video_path)
        return

    # Ingest and detector (optional: if capture source not available, director still runs with no CV)
    ingest = None
    detector = None
    try:
        from multiview_ingest import MultiviewIngest
        from atem_director import ATEMMultiviewDetector
        ingest = MultiviewIngest(
            source=config.capture.source,
            profile_path=config.capture.profile_path,
            width=getattr(config.capture, "width", None),
            height=getattr(config.capture, "height", None),
            inset_ratio=config.capture.inset_ratio,
            debug_frame_path=config.capture.debug_frame_path,
            debug_multiview_sections_path=config.capture.debug_multiview_sections_path,
            section_to_input_id=config.capture.section_to_input_id,
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
                pastor_dca_index=config.x32.pastor_dca_index,
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
            unmapped_fallback_phase=getattr(config, "unmapped_playlist_item_fallback_phase", None),
        )
        pp.start()
        # Log initial playlist → phase mapping (explicit config + any auto-assigned song phases).
        try:
            pp.log_playlist_mapping_snapshot()
        except Exception:
            logging.debug("Unable to log ProPresenter playlist mapping snapshot on startup", exc_info=True)
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

    # Record mode: capture state each tick (or at record_interval) and write on exit.
    recorder = None
    if args.record is not None:
        out_path = args.record
        if out_path == "":
            from datetime import datetime
            _dir = _ATEM_ROOT / "recordings"
            _dir.mkdir(parents=True, exist_ok=True)
            out_path = _dir / f"state_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        else:
            out_path = Path(out_path)
        record_interval = args.record_interval
        if record_interval is None:
            record_interval = 1.0 / max(1.0, config.loop_rate_hz)
        recorder = StateRecorder(
            output_path=out_path,
            record_interval_sec=record_interval,
            loop_rate_hz=config.loop_rate_hz,
        )
        director.set_state_capture_callback(recorder.on_tick_state)
        recorder.start()
        logging.info("Recording state to %s (interval=%.3fs)", out_path, record_interval)

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
                if recorder is not None:
                    try:
                        recorder.write()
                    except Exception:
                        pass
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
    finally:
        if recorder is not None:
            recorder.write()
    if x32:
        x32.stop()
    if pp:
        pp.stop()


if __name__ == "__main__":
    main()
