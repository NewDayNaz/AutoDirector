"""
FastAPI app for director control, config, and debug.

Endpoints: control (run state, pause, resume, rehearsal, manual, force phase/transition),
config (get/validate), debug (state, signals), connection status.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Optional
from dataclasses import asdict, is_dataclass

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

logger = logging.getLogger(__name__)


class RunModeBody(BaseModel):
    mode: str


class ForcePhaseBody(BaseModel):
    phase_id: Optional[str] = None


class ForceTransitionBody(BaseModel):
    input_id: int


class LockInputBody(BaseModel):
    input_id: Optional[int] = None
    timeout_seconds: Optional[float] = None


def create_app(director=None):
    """Create FastAPI app; director is the DirectorCore instance (injected by run_director)."""
    app = FastAPI(title="Church Auto-Director API", version="0.1.0")
    app.state.director = director

    def get_director():
        d = getattr(app.state, "director", None)
        if d is None:
            raise HTTPException(status_code=503, detail="Director not running")
        return d

    @app.get("/")
    def root():
        return HTMLResponse(_INDEX_HTML)

    @app.get("/api/state")
    def api_state():
        d = get_director()
        return d.get_state()

    @app.post("/api/control/run-mode")
    def api_set_run_mode(body: RunModeBody):
        d = get_director()
        d.set_run_mode(body.mode)
        return {"run_mode": d.get_run_mode()}

    @app.get("/api/control/run-mode")
    def api_get_run_mode():
        d = get_director()
        return {"run_mode": d.get_run_mode()}

    @app.post("/api/control/force-phase")
    def api_force_phase(body: ForcePhaseBody):
        d = get_director()
        d.force_phase(body.phase_id)
        return {"phase_override": body.phase_id}

    @app.post("/api/control/force-transition")
    def api_force_transition(body: ForceTransitionBody):
        d = get_director()
        ok = d.force_transition(body.input_id)
        if not ok:
            raise HTTPException(status_code=500, detail="Transition failed")
        return {"input_id": body.input_id}

    @app.get("/api/control/lock-input")
    def api_get_lock_input():
        d = get_director()
        get_lock_state = getattr(d, "get_lock_state", None)
        if callable(get_lock_state):
            try:
                return get_lock_state()
            except Exception as e:
                logger.warning("lock-input get failed: %s", e)
        return {"locked_input": None, "seconds_remaining": None}

    @app.post("/api/control/lock-input")
    def api_set_lock_input(body: LockInputBody):
        d = get_director()
        set_lock = getattr(d, "set_lock_input", None)
        if not callable(set_lock):
            raise HTTPException(status_code=500, detail="Lock-to-input not supported")
        try:
            set_lock(body.input_id, body.timeout_seconds)
        except Exception as e:
            logger.warning("lock-input set failed: %s", e)
            raise HTTPException(status_code=500, detail="Failed to set lock")
        get_lock_state = getattr(d, "get_lock_state", None)
        state: Any = {}
        if callable(get_lock_state):
            try:
                state = get_lock_state()
            except Exception as e:
                logger.warning("lock-input state fetch failed: %s", e)
                state = {}
        return state

    @app.post("/api/control/panic")
    def api_panic():
        """
        Panic: immediately cut to a configured safe input and lock to it.
        """
        d = get_director()
        panic = getattr(d, "panic", None)
        if not callable(panic):
            raise HTTPException(status_code=500, detail="Panic not supported")
        try:
            target = panic()
        except Exception as e:
            logger.warning("panic failed: %s", e)
            raise HTTPException(status_code=500, detail="Panic failed")
        return {"panic_cut_to": target}

    @app.get("/api/config")
    def api_get_config():
        d = get_director()
        cfg = d.config
        return {
            "atem": {"ip": cfg.atem.ip, "use_preview": cfg.atem.use_preview},
            "transition_duration": cfg.transition_duration,
            "backup_input_id": cfg.backup_input_id,
            "backup_timeout_seconds": cfg.backup_timeout_seconds,
            "loop_rate_hz": cfg.loop_rate_hz,
            "phases": cfg.phases.phase_ids,
            "playlist_item_to_phase": cfg.playlist_item_to_phase,
            "unmapped_playlist_item_fallback_phase": getattr(cfg, "unmapped_playlist_item_fallback_phase", None),
            "phases_locked_to_role": getattr(cfg, "phases_locked_to_role", {}),
            "input_roles": cfg.input_roles.by_input,
            "panic_safe_input_id": getattr(cfg, "panic_safe_input_id", None),
            "panic_label": getattr(cfg, "panic_label", None),
            "decision_log_max_entries": getattr(cfg, "decision_log_max_entries", None),
        }

    @app.get("/api/config/validate")
    def api_validate_config():
        try:
            from config.schema import validate_config
        except ImportError:
            from ATEM.config.schema import validate_config
        d = get_director()
        errors = validate_config(d.config)
        return {"valid": len(errors) == 0, "errors": errors}

    @app.get("/api/connections")
    def api_connections():
        d = get_director()
        s = d.get_state()
        return {
            "atem": s.get("atem_connected", False),
            "x32": s.get("x32_has_response", False),
            "propresenter": s.get("pp_connected", False),
        }

    @app.get("/api/health")
    def api_health():
        """
        Lightweight health summary suitable for external monitors.
        Includes run mode, degraded modes, and adapter connection flags.
        """
        d = get_director()
        s = d.get_state()
        return {
            "run_mode": s.get("run_mode"),
            "degraded_modes": s.get("degraded_modes", []),
            "connections": {
                "atem": s.get("atem_connected", False),
                "x32": s.get("x32_has_response", False),
                "propresenter": s.get("pp_connected", False),
            },
        }

    @app.get("/api/metrics")
    def api_metrics():
        """
        Expose basic counters and loop configuration for observability.
        Safe for use by dashboards or simple /metrics scrapers.
        """
        d = get_director()
        get_metrics = getattr(d, "get_metrics", None)
        metrics: Any = {}
        if callable(get_metrics):
            try:
                metrics = get_metrics()
            except Exception as e:
                logger.warning("metrics endpoint failed: %s", e)
                metrics = {}
        return {"metrics": metrics}

    @app.get("/api/decision-log")
    def api_decision_log():
        """
        Return a recent decision log from DirectorCore (bounded length).
        Useful for debugging why specific cuts or backups occurred.
        """
        d = get_director()
        get_log = getattr(d, "get_decision_log", None)
        log: Any = []
        if callable(get_log):
            try:
                log = get_log()
            except Exception as e:
                logger.warning("decision-log endpoint failed: %s", e)
                log = []
        return {"log": log}

    @app.get("/api/config/resolved")
    def api_config_resolved():
        """
        Return the fully-resolved DirectorConfig as JSON (including defaults and derived values).
        Intended for debugging and tooling, not for editing.
        """
        d = get_director()
        cfg = d.config
        if is_dataclass(cfg):
            try:
                return asdict(cfg)
            except Exception as e:
                logger.warning("config/resolved asdict failed: %s", e)
        # Fallback: reuse compact /api/config representation if dataclass conversion fails
        return JSONResponse(status_code=200, content={"error": "failed_to_serialize_full_config"})

    return app


_INDEX_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Auto-Director</title>
  <style>
    body { font-family: system-ui, sans-serif; max-width: 900px; margin: 1rem auto; padding: 0 1rem; }
    h1 { margin-bottom: 0.5rem; }
    h2 { font-size: 1rem; margin: 1rem 0 0.5rem; }
    .state, .config-box, .debug-box { background: #f0f0f0; padding: 0.75rem; border-radius: 6px; margin: 0.5rem 0; }
    .state dt { font-weight: 600; margin-top: 0.25rem; }
    .state dd { margin-left: 0; }
    button { margin: 0.25rem; padding: 0.35rem 0.75rem; cursor: pointer; }
    .run-mode { margin: 0.5rem 0; }
    .run-mode-row { display: flex; align-items: center; gap: 0.75rem; flex-wrap: wrap; }
    .run-mode-group { display: inline-flex; align-items: center; gap: 0.25rem; flex-wrap: wrap; }
    .run-mode-btn.run-mode-active { background: #0366d6; color: #fff; }
    .panic { background: #c00; color: #fff; }
    .lock-control { display: flex; align-items: center; gap: 0.25rem; margin-top: 0.5rem; flex-wrap: wrap; }
    #lockStatus { font-size: 0.85rem; }
    select, input { padding: 0.35rem; margin-left: 0.25rem; }
    .connections { display: flex; gap: 1rem; margin: 0.5rem 0; flex-wrap: wrap; }
    .conn { padding: 0.25rem 0.5rem; border-radius: 4px; }
    .conn.ok { background: #cfc; }
    .conn.fail { background: #fcc; }
    #configValidate { margin-top: 0.5rem; }
    .error { color: #c00; }
    .ok { color: #060; }
    pre { font-size: 0.85rem; overflow: auto; max-height: 200px; }
    .timeline-table { width: 100%; border-collapse: collapse; font-size: 0.85rem; }
    .timeline-table th, .timeline-table td { padding: 0.15rem 0.25rem; border-bottom: 1px solid #ddd; text-align: left; }
    .timeline-table th { font-weight: 600; }
  </style>
</head>
<body>
  <h1>Church Auto-Director</h1>
  <div class="run-mode">
    <div class="run-mode-row">
      <div class="run-mode-group">
        <span>Run mode:</span>
        <button class="run-mode-btn" data-mode="stopped">Stopped</button>
        <button class="run-mode-btn" data-mode="paused">Paused</button>
        <button class="run-mode-btn" data-mode="running">Running</button>
        <button class="run-mode-btn" data-mode="rehearsal">Rehearsal</button>
        <button class="run-mode-btn" data-mode="manual">Manual</button>
      </div>
      <button id="panicButton" class="panic">Panic safe shot</button>
    </div>
    <div class="lock-control">
      <label>Lock to input:</label>
      <input type="number" id="lockInputId" min="1" max="20" value="" style="width:4ch">
      <button id="lockSet">Lock</button>
      <button id="lockClear">Clear</button>
      <span id="lockStatus">Not locked</span>
    </div>
    <!-- Hidden legacy controls kept for backward compatibility and as a fallback. -->
    <select id="runMode" style="display:none">
      <option value="stopped">Stopped</option>
      <option value="paused">Paused</option>
      <option value="running">Running</option>
      <option value="rehearsal">Rehearsal</option>
      <option value="manual">Manual</option>
    </select>
    <button id="setMode" style="display:none">Set</button>
  </div>
  <div class="connections" id="connections">Connections: loading...</div>
  <div class="state" id="state">State: loading...</div>
  <h2>Decision state</h2>
  <div class="debug-box" id="decisionState">No decision data yet (director must tick at least once).</div>
  <div>
    <label>Force phase:</label>
    <select id="forcePhase"><option value="">(clear override)</option></select>
    <button id="doForcePhase">Set phase</button>
  </div>
  <div>
    <label>Force transition to input:</label>
    <input type="number" id="forceInputId" min="1" max="20" value="1" style="width:4ch">
    <button id="doForceTransition">Cut</button>
  </div>
  <h2>Config</h2>
  <div class="config-box">
    <button id="validateConfig">Validate config</button>
    <div id="configValidate"></div>
    <details><summary>Current config (read-only)</summary><pre id="configJson">Loading...</pre></details>
    <details><summary>Resolved DirectorConfig (full, read-only)</summary><pre id="configResolvedJson">Loading...</pre></details>
    <button id="reloadResolvedConfig">Reload resolved config</button>
  </div>
  <h2>Debug</h2>
  <div class="debug-box">
    <p>State and connection status refresh above. Full decision state is shown in the Decision state section.</p>
  </div>
  <h2>Decision timeline</h2>
  <div class="debug-box">
    <div id="decisionTimeline">Loading decision timeline…</div>
  </div>
  <h2>Metrics</h2>
  <div class="debug-box">
    <div id="metricsBox">Loading metrics…</div>
  </div>
  <script>
    const api = (path, opts = {}) => fetch('/api' + path, { headers: { 'Content-Type': 'application/json' }, ...opts }).then(r => r.json());
    const fmt = (v) => v === null || v === undefined ? '—' : (typeof v === 'boolean' ? (v ? 'yes' : 'no') : String(v));
    const renderDecision = (d) => {
      if (!d || typeof d !== 'object') return 'No decision data yet.';
      const role = (id) => (window._inputRoles && window._inputRoles[String(id)]) ? id + ' (' + window._inputRoles[String(id)] + ')' : id;
      const parts = [];
      parts.push('<dl class="state">');
      parts.push('<dt>Phase</dt><dd>' + fmt(d.phase) + '</dd>');
      parts.push('<dt>Phase source</dt><dd>' + fmt(d.phase_source) + '</dd>');
      if (d.manual_phase_override) {
        parts.push('<dt>Manual phase override</dt><dd>' + fmt(d.manual_phase_override) + '</dd>');
      }
      if (d.external_phase_override) {
        parts.push('<dt>External phase override</dt><dd>' + fmt(d.external_phase_override) + (d.external_phase_reason ? ' (' + fmt(d.external_phase_reason) + ')' : '') + '</dd>');
      }
      parts.push('<dt>ProPresenter phase</dt><dd>' + fmt(d.pp_phase) + '</dd>');
      parts.push('<dt>Playlist item</dt><dd>' + fmt(d.pp_item_name) + '</dd>');
      parts.push('<dt>Program input</dt><dd>' + (d.program_input != null ? role(d.program_input) : '—') + '</dd>');
      parts.push('<dt>Candidate</dt><dd>' + (d.candidate != null ? role(d.candidate) : '—') + '</dd>');
      let eligibleText;
      if (Array.isArray(d.eligible)) {
        eligibleText = d.eligible.map(role).join(', ');
      } else {
        eligibleText = fmt(d.eligible);
      }
      parts.push('<dt>Eligible inputs</dt><dd>' + eligibleText + '</dd>');
      parts.push('<dt>Inputs with person</dt><dd>' + (Array.isArray(d.inputs_with_people) ? d.inputs_with_people.join(', ') : fmt(d.inputs_with_people)) + '</dd>');
      parts.push('<dt>Segment inputs</dt><dd>' + (Array.isArray(d.segment_input_ids) ? d.segment_input_ids.join(', ') : fmt(d.segment_input_ids)) + '</dd>');
      parts.push('<dt>Roamer stable</dt><dd>' + fmt(d.roamer_stable) + '</dd>');
      parts.push('<dt>Band muted</dt><dd>' + fmt(d.band_muted) + '</dd>');
      parts.push('<dt>ProPresenter level</dt><dd>' + fmt(d.pp_level) + '</dd>');
      parts.push('<dt>Program OK</dt><dd>' + fmt(d.program_ok) + '</dd>');
      if (d.program_bad_reason) parts.push('<dt>Program bad reason</dt><dd>' + fmt(d.program_bad_reason) + '</dd>');
      parts.push('<dt>Block reason</dt><dd>' + (d.block_reason ? fmt(d.block_reason) : '<em>none (cut allowed)</em>') + '</dd>');
      if (d.seconds_on_shot != null) parts.push('<dt>Seconds on shot</dt><dd>' + Number(d.seconds_on_shot).toFixed(1) + 's</dd>');
      if (d.min_seconds_on_shot != null) parts.push('<dt>Min seconds on shot</dt><dd>' + d.min_seconds_on_shot + 's</dd>');
      if (d.dwell_elapsed_sec != null) parts.push('<dt>Dwell elapsed</dt><dd>' + Number(d.dwell_elapsed_sec).toFixed(1) + 's</dd>');
      if (d.dwell_required_sec != null) parts.push('<dt>Dwell required</dt><dd>' + d.dwell_required_sec + 's</dd>');
      if (d.dwell_target_input != null) parts.push('<dt>Dwell target input</dt><dd>' + role(d.dwell_target_input) + '</dd>');
      if (d.backup_seconds_since_good != null) parts.push('<dt>Backup: seconds since good</dt><dd>' + Number(d.backup_seconds_since_good).toFixed(1) + 's</dd>');
      if (d.backup_timeout_seconds != null) parts.push('<dt>Backup timeout</dt><dd>' + d.backup_timeout_seconds + 's</dd>');
      if (Array.isArray(d.degraded_modes) && d.degraded_modes.length) {
        parts.push('<dt>Degraded modes</dt><dd>' + d.degraded_modes.join(', ') + '</dd>');
      }
      parts.push('<dt>Cut performed</dt><dd>' + fmt(d.cut_performed) + '</dd>');
      parts.push('</dl>');
      return parts.join('');
    };

    const renderLockState = (st) => {
      const el = document.getElementById('lockStatus');
      if (!el) return;
      if (!st || (!st.locked_input && st.locked_input !== 0)) {
        el.textContent = 'Not locked';
        return;
      }
      const remaining = typeof st.seconds_remaining === 'number' ? st.seconds_remaining : null;
      const secs = remaining != null ? Math.max(0, remaining).toFixed(1) : '∞';
      el.textContent = 'LOCKED to input ' + st.locked_input + ' (' + secs + 's)';
    };

    const renderTimeline = (log) => {
      const el = document.getElementById('decisionTimeline');
      if (!el) return;
      if (!log || !Array.isArray(log) || !log.length) {
        el.textContent = 'No recent decisions yet.';
        return;
      }
      const rows = [];
      rows.push('<table class="timeline-table"><thead><tr><th>#</th><th>Age (s)</th><th>Phase</th><th>Candidate</th><th>Reason</th><th>Cut</th></tr></thead><tbody>');
      const role = (id) => (window._inputRoles && window._inputRoles[String(id)]) ? id + ' (' + window._inputRoles[String(id)] + ')' : id;
      log.slice(-100).reverse().forEach((entry, idx) => {
        const age = typeof entry.age_sec === 'number' ? entry.age_sec.toFixed(1) : '—';
        const phase = fmt(entry.phase);
        const cand = entry.candidate != null ? role(entry.candidate) : '—';
        const reason = entry.block_reason ? fmt(entry.block_reason) : (entry.program_ok ? 'cut_allowed' : '—');
        const cut = fmt(entry.cut_performed);
        rows.push('<tr><td>' + (idx + 1) + '</td><td>' + age + '</td><td>' + phase + '</td><td>' + cand + '</td><td>' + reason + '</td><td>' + cut + '</td></tr>');
      });
      rows.push('</tbody></table>');
      el.innerHTML = rows.join('');
    };

    const getState = () => api('/state').then(s => {
      document.getElementById('state').innerHTML = '<dl><dt>Phase</dt><dd>' + s.current_phase + '</dd><dt>Program</dt><dd>' + (s.program_input ?? '—') + '</dd><dt>Last cut</dt><dd>' + (s.last_cut_input ?? '—') + '</dd><dt>Run mode</dt><dd>' + s.run_mode + '</dd></dl>';
      document.getElementById('runMode').value = s.run_mode;
      const btns = Array.from(document.querySelectorAll('.run-mode-btn'));
      btns.forEach(b => {
        b.classList.toggle('run-mode-active', b.dataset.mode === s.run_mode);
      });
      if (s.program_input != null) {
        const lockInput = document.getElementById('lockInputId');
        if (lockInput && !lockInput.value) {
          lockInput.value = s.program_input;
        }
      }
      if (s.decision) document.getElementById('decisionState').innerHTML = renderDecision(s.decision);
      return s;
    });
    const getConnections = () => api('/connections').then(c => {
      const el = document.getElementById('connections');
      el.innerHTML = 'ATEM: <span class="conn ' + (c.atem ? 'ok' : 'fail') + '">' + (c.atem ? 'OK' : 'Disconnected') + '</span> X32: <span class="conn ' + (c.x32 ? 'ok' : 'fail') + '">' + (c.x32 ? 'OK' : '—') + '</span> ProPresenter: <span class="conn ' + (c.propresenter ? 'ok' : 'fail') + '">' + (c.propresenter ? 'OK' : '—') + '</span>';
    });
    const getMetrics = () => api('/metrics').then(m => {
      const box = document.getElementById('metricsBox');
      if (!m || !m.metrics) {
        box.textContent = 'No metrics yet.';
        return;
      }
      const mm = m.metrics;
      const lines = [];
      lines.push('Cut count: ' + (mm.cut_count ?? 0));
      lines.push('Backup cuts: ' + (mm.backup_cut_count ?? 0));
      lines.push('Bad-program events: ' + (mm.bad_program_events ?? 0));
      if (mm.loop_rate_hz != null) {
        lines.push('Loop rate (Hz): ' + mm.loop_rate_hz);
      }
      box.textContent = lines.join('\\n');
    }).catch(() => {
      const box = document.getElementById('metricsBox');
      box.textContent = 'Failed to load metrics.';
    });

    const setMode = (mode) => {
      const payload = { mode: mode || document.getElementById('runMode').value };
      return api('/control/run-mode', { method: 'POST', body: JSON.stringify(payload) }).then(() => getState());
    };

    document.getElementById('setMode').onclick = () => setMode();
    Array.from(document.querySelectorAll('.run-mode-btn')).forEach(btn => {
      btn.onclick = () => setMode(btn.dataset.mode);
    });

    const panicBtn = document.getElementById('panicButton');
    if (panicBtn) {
      panicBtn.onclick = () => {
        api('/control/panic', { method: 'POST', body: JSON.stringify({}) }).then(() => {
          getState();
        });
      };
    }

    const getLockState = () => api('/control/lock-input').then(st => renderLockState(st)).catch(() => {
      const el = document.getElementById('lockStatus');
      if (el) el.textContent = 'Lock status unavailable';
    });

    const lockSet = document.getElementById('lockSet');
    if (lockSet) {
      lockSet.onclick = () => {
        const inputEl = document.getElementById('lockInputId');
        const v = inputEl ? parseInt(inputEl.value, 10) : NaN;
        if (!Number.isFinite(v) || v <= 0) {
          return;
        }
        api('/control/lock-input', { method: 'POST', body: JSON.stringify({ input_id: v }) }).then(st => renderLockState(st));
      };
    }

    const lockClear = document.getElementById('lockClear');
    if (lockClear) {
      lockClear.onclick = () => {
        api('/control/lock-input', { method: 'POST', body: JSON.stringify({ input_id: null }) }).then(st => renderLockState(st));
      };
    }

    document.getElementById('doForcePhase').onclick = () => api('/control/force-phase', { method: 'POST', body: JSON.stringify({ phase_id: document.getElementById('forcePhase').value || null }) }).then(() => getState());
    document.getElementById('doForceTransition').onclick = () => api('/control/force-transition', { method: 'POST', body: JSON.stringify({ input_id: parseInt(document.getElementById('forceInputId').value, 10) }) }).then(() => getState());

    document.getElementById('validateConfig').onclick = () => api('/config/validate').then(v => {
      const el = document.getElementById('configValidate');
      el.innerHTML = v.valid ? '<span class="ok">Config valid.</span>' : '<span class="error">Errors: ' + (v.errors && v.errors.length ? v.errors.join('; ') : 'unknown') + '</span>';
    });

    const loadResolvedConfig = () => api('/config/resolved').then(rc => {
      document.getElementById('configResolvedJson').textContent = JSON.stringify(rc, null, 2);
    }).catch(() => {
      document.getElementById('configResolvedJson').textContent = 'Failed to load';
    });

    const reloadResolved = document.getElementById('reloadResolvedConfig');
    if (reloadResolved) {
      reloadResolved.onclick = () => loadResolvedConfig();
    }

    getState().then(s => {
      const sel = document.getElementById('forcePhase');
      if (sel && sel.options.length <= 1 && s.phases) {
        s.phases.forEach(p => {
          const o = document.createElement('option');
          o.value = p;
          o.textContent = p;
          sel.appendChild(o);
        });
      }
    });

    api('/config').then(c => {
      document.getElementById('configJson').textContent = JSON.stringify(c, null, 2);
      if (c.input_roles) window._inputRoles = c.input_roles;
      const panicBtnInner = document.getElementById('panicButton');
      if (panicBtnInner && c.panic_label) {
        panicBtnInner.textContent = c.panic_label;
      }
    }).catch(() => { document.getElementById('configJson').textContent = 'Failed to load'; });

    loadResolvedConfig();

    const getDecisionLog = () => api('/decision-log').then(d => {
      renderTimeline(d.log || []);
    }).catch(() => {
      const el = document.getElementById('decisionTimeline');
      if (el) el.textContent = 'Failed to load decision timeline.';
    });

    getConnections();
    getMetrics();
    getLockState();
    getDecisionLog();

    setInterval(getState, 2000);
    setInterval(getConnections, 5000);
    setInterval(getMetrics, 10000);
    setInterval(getLockState, 2000);
    setInterval(getDecisionLog, 3000);
  </script>
</body>
</html>
"""
