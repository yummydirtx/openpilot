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
