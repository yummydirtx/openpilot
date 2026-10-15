from openpilot.cereal import custom, log
from opendbc.car import structs
from openpilot.common.test import OpenpilotTestCase
from openpilot.sunnypilot.mads.helpers import MadsSteeringModeOnBrake
from openpilot.sunnypilot.mads.tests.test_mads_steering_mode import make_car_state, make_mads

ButtonType = structs.CarState.ButtonEvent.Type
EventName = log.OnroadEvent.EventName
EventNameSP = custom.OnroadEventSP.EventName
State = custom.ModularAssistiveDrivingSystem.ModularAssistiveDrivingSystemState


def on_press(pressed=True, **kwargs):
  cs = make_car_state(**kwargs)
  cs.buttonEvents = [structs.CarState.ButtonEvent(type=ButtonType.mainCruiseOn, pressed=pressed)]
  return cs


class TestMainCruiseOn(OpenpilotTestCase):
  def _mads(self, mocker, mode=MadsSteeringModeOnBrake.REMAIN_ACTIVE):
    mads, sd = make_mads(mocker, mode)
    mads.main_enabled_toggle = True
    return mads, sd

  def test_standby_waits_for_explicit_on(self, mocker):
    mads, sd = self._mads(mocker)
    mads.update(make_car_state())
    assert not mads.enabled
    mads.update(on_press())
    assert mads.enabled and mads.active
    assert not sd.enabled
    assert not sd.events.has(EventName.pcmEnable)
    assert not sd.events.has(EventName.buttonEnable)

  def test_on_is_enable_only(self, mocker):
    mads, sd = self._mads(mocker)
    mads.update(on_press())
    sd.events_sp.clear()
    mads.update(on_press())
    assert mads.enabled
    assert not sd.events_sp.has(EventNameSP.lkasEnable)
    assert not sd.events_sp.has(EventNameSP.lkasDisable)

  def test_release_and_unrelated_buttons_do_not_engage(self, mocker):
    for button in (ButtonType.mainCruiseOn, ButtonType.mainCruise, ButtonType.cancel):
      mads, _ = self._mads(mocker)
      cs = on_press(pressed=button != ButtonType.mainCruiseOn)
      cs.buttonEvents[0].type = button
      mads.update(cs)
      assert not mads.enabled

  def test_requires_standby(self, mocker):
    for available, cruise_enabled in ((False, False), (True, True)):
      mads, sd = self._mads(mocker)
      cs = on_press()
      cs.cruiseState.available = available
      cs.cruiseState.enabled = cruise_enabled
      sd.CS_prev = cs
      mads.update(cs)
      assert not mads.enabled

  def test_respects_both_engagement_settings(self, mocker):
    for mads_allowed, main_allowed in ((False, True), (True, False)):
      mads, _ = self._mads(mocker)
      mads.enabled_toggle = mads_allowed
      mads.main_enabled_toggle = main_allowed
      mads.update(on_press())
      assert not mads.enabled

  def test_main_off_still_disengages(self, mocker):
    mads, sd = self._mads(mocker)
    mads.update(on_press())
    sd.events_sp.clear()
    cs = make_car_state()
    cs.cruiseState.available = False
    mads.update(cs)
    assert not mads.enabled

  def test_no_entry_still_blocks_engagement(self, mocker):
    mads, sd = self._mads(mocker)
    sd.events.add(EventName.commIssue)
    mads.update(on_press())
    assert not mads.enabled

  def test_pause_on_brake_waits_for_release(self, mocker):
    for brake, regen in ((True, False), (False, True)):
      mads, sd = self._mads(mocker, MadsSteeringModeOnBrake.PAUSE)
      mads.update(on_press(brake_pressed=brake, regen_braking=regen, standstill=True))
      assert mads.enabled and not mads.active
      assert mads.state_machine.state == State.paused
      sd.events_sp.clear()
      mads.update(make_car_state(standstill=True))
      assert mads.enabled and mads.active

  def test_disengage_on_brake_blocks_request(self, mocker):
    mads, sd = self._mads(mocker, MadsSteeringModeOnBrake.DISENGAGE)
    sd.events.add(EventName.pedalPressed)
    mads.update(on_press(brake_pressed=True, v_ego=10.))
    assert not mads.enabled
    assert not sd.events_sp.has(EventNameSP.lkasEnable)
