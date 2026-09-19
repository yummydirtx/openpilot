import ast
from dataclasses import FrozenInstanceError
import itertools
from pathlib import Path
from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock, patch

from tools.android_auto.live_state import (
  BASE_SERVICES, SUNNYPILOT_SERVICES, LiveStateAdapter, LiveStateReader, display_status,
)


class FakeSubMaster:
  def __init__(self, now=100.0):
    self.services = BASE_SERVICES + SUNNYPILOT_SERVICES
    self.seen = dict.fromkeys(self.services, True)
    self.alive = dict.fromkeys(self.services, True)
    self.valid = dict.fromkeys(self.services, True)
    self.updated = dict.fromkeys(self.services, True)
    self.recv_time = dict.fromkeys(self.services, now)
    self.logMonoTime = dict.fromkeys(self.services, round(now * 1e9))
    self.data = {
      "carState": NS(vEgo=20.0, vEgoCluster=21.0, vCruiseCluster=100.0),
      "controlsState": NS(deprecated=NS(vCruise=90.0)),
      "selfdriveState": NS(enabled=True, state="enabled", alertSize="none", alertStatus="normal", alertText1="", alertText2=""),
      "selfdriveStateSP": NS(mads=NS(available=True, enabled=True, state="enabled")),
      "onroadEvents": [],
      "deviceState": NS(started=True),
      "pandaStates": [NS(pandaType="tres", ignitionCan=True, ignitionLine=True)],
    }
    self.update = Mock()

  def __getitem__(self, key):
    return self.data[key]

  def tick(self, now):
    self.updated = dict.fromkeys(self.services, True)
    self.recv_time = dict.fromkeys(self.services, now)
    self.logMonoTime = dict.fromkeys(self.services, round(now * 1e9))


class TestLiveState(unittest.TestCase):
  def setUp(self):
    self.sm = FakeSubMaster()
    self.adapter = LiveStateAdapter()

  def snapshot(self, now=100.0, **kwargs):
    return self.adapter.snapshot(self.sm, now=now, **kwargs)

  def test_live_immutable_snapshot_uses_native_units_and_setpoint(self):
    state = self.snapshot()
    self.assertFalse(state.stale)
    self.assertEqual(state.source, "live")
    self.assertEqual(state.speed_text, "47")
    self.assertEqual(state.unit_text, "mph")
    self.assertAlmostEqual(state.set_speed, 62.1371)
    self.assertEqual(state.display_status, "engaged")
    metric = self.snapshot(is_metric=True)
    self.assertEqual(metric.speed_text, "76")
    self.assertEqual(metric.set_speed, 100)
    with self.assertRaises(FrozenInstanceError):
      state.status = "override"
    self.sm["carState"].vEgoCluster = 1
    self.assertEqual(state.speed_text, "47")

  def test_zero_cluster_falls_back_until_cluster_seen_then_zero_is_zero(self):
    self.sm["carState"].vEgoCluster = 0
    self.assertEqual(self.snapshot(is_metric=True).speed, 72)
    self.sm["carState"].vEgoCluster = 21
    self.snapshot()
    self.sm["carState"].vEgoCluster = 0
    self.assertEqual(self.snapshot().speed, 0)
    self.assertEqual(self.snapshot(is_metric=True, true_speed=True).speed, 72)

  def test_setpoint_fallback_and_unavailable_sentinels(self):
    self.sm["carState"].vCruiseCluster = 0
    self.assertEqual(self.snapshot(is_metric=True).set_speed, 90)
    for value in (0, -1, 255):
      self.sm["controlsState"].deprecated.vCruise = value
      state = self.snapshot()
      self.assertIsNone(state.set_speed)
      self.assertFalse(state.is_cruise_set)
      self.assertEqual(state.is_cruise_available, value != -1)

  def test_fallback_requires_fresh_controls_but_real_setpoint_does_not(self):
    self.sm.alive["controlsState"] = False
    self.assertFalse(self.snapshot().stale)
    self.sm["carState"].vCruiseCluster = 0
    state = self.snapshot()
    self.assertTrue(state.stale)
    self.assertEqual(state.missing_services, ("controlsState",))
    self.assertEqual(state.speed_text, "–")

  def test_missing_invalid_dead_and_old_high_rate_messages_remove_engagement(self):
    for name in ("carState", "selfdriveState", "selfdriveStateSP", "pandaStates"):
      for failure in ("seen", "alive", "valid", "recv_time", "logMonoTime"):
        with self.subTest(service=name, failure=failure):
          self.setUp()
          self.snapshot()
          mapping = getattr(self.sm, failure)
          mapping[name] = 99.49 if failure == "recv_time" else 99_490_000_000 if failure == "logMonoTime" else False
          state = self.snapshot()
          self.assertTrue(state.stale)
          self.assertIsNone(state.speed)
          self.assertIsNone(state.set_speed)
          self.assertEqual(state.display_status, "disengaged")
          self.assertIn(name, state.missing_services)

  def test_500ms_deadline_and_recovery(self):
    self.snapshot()
    self.assertFalse(self.snapshot(now=100.5).stale)
    self.assertTrue(self.snapshot(now=100.501).stale)
    self.sm.tick(101.0)
    self.assertFalse(self.snapshot(now=101.0).stale)

  def test_early_stale_threshold_reserves_rendering_budget(self):
    self.adapter = LiveStateAdapter(stale_after=0.35)
    self.snapshot()
    self.sm.tick(100.34)
    self.sm.logMonoTime["carState"] = 100_000_000_000
    self.assertFalse(self.snapshot(now=100.34).stale)
    self.sm.tick(100.36)
    self.sm.logMonoTime["carState"] = 100_000_000_000
    state = self.snapshot(now=100.36)
    self.assertTrue(state.started)
    self.assertTrue(state.stale)
    self.assertEqual(state.missing_services, ("carState",))
    self.assertEqual(state.speed_text, "–")
    self.assertEqual(state.display_status, "disengaged")
    self.assertLess(state.age_seconds, 0.5)

  def test_early_stale_threshold_preserves_slow_topic_deadlines(self):
    self.adapter = LiveStateAdapter(stale_after=0.35)
    self.snapshot()
    self.sm.tick(101)
    self.sm.logMonoTime["deviceState"] = 100_000_000_000
    self.sm.logMonoTime["onroadEvents"] = 99_000_000_000
    self.assertFalse(self.snapshot(now=101).stale)
    self.assertEqual(self.adapter.deadlines["pandaStates"], 0.35)
    self.assertEqual(self.adapter.deadlines["controlsState"], 0.35)
    self.assertEqual(LiveStateAdapter().deadlines["carState"], 0.5)

  def test_stale_threshold_cannot_disable_or_extend_freshness_guard(self):
    for threshold in (0, -0.1, 0.501, float("nan"), float("inf")):
      with self.subTest(stale_after=threshold):
        with self.assertRaisesRegex(ValueError, "stale_after"):
          LiveStateAdapter(stale_after=threshold)
        # Configuration validation happens before any Cereal socket import.
        with self.assertRaisesRegex(ValueError, "stale_after"):
          LiveStateReader(stale_after=threshold)

  def test_reader_applies_early_threshold_to_legacy_cruise_fallback(self):
    self.sm["carState"].vCruiseCluster = 0
    params = Mock(spec=["get_bool"])
    params.get_bool.return_value = False
    reader = LiveStateReader(sm=self.sm, params=params, stale_after=0.35)
    with patch("tools.android_auto.live_state.time.monotonic", return_value=100):
      self.assertFalse(reader.poll().stale)
    self.sm.tick(100.36)
    self.sm.logMonoTime["controlsState"] = 100_000_000_000
    with patch("tools.android_auto.live_state.time.monotonic", return_value=100.36):
      state = reader.poll()
    self.assertTrue(state.stale)
    self.assertEqual(state.missing_services, ("controlsState",))

  def test_slow_topic_cadences_do_not_blink_stale(self):
    self.snapshot()
    self.sm.tick(101.0)
    for name, age in (("deviceState", 0.9), ("onroadEvents", 1.8)):
      self.sm.recv_time[name] = 101.0 - age
      self.sm.logMonoTime[name] = round((101.0 - age) * 1e9)
    self.assertFalse(self.snapshot(now=101.0).stale)
    self.sm.logMonoTime["onroadEvents"] = 98_000_000_000
    self.assertTrue(self.snapshot(now=101.0).stale)

  def test_old_published_message_cannot_be_refreshed_by_receive_time(self):
    self.snapshot()
    self.sm.tick(103.0)
    self.sm.logMonoTime["selfdriveState"] = 100_000_000_000
    self.assertIn("selfdriveState", self.snapshot(now=103.0).missing_services)

  def test_offroad_and_unknown_panda_do_not_show_active_hud(self):
    self.sm["deviceState"].started = False
    self.assertTrue(self.snapshot().stale)
    self.sm["deviceState"].started = True
    self.sm["pandaStates"][0].pandaType = "unknown"
    self.assertFalse(self.snapshot().started)

  def test_onroad_transition_rejects_previous_drive_messages(self):
    self.sm["deviceState"].started = False
    self.snapshot()
    self.sm.tick(100.1)
    self.sm["deviceState"].started = True
    self.sm.recv_time["carState"] = 100
    self.assertIn("carState", self.snapshot(now=100.1).missing_services)
    self.sm.tick(100.2)
    self.assertFalse(self.snapshot(now=100.2).stale)

  def test_ignition_can_hold_has_native_five_second_bound(self):
    self.assertTrue(self.snapshot().started)
    self.sm["pandaStates"][0].ignitionCan = False
    self.sm.tick(104.9)
    self.assertTrue(self.snapshot(now=104.9).started)
    self.sm.tick(105)
    self.assertFalse(self.snapshot(now=105).started)

  def test_line_only_ignition_is_supported(self):
    self.sm["pandaStates"][0].ignitionCan = False
    self.assertTrue(self.snapshot().started)
    self.sm.tick(120)
    self.assertTrue(self.snapshot(now=120).started)
    self.sm["pandaStates"][0].ignitionLine = False
    self.assertFalse(self.snapshot(now=120).started)

  def test_fresh_alert_is_copied_even_if_car_data_fails(self):
    ss = self.sm["selfdriveState"]
    ss.alertSize, ss.alertStatus = "full", "critical"
    ss.alertText1, ss.alertText2 = "TAKE CONTROL IMMEDIATELY", "System Unresponsive"
    self.sm.valid["carState"] = False
    state = self.snapshot()
    self.assertTrue(state.stale)
    self.assertEqual(state.alert.size, 3)
    self.assertEqual(state.alert_status, 2)
    self.assertEqual(state.alert_text_1, "TAKE CONTROL IMMEDIATELY")
    ss.alertText1 = "changed"
    self.assertEqual(state.alert_text_1, "TAKE CONTROL IMMEDIATELY")
    with self.assertRaises(FrozenInstanceError):
      state.alert.text1 = "changed"
    self.sm.valid["selfdriveState"] = False
    self.assertIsNone(self.snapshot().alert)

  def test_alert_has_independent_source_age_when_hud_is_stale(self):
    self.adapter = LiveStateAdapter(stale_after=0.35)
    ss = self.sm["selfdriveState"]
    ss.alertSize, ss.alertStatus = "full", "critical"
    ss.alertText1 = "TAKE CONTROL IMMEDIATELY"
    self.sm.valid["carState"] = False
    self.sm.logMonoTime["selfdriveState"] = 99_700_000_000
    state = self.snapshot()
    self.assertTrue(state.stale)
    self.assertIsNotNone(state.alert)
    self.assertAlmostEqual(state.alert_age_seconds, 0.3)
    self.sm.logMonoTime["selfdriveState"] = 99_600_000_000
    state = self.snapshot()
    self.assertIsNone(state.alert)
    self.assertEqual(state.alert_age_seconds, float("inf"))

  def test_no_alert_has_no_independent_display_age(self):
    state = self.snapshot()
    self.assertIsNone(state.alert)
    self.assertEqual(state.alert_age_seconds, float("inf"))

  def test_stock_mode_does_not_require_sunnypilot_messages(self):
    self.adapter = LiveStateAdapter(sunnypilot=False)
    self.sm.seen["selfdriveStateSP"] = False
    self.sm.seen["onroadEvents"] = False
    self.assertFalse(self.snapshot().stale)
    self.sm["selfdriveState"].state = "overriding"
    self.assertEqual(self.snapshot().display_status, "override")

  def test_invalid_numeric_data_aborts_instead_of_rendering_it(self):
    for attr in ("vEgoCluster", "vCruiseCluster"):
      for value in (float("nan"), float("inf")):
        with self.subTest(attr=attr, value=value), self.assertRaisesRegex(ValueError, "Non-finite"):
          self.setUp()
          setattr(self.sm["carState"], attr, value)
          self.snapshot()

  def test_no_gui_dependency_and_reader_only_reads_settings(self):
    params = Mock(spec=["get_bool"])
    params.get_bool.side_effect = lambda key: key == "IsMetric"
    reader = LiveStateReader(sm=self.sm, params=params)
    with patch("tools.android_auto.live_state.time.monotonic", return_value=100):
      state = reader.poll()
      reader.poll()
    self.sm.update.assert_called_with(0)
    self.assertEqual(params.get_bool.call_count, 3)
    self.assertEqual(state.speed_text, "76")
    self.assertFalse(state.hide_speed)

  def test_mads_status_matches_native_branch_order(self):
    # Execute only the existing pure static method, avoiding GUI/Params imports.
    source = Path(__file__).resolve().parents[2] / "openpilot/selfdrive/ui/sunnypilot/ui_state.py"
    tree = ast.parse(source.read_text())
    cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "UIStateSP")
    method = next(node for node in cls.body if isinstance(node, ast.FunctionDef) and node.name == "update_status")
    method.decorator_list = []
    namespace = {"OpenpilotState": NS(preEnabled="preEnabled", overriding="overriding"),
                 "MADSState": NS(paused="paused", overriding="overriding")}
    exec(compile(ast.Module(body=[method], type_ignores=[]), str(source), "exec"), namespace)
    native = namespace["update_status"]
    for state, enabled, available, mads_enabled, mads_state, long_override in itertools.product(
      ("disabled", "enabled", "preEnabled", "overriding"), (False, True), (False, True), (False, True),
      ("disabled", "enabled", "paused", "overriding"), (False, True),
    ):
      ss = NS(state=state, enabled=enabled)
      sp = NS(mads=NS(available=available, enabled=mads_enabled, state=mads_state))
      events = [NS(overrideLongitudinal=long_override)]
      self.assertEqual(display_status(ss, sp, events), native(ss, sp, events))


if __name__ == "__main__":
  unittest.main()
