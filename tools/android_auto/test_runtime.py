from contextlib import ExitStack, contextmanager, nullcontext
import io
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from tools.android_auto import runtime
from tools.android_auto.session import field
from tools.android_auto.test_live_session import KEYFRAME, PFRAME, acknowledge, grant_focus, session


class TestGadgetOwnership(unittest.TestCase):
  def setUp(self):
    temporary = tempfile.TemporaryDirectory()
    self.addCleanup(temporary.cleanup)
    self.root = Path(temporary.name).resolve()
    self.gadgets = self.root / "usb_gadget"
    self.gadgets.mkdir()
    self.lease = self.root / "owned.json"
    self.device = self.root / "usb_accessory"
    self.gadget = self.gadgets / "automaxxing"
    paths = patch.multiple(runtime, GADGET_ROOT=self.gadgets, LEASE=self.lease, DEVICE=str(self.device))
    paths.start()
    self.addCleanup(paths.stop)
    # Files are private temporary fixtures; simulate root ownership on the Mac.
    original_stat = Path.stat

    def stat(path, *args, **kwargs):
      result = original_stat(path, *args, **kwargs)
      if path == self.lease:
        values = list(result)
        values[4] = 0
        return os.stat_result(values)
      return result

    ownership = patch.object(Path, "stat", stat)
    ownership.start()
    self.addCleanup(ownership.stop)

  def create_owned_gadget(self):
    runtime.claim_gadget()
    for relative in ("functions/accessory.0", "configs/c.1/strings/0x409", "strings/0x409"):
      (self.gadget / relative).mkdir(parents=True)
    (self.gadget / "strings/0x409/serialnumber").write_text("automaxxing-dev\n")
    (self.gadget / "configs/c.1/accessory.0").symlink_to(self.gadget / "functions/accessory.0")
    (self.gadget / "UDC").write_text("a600000.dwc3\n")

  def test_claim_records_private_exact_ownership_before_setup(self):
    runtime.claim_gadget()
    self.assertEqual(json.loads(self.lease.read_text()), {"version": 1, "gadget": str(self.gadget)})
    self.assertEqual(self.lease.stat().st_mode & 0o777, 0o600)
    self.assertFalse(self.gadget.exists())

  def test_claim_never_replaces_existing_gadget_lease_or_device(self):
    for existing in (self.gadgets / "other-gadget", self.lease, self.device):
      with self.subTest(existing=existing):
        existing.touch()
        with self.assertRaisesRegex(RuntimeError, "Existing gadget/lease"):
          runtime.claim_gadget()
        self.assertEqual(existing.read_bytes(), b"")
        existing.unlink()

  def test_recovery_without_lease_never_touches_gadgets(self):
    self.gadget.mkdir()
    runtime.recover_owned_gadget()
    self.assertTrue(self.gadget.is_dir())

  def test_crash_after_claim_before_gadget_creation_clears_lease(self):
    runtime.claim_gadget()
    runtime.recover_owned_gadget()
    self.assertFalse(self.lease.exists())

  def test_public_lease_permissions_prevent_recovery(self):
    runtime.claim_gadget()
    self.lease.chmod(0o644)
    with self.assertRaisesRegex(RuntimeError, "permissions"):
      runtime.recover_owned_gadget()
    self.assertTrue(self.lease.exists())

  def test_wrong_lease_target_prevents_recovery(self):
    runtime.claim_gadget()
    self.lease.write_text(json.dumps({"version": 1, "gadget": str(self.gadgets / "other")}))
    with self.assertRaisesRegex(RuntimeError, "Unknown gadget ownership"):
      runtime.recover_owned_gadget()
    self.assertTrue(self.lease.exists())

  def test_unknown_function_refuses_before_unbinding(self):
    self.create_owned_gadget()
    (self.gadget / "functions/another-function").mkdir()
    with self.assertRaisesRegex(RuntimeError, "Unexpected USB function"):
      runtime.recover_owned_gadget()
    self.assertEqual((self.gadget / "UDC").read_text(), "a600000.dwc3\n")
    self.assertTrue(self.lease.exists())

  def test_unknown_configuration_refuses_before_unbinding(self):
    self.create_owned_gadget()
    (self.gadget / "configs/another-config").mkdir()
    with self.assertRaisesRegex(RuntimeError, "Unexpected USB configuration"):
      runtime.recover_owned_gadget()
    self.assertEqual((self.gadget / "UDC").read_text(), "a600000.dwc3\n")

  def test_changed_identity_refuses_before_unbinding(self):
    self.create_owned_gadget()
    (self.gadget / "strings/0x409/serialnumber").write_text("another-owner\n")
    with self.assertRaisesRegex(RuntimeError, "Unexpected gadget serial"):
      runtime.recover_owned_gadget()
    self.assertEqual((self.gadget / "UDC").read_text(), "a600000.dwc3\n")

  def test_changed_link_refuses_before_unbinding(self):
    self.create_owned_gadget()
    link = self.gadget / "configs/c.1/accessory.0"
    link.unlink()
    link.symlink_to(self.root / "another-function")
    with self.assertRaisesRegex(RuntimeError, "Unexpected gadget link"):
      runtime.recover_owned_gadget()
    self.assertEqual((self.gadget / "UDC").read_text(), "a600000.dwc3\n")

  def test_cleanup_failure_preserves_lease_for_recovery(self):
    self.create_owned_gadget()
    with patch.object(Path, "rmdir", side_effect=OSError("function still busy")):
      with self.assertRaisesRegex(OSError, "still busy"):
        runtime.recover_owned_gadget()
    self.assertTrue(self.lease.exists())
    self.assertEqual((self.gadget / "UDC").read_text(), "\n")

  def test_recovery_unbinds_then_removes_only_owned_configfs_entries(self):
    self.create_owned_gadget()
    other = self.gadgets / "other-gadget"
    other.mkdir()
    operations = []
    original_remove = Path.rmdir

    def configfs_remove(path):
      self.assertEqual((self.gadget / "UDC").read_text(), "\n")
      self.assertFalse((self.gadget / "configs/c.1/accessory.0").is_symlink())
      operations.append(path.relative_to(self.gadget).as_posix())
      # In configfs these empty structural dirs and attributes are virtual;
      # emulate their removal while retaining normal filesystem error checks.
      for name in ("functions", "configs", "strings"):
        child = path / name
        if child.exists():
          original_remove(child)
      for name in ("UDC", "serialnumber"):
        (path / name).unlink(missing_ok=True)
      original_remove(path)

    with patch.object(Path, "rmdir", configfs_remove):
      runtime.recover_owned_gadget()
    self.assertEqual(operations, ["functions/accessory.0", "configs/c.1/strings/0x409", "configs/c.1", "strings/0x409", "."])
    self.assertFalse(self.gadget.exists())
    self.assertFalse(self.lease.exists())
    self.assertTrue(other.exists())

  @contextmanager
  def runtime_case(self, *, setup_error=None, close_error=None, cleanup_error=None):
    operations = []
    clock = [0.0]
    worker = Mock()
    gadget = Mock()
    gadget.path = self.gadget

    def operation(name, error):
      def run(*args, **kwargs):
        operations.append(name)
        if error is not None:
          raise error
      return run

    def sleep(seconds):
      operations.append("backoff")
      self.assertFalse(self.lease.exists(), "Gadget lease remained during reconnect backoff")
      clock[0] += seconds

    worker.close.side_effect = operation("worker_close", close_error)
    gadget.setup.side_effect = operation("gadget_setup", setup_error)
    gadget.cleanup.side_effect = operation("gadget_cleanup", cleanup_error)
    output = self.root / "runtime-run"
    live = Mock(margin_width=0, margin_height=240, frames_sent=0, acked=0)
    with ExitStack() as stack:
      stack.enter_context(patch("sys.argv", ["runtime", "--duration", "1", "--output", str(output)]))
      stack.enter_context(patch("sys.stdout", new_callable=io.StringIO))
      stack.enter_context(patch.object(runtime, "LOCK", self.root / "lock"))
      stack.enter_context(patch.object(runtime.os, "geteuid", return_value=0))
      stack.enter_context(patch.object(runtime.os, "umask"))
      stack.enter_context(patch.object(runtime.signal, "signal"))
      stack.enter_context(patch.object(runtime.time, "monotonic", side_effect=lambda: clock[0]))
      stack.enter_context(patch.object(runtime.time, "sleep", side_effect=sleep))
      stack.enter_context(patch("tools.android_auto.frame_worker.FrameWorker", return_value=worker))
      stack.enter_context(patch.object(runtime, "Gadget", return_value=gadget))
      stack.enter_context(patch.object(runtime, "wait_accessory"))
      stack.enter_context(patch.object(runtime, "accessory_peer", return_value=nullcontext(Mock())))
      stack.enter_context(patch.object(runtime, "LiveVideoSession", return_value=live))
      yield SimpleNamespace(operations=operations, worker=worker, gadget=gadget, output=output)

  def test_failed_session_cleans_up_before_reconnect_backoff(self):
    with self.runtime_case(setup_error=RuntimeError("attachment failed")) as case:
      self.assertEqual(runtime.main(), 1)
    self.assertEqual(case.operations, ["gadget_setup", "worker_close", "gadget_cleanup", "backoff"])
    self.assertEqual(json.loads((case.output / "summary.json").read_text())["attempts"], 1)

  def test_failed_worker_close_still_cleans_gadget(self):
    with self.runtime_case(setup_error=RuntimeError("attachment failed"), close_error=RuntimeError("worker close failed")) as case:
      with self.assertRaisesRegex(RuntimeError, "worker close failed"):
        runtime.main()
    self.assertEqual(case.operations, ["gadget_setup", "worker_close", "gadget_cleanup"])
    self.assertFalse(self.lease.exists())

  def test_failed_gadget_cleanup_retains_lease_and_does_not_retry(self):
    with self.runtime_case(setup_error=RuntimeError("attachment failed"), cleanup_error=OSError("UDC busy")) as case:
      with self.assertRaisesRegex(OSError, "UDC busy"):
        runtime.main()
    self.assertEqual(case.operations, ["gadget_setup", "worker_close", "gadget_cleanup"])
    self.assertTrue(self.lease.exists())

  def test_incomplete_cleanup_keeps_lease_for_stop_post_recovery(self):
    with self.runtime_case() as case:
      def interrupted_setup(**kwargs):
        self.gadget.mkdir()
        raise RuntimeError("setup interrupted before creation bookkeeping")

      case.gadget.setup.side_effect = interrupted_setup
      with self.assertRaisesRegex(RuntimeError, "cleanup|gadget|leftover"):
        runtime.main()
    self.assertEqual(case.operations, ["worker_close", "gadget_cleanup"])
    self.assertTrue(self.lease.exists())
    self.assertTrue(self.gadget.exists())

  def test_user_exit_cleans_up_without_reclaiming_projection(self):
    with self.runtime_case() as case:
      with patch.object(runtime, "project_live", side_effect=runtime.PeerRequestedStop("User left AA")):
        self.assertEqual(runtime.main(), 1)
    self.assertEqual(case.operations, ["gadget_setup", "worker_close", "gadget_cleanup"])
    self.assertFalse(self.lease.exists())


class TestLiveProjectionLoop(unittest.TestCase):
  def run_loop(self, *, frame_metadata=None, focus_change=None, focused=True, seconds=0.2, step=0.04, hang=False):
    clock = [0.0]
    target = session()
    if focused:
      grant_focus(target)
    requests = []
    count = [0]

    def pump(timeout):
      clock[0] += step
      count[0] += 1
      if focus_change is not None:
        focus_change(target, count[0])
      if target.unacked:
        acknowledge(target, sid=target.session_id, count=target.unacked)

    target.pump = pump
    worker = SimpleNamespace(busy=False, pid=12345)

    def request(force_keyframe=False):
      self.assertFalse(worker.busy)
      worker.busy = True
      worker.captured_at = clock[0]
      worker.force_keyframe = force_keyframe
      requests.append(force_keyframe)

    def poll(timeout):
      if hang:
        return None
      worker.busy = False
      metadata = {"captured_at": worker.captured_at, "source_age_seconds": 0.0, "stale": False, "cpu_seconds": 0.0}
      if frame_metadata is not None:
        metadata.update(frame_metadata(len(requests), clock[0]))
      return KEYFRAME if worker.force_keyframe else PFRAME, metadata

    worker.request, worker.poll = request, poll
    with patch.object(runtime.time, "monotonic", side_effect=lambda: clock[0]), \
         patch.object(runtime.time, "process_time", return_value=0), patch.object(runtime, "atomic_json"):
      result = runtime.project_live(target, worker, seconds, 30, Path("unused-status.json"))
    return result, requests, target

  def test_source_plus_encoding_age_drops_frame_then_requests_keyframe(self):
    result, requests, target = self.run_loop(frame_metadata=lambda index, now: {"source_age_seconds": 0.49 if index == 1 else 0.0})
    self.assertEqual(result["dropped_frames"], 1)
    self.assertGreater(result["live_frames"], 0)
    self.assertEqual(requests[:2], [True, True])
    self.assertLessEqual(target.max_pending, 2)

  def test_focus_epoch_change_discards_in_progress_frame(self):
    def change(target, count):
      if count == 2:
        target.handle(9, 0x8008, field(1, 2))
        grant_focus(target)

    result, requests, target = self.run_loop(focus_change=change)
    self.assertEqual(result["dropped_frames"], 1)
    self.assertEqual(requests[:2], [True, True])
    self.assertEqual(target.session_id, 2)

  def test_native_display_focus_does_not_generate_frames_or_timeout(self):
    result, requests, _ = self.run_loop(focused=False, seconds=7, step=1)
    self.assertEqual(result["frames_sent"], 0)
    self.assertEqual(requests, [])

  def test_worker_hang_is_bounded(self):
    with self.assertRaisesRegex(TimeoutError, "500 ms|expired"):
      self.run_loop(hang=True, seconds=2, step=0.1)

  def test_repeated_dropped_frames_cannot_freeze_live_display_indefinitely(self):
    def metadata(index, now):
      return {} if index == 1 else {"captured_at": now - 0.3}

    with self.assertRaises(TimeoutError):
      self.run_loop(frame_metadata=metadata, seconds=2)

  def test_last_live_frame_expires_from_source_time_not_send_time(self):
    def metadata(index, now):
      return {"source_age_seconds": 0.35} if index == 1 else {"captured_at": now - 0.3}

    # First capture is at 40 ms, sent at 80 ms, source already 350 ms old. It
    # expires at 190 ms; a 500 ms since-send watchdog would wait until 580 ms.
    with self.assertRaisesRegex(TimeoutError, "freshness"):
      self.run_loop(frame_metadata=metadata, seconds=0.3)

  def test_unavailable_frames_can_continue_without_live_source_timestamp(self):
    result, _, _ = self.run_loop(frame_metadata=lambda index, now: {"stale": True, "source_age_seconds": None})
    self.assertEqual(result["live_frames"], 0)
    self.assertGreater(result["stale_frames"], 0)


if __name__ == "__main__":
  unittest.main()
