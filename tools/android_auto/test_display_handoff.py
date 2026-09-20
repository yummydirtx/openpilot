from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from unittest.mock import Mock

from openpilot.system.ui.lib.display_handoff import HandoffClient, read_json, write_json
from tools.android_auto.control import valid_request
from tools.android_auto.display_control import DisplayControl
from tools.android_auto.test_live_session import session, grant_focus, acknowledge, KEYFRAME


def touch(*, down=False, pressed=False, released=False, slot=0):
  return SimpleNamespace(left_down=down, left_pressed=pressed, left_released=released, slot=slot)


class TestDisplayHandoff(unittest.TestCase):
  def setUp(self):
    self.temp = TemporaryDirectory()
    self.addCleanup(self.temp.cleanup)
    self.root = Path(self.temp.name)
    self.client = HandoffClient(self.root, self.root)

  def status(self, *, ready=False, at=10.0, token=None, until=10.3):
    write_json(self.root / "status.json", {"at": at, "ready_until": until, "token": token or self.client.token,
                                           "ready": ready, "phase": "projecting" if ready else "connecting"})

  def project(self):
    self.client.select("project")
    self.status(ready=True)
    self.client.tick(now=10.0)
    self.assertTrue(self.client.suppressed)

  def test_start_waits_for_current_token_and_fresh_frame(self):
    self.status()
    self.client.tick(now=10.0)
    self.assertTrue(self.client.available)
    self.client.select("project")
    self.client.tick(now=10.1)
    self.assertFalse(self.client.suppressed)
    self.status(ready=True, token="old")
    self.client.tick(now=10.1)
    self.assertFalse(self.client.suppressed)
    self.status(ready=True)
    self.client.tick(now=10.1)
    self.assertTrue(self.client.suppressed)

  def test_new_tap_survives_previous_sessions_failure_and_publishes_request(self):
    old_token = self.client.token
    write_json(self.root / "status.json", {"at": 10., "token": old_token, "phase": "failed"})
    self.client.tick(now=10.)
    self.client.select("project")
    new_token = self.client.token
    self.assertEqual(self.client.status_text, "starting...")
    self.client.tick(now=10.1)
    self.assertEqual(self.client.mode, "project")
    request = read_json(self.root / "request.json")
    self.assertEqual((request["token"], request["mode"]), (new_token, "project"))
    self.assertFalse(self.client.failed)

  def test_status_immediately_acknowledges_tap_then_tracks_current_session(self):
    self.assertEqual(self.client.status_text, "tap to start")
    self.client.select("project")
    self.assertEqual(self.client.status_text, "starting...")
    self.assertTrue(self.client.connecting)
    self.status()
    self.client.tick(now=10.)
    self.assertEqual(self.client.status_text, "connecting...")
    self.status(ready=True)
    self.client.tick(now=10.1)
    self.assertEqual(self.client.status_text, "connected")
    self.assertFalse(self.client.connecting)
    self.client.select("local")
    self.assertEqual(self.client.status_text, "tap to start")

  def test_failure_status_persists_until_explicit_retry(self):
    self.client.select("project")
    write_json(self.root / "status.json", {"at": 10., "token": self.client.token, "phase": "failed"})
    self.client.tick(now=10.)
    self.assertEqual(self.client.mode, "local")
    self.assertEqual(self.client.status_text, "failed - retry")
    self.assertFalse(self.client.connecting)
    write_json(self.root / "status.json", {"at": 10.1, "token": self.client.token, "phase": "local"})
    self.client.tick(now=10.1)
    self.assertEqual(self.client.status_text, "failed - retry")
    self.client.select("project")
    self.assertEqual(self.client.status_text, "starting...")

  def test_bootstrap_without_supervisor_response_has_bounded_starting_status(self):
    with patch("time.monotonic", return_value=10.):
      self.client.select("project")
    self.client.tick(now=19.9)
    self.assertEqual(self.client.status_text, "starting...")
    self.client.tick(now=20.1)
    self.assertEqual(self.client.status_text, "failed - retry")

  def test_supervisor_launcher_failure_is_visible(self):
    client = HandoffClient(self.root, self.root, starter=Mock(side_effect=OSError("missing")))
    client.select("project")
    self.assertEqual(client.status_text, "failed - retry")

  def test_touch_consumes_whole_gesture_and_local_choice_is_sticky(self):
    self.project()
    previous = self.client.token
    self.assertEqual(self.client.tick([touch(down=True, pressed=True)], now=10.1), [])
    self.assertFalse(self.client.suppressed)
    self.assertEqual(self.client.mode, "local")
    self.status(ready=True, token=previous)
    self.assertEqual(self.client.tick([touch(down=True)], now=10.11), [])
    self.assertEqual(self.client.tick([touch(released=True)], now=10.12), [])
    event = touch(pressed=True)
    self.assertEqual(self.client.tick([event], now=10.13), [event])
    self.assertFalse(self.client.suppressed)

  def test_all_multitouch_release_events_are_consumed(self):
    self.project()
    self.client.tick([touch(down=True, slot=0), touch(down=True, slot=1)], now=10.1)
    self.assertEqual(self.client.tick([touch(released=True, slot=0)], now=10.11), [])
    self.assertEqual(self.client.tick([touch(down=True, slot=1)], now=10.12), [])
    self.assertEqual(self.client.tick([touch(released=True, slot=1)], now=10.13), [])

  def test_expired_supervisor_and_critical_alert_restore_local(self):
    for kwargs in ({"now": 10.6}, {"now": 10.1, "critical": True}):
      self.project()
      self.client.tick(**kwargs)
      self.assertFalse(self.client.suppressed)
      self.assertEqual(self.client.mode, "local")

  def test_frame_expiry_is_not_extended_by_supervisor_heartbeat(self):
    self.project()
    self.status(ready=True, at=10.31, until=10.3)
    self.client.tick(now=10.32)
    self.assertFalse(self.client.suppressed)

  def test_supervisor_loss_during_connection_or_after_frame_expiry_cancels_request(self):
    for ready in (False, True):
      self.client.select("project")
      self.status(ready=ready)
      self.client.tick(now=10.1)
      self.client.tick(now=10.4)
      self.client.tick(now=10.6)
      self.assertEqual(self.client.mode, "local")
      self.assertFalse(self.client.suppressed)

  def test_restart_does_not_reuse_old_ready_lease(self):
    self.project()
    new_client = HandoffClient(self.root, self.root)
    new_client.tick(now=10.1)
    self.assertFalse(new_client.suppressed)

  def test_explicit_selection_can_bootstrap_supervisor_and_failure_stays_local(self):
    starter = Mock()
    client = HandoffClient(self.root, self.root, starter=starter)
    self.assertTrue(client.enabled)
    client.select("project")
    starter.assert_called_once_with()
    self.assertFalse(client.suppressed)
    client.select("local")
    starter.side_effect = OSError("Launcher missing")
    client.select("project")
    self.assertEqual(client.mode, "local")

  def test_comma_display_menu_choice_resets_native_button(self):
    self.project()
    status = read_json(self.root / "status.json")
    status["local_requested"] = True
    write_json(self.root / "status.json", status)
    self.client.tick(now=10.1)
    self.assertEqual(self.client.mode, "local")
    self.assertEqual(self.client.label, "Use Mazda display")
    self.assertFalse(self.client.suppressed)

  def test_invalid_files_fail_open(self):
    for text in ("{", "[]", "x" * 5000):
      (self.root / "status.json").write_text(text)
      self.client.tick(now=10)
      self.assertFalse(self.client.suppressed)
    (self.root / "link").symlink_to(self.root / "status.json")
    self.assertEqual(read_json(self.root / "link"), {})

  def test_supervisor_accepts_only_fresh_fixed_mode_request(self):
    request = {"at": 10., "token": "a" * 32, "mode": "project"}
    self.assertTrue(valid_request(request, 10.1))
    self.assertFalse(valid_request(request, 11.))
    for update in ({"mode": "shell"}, {"token": "../bad"}, {"at": float("nan")}, {"at": 12.}):
      self.assertFalse(valid_request({**request, **update}, 10.1))

  def test_runtime_ready_requires_ack_current_frame_and_intent(self):
    controller = DisplayControl(self.root)
    s = session()
    grant_focus(s)
    write_json(self.root / "intent.json", {"at": 10., "token": "a" * 32, "mode": "project"})
    with patch("time.monotonic", return_value=10.):
      controller.sync(s)
      controller.publish(s, valid_until=10.4, stale=False)
      self.assertFalse(read_json(self.root / "projection.json")["ready"])
      s.send_frame(KEYFRAME, 0)
      acknowledge(s)
    with patch("time.monotonic", return_value=10.11):
      controller.publish(s, valid_until=10.4, stale=False)
      self.assertTrue(read_json(self.root / "projection.json")["ready"])
    write_json(self.root / "intent.json", {"at": 10.2, "token": "b" * 32, "mode": "local"})
    with patch("time.monotonic", return_value=10.21):
      controller.sync(s)
      self.assertFalse(s.focused)
    with patch("time.monotonic", return_value=11.):
      with self.assertRaisesRegex(RuntimeError, "heartbeat"):
        controller.sync(s)

  def test_heartbeat_written_during_read_is_not_rejected_as_future(self):
    # A writer can rename its new file after the reader starts but before it
    # samples the validation clock. All readers must clock AFTER the read.
    clock = [10.0]

    def concurrent_read(_):
      clock[0] = 10.002
      return {"at": 10.001, "token": self.client.token, "mode": "project",
              "phase": "projecting", "ready": True, "ready_until": 10.3}

    self.client.select("project")
    with patch("time.monotonic", side_effect=lambda: clock[0]), \
         patch("openpilot.system.ui.lib.display_handoff.read_json", side_effect=concurrent_read):
      self.client.tick()
      self.assertTrue(self.client.suppressed)
    clock[0] = 10.0
    with patch("time.monotonic", side_effect=lambda: clock[0]), \
         patch("tools.android_auto.display_control.read_json", side_effect=concurrent_read):
      DisplayControl(self.root).sync(session())
