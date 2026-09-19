import io
import unittest
from unittest.mock import patch

from tools.android_auto.session import field
from tools.android_auto.video import VideoSession, access_units
from tools.android_auto.viewport import Viewport, demo_state


def nal(kind, data=b"sample"):
  return b"\x00\x00\x00\x01" + bytes([kind]) + data


class TestVideo(unittest.TestCase):
  def test_access_units_keep_headers_with_keyframe(self):
    first = nal(9) + nal(7) + nal(8) + nal(5)
    second = nal(9) + nal(1)
    self.assertEqual(access_units(first + second), [first, second])

  def test_reject_unframed_or_non_random_access_clip(self):
    for clip in (b"", b"garbage", nal(5), nal(9) + nal(1), nal(9) + nal(7) + nal(8), nal(9) + b"\x00\x00\x01"):
      with self.subTest(clip=clip), self.assertRaises(ValueError):
        access_units(clip)

  def session(self, incoming):
    session = object.__new__(VideoSession)
    session.peer = object()
    session.video_channel = 2
    session.session_id = 1
    session.config_index = 0
    session.unacked = 2
    session.acked = 3
    session.focused = True
    session.log = io.StringIO()
    session.receive = lambda: incoming
    session.sent = []
    session.send = lambda *args: session.sent.append(args)
    return session

  def pump(self, session):
    with patch("tools.android_auto.video.select.select", return_value=([session.peer], [], [])):
      session.pump(0)

  def test_acknowledgements_release_only_the_matching_session_window(self):
    session = self.session((2, 0x8004, field(1, 1) + field(2, 2)))
    self.pump(session)
    self.assertEqual((session.unacked, session.acked), (0, 5))
    for sid, count in ((2, 1), (1, 3), (1, 0)):
      with self.subTest(sid=sid, count=count), self.assertRaises(ValueError):
        self.pump(self.session((2, 0x8004, field(1, sid) + field(2, count))))

  def test_focus_loss_pauses_and_focus_gain_starts_selected_configuration(self):
    session = self.session((2, 0x8008, field(1, 2)))
    self.pump(session)
    self.assertFalse(session.focused)
    self.assertEqual(session.sent, [])
    session.receive = lambda: (2, 0x8008, field(1, 1))
    self.pump(session)
    self.assertTrue(session.focused)
    self.assertEqual(session.sent, [(2, 0x8001, field(1, 1) + field(2, 0))])

  def test_keepalive_during_streaming(self):
    session = self.session((0, 11, field(1, 9876) + field(2, 0)))
    self.pump(session)
    self.assertEqual(session.sent, [(0, 12, field(1, 9876))])

  def test_unknown_message_is_not_silently_ignored(self):
    with self.assertRaisesRegex(ValueError, "Unhandled"):
      self.pump(self.session((7, 0x8001, b"")))

  def test_video_stall_is_bounded(self):
    session = self.session((2, 0x8008, field(1, 2)))
    session.window = 2
    session.pump = lambda timeout: None
    with patch("tools.android_auto.video.time.monotonic", side_effect=range(20)), self.assertRaises(TimeoutError):
      session.project([b"frame"], 30)
    self.assertEqual(session.sent, [])

  def test_orderly_shutdown_waits_for_peer_response(self):
    session = self.session((0, 16, b""))
    with patch("tools.android_auto.video.select.select", return_value=([session.peer], [], [])):
      session.shutdown()
    self.assertEqual(session.sent, [(0, 15, field(1, 1))])
    self.assertIn('"event": "shutdown_acknowledged"', session.log.getvalue())


class TestViewport(unittest.TestCase):
  def test_uniform_scale_and_margins(self):
    for width, height, margin_width, margin_height in ((800, 480, 0, 0), (1280, 720, 0, 0), (1280, 720, 0, 220), (800, 480, 50, 30)):
      with self.subTest(size=(width, height, margin_width, margin_height)):
        viewport = Viewport(width, height, margin_width, margin_height)
        self.assertEqual(viewport.to_video(0, 0), (margin_width / 2, margin_height / 2))
        x, y = viewport.to_video(viewport.logical_width, 1080)
        self.assertAlmostEqual(x, width - margin_width / 2)
        self.assertAlmostEqual(y, height - margin_height / 2)
        dx, dy = viewport.to_video(100, 100)
        self.assertAlmostEqual(dx - margin_width / 2, dy - margin_height / 2)

  def test_invalid_margins(self):
    for args in ((0, 480), (800, 0), (800, 480, 800, 0), (800, 480, -1, 0)):
      with self.subTest(args=args), self.assertRaises(ValueError):
        Viewport(*args)

  def test_stale_demo_removes_active_state_and_speed(self):
    active = demo_state(2)
    stale = demo_state(10)
    self.assertEqual(active.display_status, "engaged")
    self.assertFalse(active.stale)
    self.assertEqual(stale.display_status, "disengaged")
    self.assertEqual(stale.speed_text, "–")
    self.assertEqual(stale.source, "synthetic")


if __name__ == "__main__":
  unittest.main()
