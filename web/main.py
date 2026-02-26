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
            "phases_locked_to_role": getattr(cfg, "phases_locked_to_role", {}),
            "input_roles": cfg.input_roles.by_input,
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
    select, input { padding: 0.35rem; margin-left: 0.25rem; }
    .connections { display: flex; gap: 1rem; margin: 0.5rem 0; flex-wrap: wrap; }
    .conn { padding: 0.25rem 0.5rem; border-radius: 4px; }
    .conn.ok { background: #cfc; }
    .conn.fail { background: #fcc; }
    #configValidate { margin-top: 0.5rem; }
    .error { color: #c00; }
    .ok { color: #060; }
    pre { font-size: 0.85rem; overflow: auto; max-height: 200px; }
  </style>
</head>
<body>
  <h1>Church Auto-Director</h1>
  <div class="run-mode">
    <label>Run mode:</label>
    <select id="runMode">
      <option value="stopped">Stopped</option>
      <option value="paused">Paused</option>
      <option value="running">Running</option>
      <option value="rehearsal">Rehearsal</option>
      <option value="manual">Manual</option>
    </select>
    <button id="setMode">Set</button>
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
  </div>
  <h2>Debug</h2>
  <div class="debug-box">
    <p>State and connection status refresh above. Full decision state is shown in the Decision state section.</p>
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
      parts.push('<dt>ProPresenter phase</dt><dd>' + fmt(d.pp_phase) + '</dd>');
      parts.push('<dt>Playlist item</dt><dd>' + fmt(d.pp_item_name) + '</dd>');
      parts.push('<dt>Program input</dt><dd>' + (d.program_input != null ? role(d.program_input) : '—') + '</dd>');
      parts.push('<dt>Candidate</dt><dd>' + (d.candidate != null ? role(d.candidate) : '—') + '</dd>');
      parts.push('<dt>Eligible inputs</dt><dd>' + (Array.isArray(d.eligible) ? d.eligible.map(role).join(', ') : fmt(d.eligible)) + '</dd>');
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
      parts.push('<dt>Cut performed</dt><dd>' + fmt(d.cut_performed) + '</dd>');
      parts.push('</dl>');
      return parts.join('');
    };
    const getState = () => api('/state').then(s => {
      document.getElementById('state').innerHTML = '<dl><dt>Phase</dt><dd>' + s.current_phase + '</dd><dt>Program</dt><dd>' + (s.program_input ?? '—') + '</dd><dt>Last cut</dt><dd>' + (s.last_cut_input ?? '—') + '</dd><dt>Run mode</dt><dd>' + s.run_mode + '</dd></dl>';
      document.getElementById('runMode').value = s.run_mode;
      if (s.decision) document.getElementById('decisionState').innerHTML = renderDecision(s.decision);
      return s;
    });
    const getConnections = () => api('/connections').then(c => {
      const el = document.getElementById('connections');
      el.innerHTML = 'ATEM: <span class="conn ' + (c.atem ? 'ok' : 'fail') + '">' + (c.atem ? 'OK' : 'Disconnected') + '</span> X32: <span class="conn ' + (c.x32 ? 'ok' : 'fail') + '">' + (c.x32 ? 'OK' : '—') + '</span> ProPresenter: <span class="conn ' + (c.propresenter ? 'ok' : 'fail') + '">' + (c.propresenter ? 'OK' : '—') + '</span>';
    });
    const setMode = () => api('/control/run-mode', { method: 'POST', body: JSON.stringify({ mode: document.getElementById('runMode').value }) }).then(() => getState());
    document.getElementById('setMode').onclick = setMode;
    document.getElementById('doForcePhase').onclick = () => api('/control/force-phase', { method: 'POST', body: JSON.stringify({ phase_id: document.getElementById('forcePhase').value || null }) }).then(() => getState());
    document.getElementById('doForceTransition').onclick = () => api('/control/force-transition', { method: 'POST', body: JSON.stringify({ input_id: parseInt(document.getElementById('forceInputId').value, 10) }) }).then(() => getState());
    document.getElementById('validateConfig').onclick = () => api('/config/validate').then(v => {
      const el = document.getElementById('configValidate');
      el.innerHTML = v.valid ? '<span class="ok">Config valid.</span>' : '<span class="error">Errors: ' + (v.errors && v.errors.length ? v.errors.join('; ') : 'unknown') + '</span>';
    });
    getState().then(s => {
      const sel = document.getElementById('forcePhase');
      if (sel.options.length <= 1 && s.phases) { s.phases.forEach(p => { const o = document.createElement('option'); o.value = p; o.textContent = p; sel.appendChild(o); }); }
    });
    api('/config').then(c => {
      document.getElementById('configJson').textContent = JSON.stringify(c, null, 2);
      if (c.input_roles) window._inputRoles = c.input_roles;
    }).catch(() => { document.getElementById('configJson').textContent = 'Failed to load'; });
    getConnections();
    setInterval(getState, 2000);
    setInterval(getConnections, 5000);
  </script>
</body>
</html>
"""
