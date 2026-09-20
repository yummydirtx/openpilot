from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

from openpilot.system.ui.lib.display_handoff import read_json, write_json
from openpilot.system.ui.lib.driver_preview import DriverPreviewHost, DriverPreviewRequest, preview_allowed, monitoring_fresh, preview_offroad
from openpilot.system.hardware.driver_view import preview_core_ready, should_power_save


class Subscriber(dict):
  def __init__(self):
    super().__init__(deviceState=SimpleNamespace(started=True),
                     pandaStates=[SimpleNamespace(ignitionLine=False, ignitionCan=False)],
                     carState=SimpleNamespace(canValid=True, vEgo=0., gearShifter="park"),
                     selfdriveState=SimpleNamespace(enabled=False), carControl=SimpleNamespace(latActive=False, longActive=False))
    self.alive = dict.fromkeys(self, True)
    self.valid = dict.fromkeys(self, True)
    self.recv_time = dict.fromkeys(self, 10.)
    self.logMonoTime = dict.fromkeys(self, int(10e9))


class TestPreviewAccess(TestCase):
  def test_raw_ignition_revokes_demo_before_device_started(self):
    sm = Subscriber()
    sm["deviceState"].started = False
    self.assertTrue(preview_offroad(sm, 10.1))
    for field in ("ignitionLine", "ignitionCan"):
      setattr(sm["pandaStates"][0], field, True)
      self.assertFalse(preview_offroad(sm, 10.1))
      self.assertFalse(preview_allowed(sm, 10.1))
      setattr(sm["pandaStates"][0], field, False)
    sm.logMonoTime["pandaStates"] = 0
    self.assertFalse(preview_offroad(sm, 10.1))

  def test_offroad_demo_accepts_fresh_policy_without_missing_driving_inputs(self):
    sm = Subscriber()
    sm["deviceState"].started = False
    sm.update(driverStateV2=object(), driverMonitoringState=object())
    for name in ("driverStateV2", "driverMonitoringState"):
      sm.alive[name] = sm.valid[name] = True
      sm.recv_time[name] = 10.
      sm.logMonoTime[name] = int(10e9)
    sm.valid["driverMonitoringState"] = False
    self.assertFalse(monitoring_fresh(sm, 10.1))
    self.assertTrue(monitoring_fresh(sm, 10.1, demo=True))
    sm["deviceState"].started = True
    self.assertFalse(monitoring_fresh(sm, 10.1, demo=True))
    sm["deviceState"].started = False
    sm.valid["driverStateV2"] = False
    self.assertFalse(monitoring_fresh(sm, 10.1, demo=True))
    sm.valid["driverStateV2"] = True
    sm.logMonoTime["driverMonitoringState"] = 0
    self.assertFalse(monitoring_fresh(sm, 10.1, demo=True))

  def test_ignition_on_in_park_and_disengaged_can_preview(self):
    self.assertTrue(preview_allowed(Subscriber(), 10.1))

  def test_offroad_does_not_require_car_data(self):
    sm = Subscriber()
    sm["deviceState"].started = False
    sm.alive["carState"] = sm.valid["selfdriveState"] = False
    self.assertTrue(preview_allowed(sm, 10.1))

  def test_motion_gear_engagement_and_invalid_data_close_preview(self):
    cases = [("carState", "vEgo", .1), ("carState", "vEgo", float("nan")),
             ("carState", "gearShifter", "drive"), ("carState", "canValid", False),
             ("selfdriveState", "enabled", True), ("carControl", "latActive", True), ("carControl", "longActive", True)]
    for service, field, value in cases:
      with self.subTest(service=service, field=field, value=value):
        sm = Subscriber()
        setattr(sm[service], field, value)
        self.assertFalse(preview_allowed(sm, 10.1))

  def test_stale_or_invalid_services_fail_closed(self):
    for name in ("deviceState", "carState", "selfdriveState", "carControl"):
      for field, value in [("alive", False), ("valid", False), ("recv_time", 0.), ("logMonoTime", 0)]:
        with self.subTest(name=name, field=field):
          sm = Subscriber()
          getattr(sm, field)[name] = value
          self.assertFalse(preview_allowed(sm, 10.1))


class TestPreviewPower(TestCase):
  def test_screen_off_preview_keeps_model_core_and_audio_awake(self):
    self.assertTrue(should_power_save(False, 0., False))
    self.assertFalse(should_power_save(False, 0., True))
    self.assertFalse(should_power_save(True, 0., False))
    self.assertFalse(should_power_save(False, 50., False))

  def test_wait_for_model_core_before_starting_offroad_processes(self):
    with patch.object(Path, "read_text", return_value="0\n"):
      self.assertFalse(preview_core_ready())
    with patch.object(Path, "read_text", return_value="1\n"):
      self.assertTrue(preview_core_ready())
    with patch.object(Path, "read_text", side_effect=OSError):
      self.assertFalse(preview_core_ready())


class Params:
  def __init__(self):
    self.enabled = False
    self.resets = 0

  def get_bool(self, key):
    assert key == "IsDriverViewEnabled"
    return self.enabled

  def put_bool(self, key, value, *, block):
    assert key == "IsDriverViewEnabled" and block
    self.enabled = value

  def remove(self, key):
    assert key == "DriverTooDistracted"
    self.resets += 1


class TestDriverPreviewHost(TestCase):
  def setUp(self):
    self.params = Params()
    self.publish = Mock()
    self.factory = Mock(return_value=self.publish)
    self.host = DriverPreviewHost(self.params, self.factory)
    self.request = {"at": 10., "token": "a" * 32, "active": True, "reset": "first"}

  def update(self, **kwargs):
    args = {"token": "a" * 32, "projecting": True, "offroad": True, "now": 10.1}
    args.update(kwargs)
    self.host.update(self.request, **args)

  def test_offroad_preview_enables_camera_and_forwards_monitoring_feedback(self):
    dm = object()
    self.update(dm_state=dm)
    self.assertTrue(self.params.enabled)
    self.factory.assert_called_once_with()
    self.publish.assert_called_once_with(dm)

  def test_expired_renderer_lease_clears_camera_and_releases_publisher(self):
    self.update()
    self.update(now=10.6)
    self.assertFalse(self.params.enabled)
    self.assertIsNone(self.host.publisher)
    self.assertIsNone(self.host.owner)
    self.publish.assert_called_with(None)
    count = self.publish.call_count
    self.host.close()
    self.assertEqual(self.publish.call_count, count)

  def test_local_display_or_oem_focus_return_stops_preview(self):
    self.update()
    self.update(projecting=False)
    self.assertFalse(self.params.enabled)

  def test_onroad_transition_releases_without_publishing_over_driving_state(self):
    self.update()
    self.publish.reset_mock()
    self.update(offroad=False)
    self.assertFalse(self.params.enabled)
    self.publish.assert_not_called()

  def test_onroad_or_wrong_session_cannot_open_preview(self):
    self.update(offroad=False)
    self.update(token="b" * 32)
    self.factory.assert_not_called()
    self.assertFalse(self.params.enabled)

  def test_existing_local_camera_preview_keeps_ownership(self):
    self.params.enabled = True
    self.update()
    self.host.close()
    self.factory.assert_not_called()
    self.assertTrue(self.params.enabled)

  def test_reset_command_is_processed_once_not_every_frame(self):
    self.update()
    resets = self.params.resets
    self.update()
    self.assertEqual(self.params.resets, resets)
    self.request["reset"] = "next"
    self.update()
    self.assertEqual(self.params.resets, resets + 1)

  def test_closed_preview_releases_camera_immediately(self):
    self.update()
    self.request["active"] = False
    self.update()
    self.assertFalse(self.params.enabled)

  def test_publisher_creation_failure_does_not_take_camera_ownership(self):
    self.factory.side_effect = RuntimeError("Publisher already owned")
    self.update()
    self.assertFalse(self.params.enabled)
    self.assertIsNone(self.host.owner)

  def test_publisher_failure_releases_preview_without_crashing_ui(self):
    self.publish.side_effect = OSError("Publisher disconnected")
    self.update()
    self.assertFalse(self.params.enabled)
    self.assertIsNone(self.host.publisher)


class TestDriverPreviewRequest(TestCase):
  def test_request_uses_current_session_and_close_revokes_without_live_intent(self):
    with TemporaryDirectory() as temporary, patch("time.monotonic", return_value=10.):
      root = Path(temporary)
      request = DriverPreviewRequest(root, root)
      request.update(True, "reset")
      self.assertFalse((root / "driver-preview.json").exists())
      write_json(root / "intent.json", {"at": 10., "mode": "local", "token": "a" * 32})
      request.update(True, "reset")
      self.assertFalse((root / "driver-preview.json").exists())
      write_json(root / "intent.json", {"at": 10., "mode": "project", "token": "a" * 32})
      request.update(True, "reset")
      value = read_json(root / "driver-preview.json")
      self.assertEqual((value["active"], value["token"]), (True, "a" * 32))
      (root / "intent.json").unlink()
      request.update(False, "reset")
      self.assertFalse(read_json(root / "driver-preview.json")["active"])
