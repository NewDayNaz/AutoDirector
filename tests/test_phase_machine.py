from config.schema import PHASE_IDS
from phase_machine import PhaseMachine


def test_phase_defaults_to_first_phase():
    pm = PhaseMachine(phase_ids=list(PHASE_IDS))
    assert pm.current_phase == list(PHASE_IDS)[0]


def test_manual_override_takes_precedence():
    pm = PhaseMachine(phase_ids=list(PHASE_IDS))
    pm.set_manual_override("Sermon")
    pm.update(propresenter_phase="Band1")
    assert pm.current_phase == "Sermon"
    assert pm.phase_source == "manual_override"


def test_external_override_used_when_no_manual():
    pm = PhaseMachine(phase_ids=list(PHASE_IDS))
    pm.set_external_override("Sermon", reason="test")
    pm.update(propresenter_phase="Band1")
    assert pm.current_phase == "Sermon"
    assert pm.phase_source == "external_override"

