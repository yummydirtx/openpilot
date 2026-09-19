import os
import time
import unittest

from tools.android_auto.frame_worker import FrameWorker, frame_metadata, receive_control, send_control
from tools.android_auto.live_state import DisplayAlert, LiveDisplayState
from tools.android_auto.viewport import Viewport


def fake_frames(connection, buffer, config):
  send_control(connection, ("ready",))
  while True:
    request = receive_control(connection)
    data = b"keyframe" if request[1] else b"frame"
    memoryview(buffer).cast("B")[:len(data)] = data
    send_control(connection, ("frame", len(data), {"captured_at": time.monotonic()}))


def fake_stall(connection, buffer, config):
  send_control(connection, ("ready",))
  receive_control(connection)
  time.sleep(60)


def fake_crash(connection, buffer, config):
  send_control(connection, ("ready",))
  receive_control(connection)
  connection.close()


def fake_hard_exit(connection, buffer, config):
  send_control(connection, ("ready",))
  receive_control(connection)
  os._exit(23)


def fake_init_error(connection, buffer, config):
  send_control(connection, ("error", "missing native assets"))
  connection.close()


class TestFrameWorker(unittest.TestCase):
  def worker(self, target, **kwargs):
    worker = FrameWorker(Viewport(800, 480), "/unused", _target=target, startup_timeout=5, **kwargs)
    self.addCleanup(worker.close)
    return worker

  def test_one_outstanding_request_and_reusable_shared_buffer(self):
    worker = self.worker(fake_frames)
    self.assertIsNone(worker.poll())
    worker.request()
    self.assertTrue(worker.busy)
    with self.assertRaises(RuntimeError):
      worker.request()
    frame, metadata = worker.poll(0.5)
    self.assertEqual(frame, b"frame")
    self.assertLess(time.monotonic() - metadata["captured_at"], 0.5)
    self.assertFalse(worker.busy)
    worker.request(force_keyframe=True)
    self.assertEqual(worker.poll(0.5)[0], b"keyframe")
    self.assertEqual(frame, b"frame")  # Prior copied payload is not overwritten.

  def test_watchdog_terminates_a_stuck_child_without_waiting_for_it(self):
    worker = self.worker(fake_stall, frame_timeout=0.1)
    worker.request()
    started = time.monotonic()
    with self.assertRaises(TimeoutError):
      worker.poll(1)
    self.assertLess(time.monotonic() - started, 0.8)
    self.assertTrue(worker.closed)
    self.assertFalse(worker._child.is_alive())

  def test_crashed_child_is_reported_and_cleaned_up(self):
    worker = self.worker(fake_crash)
    worker.request()
    with self.assertRaises((RuntimeError, EOFError)):
      worker.poll(0.5)
    self.assertTrue(worker.closed)

  def test_initialization_failure_is_actionable(self):
    with self.assertRaisesRegex(RuntimeError, "missing native assets"):
      self.worker(fake_init_error)

  def test_hard_crash_cleanup_is_idempotent(self):
    worker = self.worker(fake_hard_exit)
    worker.request()
    with self.assertRaises((RuntimeError, EOFError)):
      worker.poll(0.5)
    worker.close()
    worker.close()
    self.assertFalse(worker._child.is_alive())
    self.assertEqual(worker._child.exitcode, 23)
    self.assertTrue(worker._connection.closed)

  def test_metadata_never_retains_active_values_when_stale(self):
    state = LiveDisplayState(speed=47, set_speed=65, status="engaged", started=True, age_seconds=1)
    metadata = frame_metadata(state, 123.0, 0.01)
    self.assertTrue(metadata["stale"])
    self.assertEqual(metadata["status"], "disengaged")
    self.assertIsNone(metadata["speed"])
    self.assertIsNone(metadata["set_speed"])
    self.assertEqual(metadata["source_age_seconds"], 1)

  def test_metadata_accounts_for_poll_delay_and_cumulative_worker_cpu(self):
    state = LiveDisplayState(speed=47, set_speed=65, status="engaged", started=True, age_seconds=0.45)
    metadata = frame_metadata(state, 123.0, 0.015, cpu_seconds=2.5, render_encode_cpu_seconds=0.01, poll_seconds=0.1)
    self.assertAlmostEqual(metadata["source_age_seconds"], 0.55)
    self.assertEqual(metadata["cpu_seconds"], 2.5)
    self.assertEqual(metadata["render_encode_cpu_seconds"], 0.01)

  def test_alert_keeps_its_own_expiry_when_other_hud_sources_are_stale(self):
    state = LiveDisplayState(speed=47, started=True, age_seconds=1, alert=DisplayAlert("Take control", "", 3, 2),
                             alert_age_seconds=0.3)
    metadata = frame_metadata(state, 123.0, 0.02, poll_seconds=0.01,
                              road={"camera_displayed": True, "display_age_seconds": 0.1})
    self.assertTrue(metadata["stale"])
    self.assertAlmostEqual(metadata["display_source_age_seconds"], 0.31)
    self.assertAlmostEqual(metadata["alert_age_seconds"], 0.31)

  def test_road_source_timestamp_counts_poll_and_paint_time_only_once(self):
    state = LiveDisplayState(speed=0, started=True, age_seconds=0.04)
    metadata = frame_metadata(state, 123.0, 0.14, poll_seconds=0.075,
                              road={"camera_displayed": True, "display_age_seconds": 0.2,
                                    "display_source_timestamp": 122.85})
    self.assertAlmostEqual(metadata["display_source_age_seconds"], 0.15)


if __name__ == "__main__":
  unittest.main()
