from types import SimpleNamespace

from config.schema import DirectorConfig, PhaseConfig, InputRolesConfig
from director_core import DirectorCore


class _FakeATEM:
    def __init__(self) -> None:
        self._program = 1
        self.is_connected = True
        self.cuts = []

    def get_program_input(self):
        return self._program

    def cut_to_input(self, input_id: int, _duration: float) -> bool:
        self._program = input_id
        self.cuts.append(input_id)
        return True

    def connect(self):
        self.is_connected = True


class _FakePhaseMachine:
    def __init__(self, phase_ids):
        self.phase_ids = phase_ids
        self.current_phase = phase_ids[0]
        self._manual_override = None
        self._external_override = None
        self._external_reason = None
        self.on_phase_changed = None

    def set_manual_override(self, phase_id):
        self._manual_override = phase_id

    def get_manual_override(self):
        return self._manual_override

    def set_external_override(self, phase_id, reason=None):
        self._external_override = phase_id
        self._external_reason = reason

    def get_external_override(self):
        return self._external_override

    def get_external_override_reason(self):
        return self._external_reason

    def update(self, propresenter_phase=None):
        previous = self.current_phase
        if self._manual_override is not None:
            self.current_phase = self._manual_override
        elif self._external_override is not None:
            self.current_phase = self._external_override
        elif propresenter_phase is not None and propresenter_phase in self.phase_ids:
            self.current_phase = propresenter_phase
        if self.on_phase_changed and self.current_phase != previous:
            self.on_phase_changed(previous, self.current_phase)
        return self.current_phase


class _FakeX32:
    def __init__(self, level: float = 0.0) -> None:
        self._level = level

    def get_propresenter_level(self) -> float:
        return self._level

    def is_band_muted(self):
        return None

    @property
    def has_recent_response(self) -> bool:
        return True


class _FakeProPresenter:
    def __init__(self, item_name: str, slide_type: str | None, stage_layout: str | None):
        self._item_name = item_name
        self._slide_type = slide_type
        self._stage_layout = stage_layout
        self._phase = "Sermon"
        self.is_connected = True

    def get_current_phase(self):
        return self._phase

    def poll(self):
        return None

    def get_current_item_name(self):
        return self._item_name

    def get_slide_type(self):
        return self._slide_type

    def get_stage_display_layout(self):
        return self._stage_layout


def _make_config() -> DirectorConfig:
    phases = PhaseConfig(
        phase_ids=["Intro", "BumperSermon", "Sermon"],
    )
    input_roles = InputRolesConfig(by_input={1: "cg"})
    cfg = DirectorConfig(
        atem=DirectorConfig.atem,  # type: ignore[assignment]
        capture=DirectorConfig.capture,  # type: ignore[assignment]
        input_roles=input_roles,
        phases=phases,
    )
    cfg.phases_locked_to_role = {"BumperSermon": "cg"}
    cfg.playlist_item_to_phase = {"Sermon": "Sermon"}
    cfg.backup_input_id = 1
    cfg.transition_duration = 0.25
    return cfg


def test_sermon_bumper_forces_bumpersermon_phase_and_cg_input(monkeypatch):
    cfg = _make_config()
    atem = _FakeATEM()
    phase_machine = _FakePhaseMachine(cfg.phases.phase_ids)
    pp = _FakeProPresenter(
        item_name="Sermon",
        slide_type="video",
        stage_layout="SERMON VIDEO",
    )
    x32 = _FakeX32(level=0.8)
    director = DirectorCore(
        config=cfg,
        ingest=None,
        detector=None,
        atem_controller=atem,
        phase_machine=phase_machine,
        x32_adapter=x32,
        propresenter_adapter=pp,
        ptz_adapter=None,
    )

    # Speed up tests by forcing time.monotonic() to advance quickly.
    import time as _time

    base = _time.monotonic()

    def fake_monotonic():
        return fake_monotonic.value

    fake_monotonic.value = base
    monkeypatch.setattr("time.monotonic", fake_monotonic)

    # First few ticks: bumper detection hysteresis window.
    for _ in range(5):
        fake_monotonic.value += 0.2
        director.tick()

    assert phase_machine.current_phase == "BumperSermon"
    assert atem.get_program_input() == 1

