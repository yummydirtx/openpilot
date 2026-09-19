import io
import struct
import unittest
from unittest.mock import Mock, patch

from tools.android_auto.live_session import LiveVideoSession, PeerRequestedStop
from tools.android_auto.session import Session, field
from tools.android_auto.test_probe import FragmentedPeer
from tools.android_auto.test_session import frame


def nal(kind):
  return b"\x00\x00\x00\x01" + bytes([kind]) + b"sample"


KEYFRAME = nal(9) + nal(7) + nal(8) + nal(5)
PFRAME = nal(9) + nal(1)


def session():
  # Keep the live lifecycle initialization, replacing only credential setup.
  with patch.object(Session, "__init__", return_value=None):
    result = LiveVideoSession()
  result.peer = object()
  result.log = io.StringIO()
  result.fragments = {}
  result.video_channel = 9
  result.session_id = 1
  result.config_index = 0
  result.focused = False
  result.window = 2
  result.unacked = 0
  result.acked = 0
  result.send = Mock()
  return result


def grant_focus(target, mode=1):
  target.handle(9, 0x8008, field(1, mode))


def acknowledge(target, sid=1, count=1):
  target.handle(9, 0x8004, field(1, sid) + field(2, count))


class TestLiveVideoSession(unittest.TestCase):
  def test_open_video_preserves_receiver_margins_and_caps_window(self):
    target = session()
    target.channels = [{"id": 9, "video_configs": [{1: [2], 3: [0], 4: [240]}]}]
    target.discover = lambda: target.channels
    target.wait_for = Mock(side_effect=[field(1, 0), field(1, 2) + field(2, 4) + field(3, 0)])
    target.receive = lambda: (9, 0x8008, field(1, 1))
    with patch("tools.android_auto.live_session.select.select", return_value=([target.peer], [], [])):
      target.open_video(1280, 720)
    self.assertEqual((target.window, target.margin_width, target.margin_height), (2, 0, 240))
    self.assertTrue(target.focused)
    self.assertTrue(target.needs_keyframe)
    target.send.assert_any_call(9, 0x8007, field(2, 1) + field(3, 4))
    target.send.assert_any_call(9, 0x8001, field(1, 1) + field(2, 0))

  def test_focus_loss_longer_than_five_seconds_preserves_control_channel(self):
    target = session()
    grant_focus(target)
    with patch("tools.android_auto.live_session.time.monotonic", return_value=0):
      target.send_frame(KEYFRAME, 0)
    target.handle(9, 0x8008, field(1, 2))
    target.check_progress(now=60)
    target.handle(0, 11, field(1, 9876))
    target.send.assert_called_with(0, 12, field(1, 9876))
    with self.assertRaisesRegex(RuntimeError, "focus/window"):
      target.send_frame(PFRAME, 60_000_000)
    self.assertEqual(target.frames_sent, 1)

  def test_regained_focus_starts_new_session_and_requires_fresh_keyframe(self):
    target = session()
    grant_focus(target)
    target.send_frame(KEYFRAME, 0)
    target.handle(9, 0x8008, field(1, 2))
    grant_focus(target)
    self.assertEqual((target.session_id, target.focus_epoch, target.discarded_inflight), (2, 2, 1))
    self.assertEqual((target.unacked, len(target.pending)), (0, 0))
    target.send.assert_called_with(9, 0x8001, field(1, 2) + field(2, 0))
    for invalid in (PFRAME, nal(5), nal(7) + nal(8), nal(7) + nal(5)):
      with self.subTest(invalid=invalid), self.assertRaisesRegex(ValueError, "SPS/PPS"):
        target.send_frame(invalid, 10_000_000)
    self.assertTrue(target.needs_keyframe)
    target.send_frame(KEYFRAME, 10_000_000)
    self.assertFalse(target.needs_keyframe)
    self.assertEqual((target.unacked, target.frames_sent), (1, 2))

  def test_duplicate_focus_preserves_decoder_and_pending_frames(self):
    for mode in (1, 4):
      with self.subTest(mode=mode):
        target = session()
        grant_focus(target, mode)
        target.send_frame(KEYFRAME, 0)
        target.send.reset_mock()
        grant_focus(target, mode)
        target.send.assert_not_called()
        self.assertEqual((target.session_id, target.focus_epoch, target.unacked), (1, 1, 1))
        self.assertFalse(target.needs_keyframe)

  def test_old_session_ack_cannot_release_current_window_credit(self):
    target = session()
    grant_focus(target)
    target.send_frame(KEYFRAME, 0)
    target.handle(9, 0x8008, field(1, 2))
    grant_focus(target)
    target.send_frame(KEYFRAME, 1_000_000)
    acknowledge(target, sid=1)
    self.assertEqual((target.unacked, target.acked, len(target.pending)), (1, 0, 1))
    acknowledge(target, sid=2)
    self.assertEqual((target.unacked, target.acked, len(target.pending)), (0, 1, 0))

  def test_late_ack_while_unfocused_is_accounted_before_session_restarts(self):
    target = session()
    grant_focus(target)
    target.send_frame(KEYFRAME, 0)
    target.handle(9, 0x8008, field(1, 2))
    acknowledge(target)
    grant_focus(target)
    self.assertEqual((target.discarded_inflight, target.acked, target.session_id), (0, 1, 2))

  def test_window_backpressure_releases_only_acknowledged_credit(self):
    target = session()
    grant_focus(target)
    with patch("tools.android_auto.live_session.time.monotonic", return_value=0):
      target.send_frame(KEYFRAME, 0)
      target.send_frame(PFRAME, 100_000)
    with self.assertRaisesRegex(RuntimeError, "window"):
      target.send_frame(PFRAME, 200_000)
    self.assertEqual((target.max_pending, target.frames_sent), (2, 2))
    with patch("tools.android_auto.live_session.time.monotonic", return_value=0.25):
      acknowledge(target)
      target.send_frame(PFRAME, 300_000)
    self.assertEqual((target.max_pending, target.unacked, target.frames_sent), (2, 2, 3))
    self.assertEqual(target.max_ack_seconds, 0.25)

  def test_active_ack_stall_fails_after_500_ms(self):
    target = session()
    grant_focus(target)
    with patch("tools.android_auto.live_session.time.monotonic", return_value=10):
      target.send_frame(KEYFRAME, 0)
    target.check_progress(now=10.49)
    with self.assertRaisesRegex(TimeoutError, "500 ms"):
      target.check_progress(now=10.51)

  def test_invalid_ack_cannot_change_credit(self):
    for sid, count in ((99, 1), (1, 0), (1, 2)):
      with self.subTest(sid=sid, count=count):
        target = session()
        grant_focus(target)
        target.send_frame(KEYFRAME, 0)
        with self.assertRaisesRegex(ValueError, "acknowledgement"):
          acknowledge(target, sid, count)
        self.assertEqual((target.unacked, target.acked, len(target.pending)), (1, 0, 1))

  def test_frame_payload_carries_requested_timestamp(self):
    target = session()
    grant_focus(target)
    target.send_frame(KEYFRAME, 1_234_567)
    target.send.assert_called_with(9, 0, struct.pack(">Q", 1_234_567) + KEYFRAME)

  def test_peer_bye_bye_is_acknowledged_and_stops_projection(self):
    target = session()
    with self.assertRaises(PeerRequestedStop):
      target.handle(0, 15, field(1, 1))
    target.send.assert_called_once_with(0, 16)
    self.assertIn("peer_requested_shutdown", target.log.getvalue())

  def test_known_control_notifications_do_not_start_services(self):
    target = session()
    for kind in (14, 19):
      target.handle(0, kind, field(1, 1))
    target.send.assert_not_called()
    self.assertFalse(target.media_started)

  def test_unknown_or_unopened_input_message_fails_closed(self):
    for channel, kind in ((0, 17), (9, 0x9000), (3, 0x8001)):
      with self.subTest(channel=channel, kind=kind):
        target = session()
        with self.assertRaisesRegex(ValueError, "Unsupported live message"):
          target.handle(channel, kind, b"")
        target.send.assert_not_called()

  def test_live_transport_rejects_plaintext_after_authentication(self):
    target = session()
    target.authenticated = True
    target.peer = FragmentedPeer(frame(0, 3, b"\x00\x0b" + field(1, 1)))
    with self.assertRaisesRegex(ValueError, "Plaintext"):
      target.receive()

  def test_shutdown_drains_ack_and_ping_before_response(self):
    target = session()
    grant_focus(target)
    target.send_frame(KEYFRAME, 0)
    target.receive = Mock(side_effect=[(9, 0x8004, field(1, 1) + field(2, 1)),
                                      (0, 11, field(1, 123)), (0, 16, b"")])
    with patch("tools.android_auto.live_session.select.select", return_value=([target.peer], [], [])):
      target.shutdown()
    target.send.assert_any_call(0, 15, field(1, 1))
    target.send.assert_any_call(0, 12, field(1, 123))
    self.assertEqual(target.unacked, 0)
    self.assertIn("shutdown_acknowledged", target.log.getvalue())


if __name__ == "__main__":
  unittest.main()
