from copy import deepcopy
from pathlib import Path
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch

from tools.android_auto.monitor import Accumulator, ProcSampler, SERVICES, compare, parse_proc_stat, summarize
from tools.android_auto.test_live_state import FakeSubMaster


def sample_master():
  sm = FakeSubMaster()
  for name in SERVICES:
    sm.seen[name] = sm.alive[name] = sm.valid[name] = sm.updated[name] = True
    sm.recv_time[name] = 100.0
    sm.logMonoTime[name] = 100_000_000_000
  sm["carState"].canValid = True
  sm["carState"].canTimeout = False
  sm["carState"].cumLagMs = -8.0
  sm["carState"].canErrorCounter = 4
  sm["selfdriveState"].alertType = ""
  sm["deviceState"].thermalStatus = "ok"
  sm["deviceState"].memoryUsagePercent = 70
  sm["deviceState"].cpuTempC = [61.5, 63.0]
  sm["deviceState"].gpuTempC = [62.5]
  sm["deviceState"].cpuUsagePercent = [25, 30]
  sm["pandaStates"][0].faults = ["interruptRateCan2"]
  sm["pandaStates"][0].rxBufferOverflow = 0
  sm.data["modelV2"] = NS(modelExecutionTime=0.025, frameDropPerc=0)
  sm.data["managerState"] = NS(processes=[NS(name="controlsd", pid=123, running=True, shouldBeRunning=True)])
  return sm


def record(sm=None, elapsed=1.0):
  sm = sample_master() if sm is None else sm
  accumulator = Accumulator()
  accumulator.observe(sm, 100)
  return accumulator.result(sm, elapsed)


class TestCoexistenceMonitor(unittest.TestCase):
  def test_transient_can_error_and_lag_spike_survive_one_second_aggregation(self):
    sm = sample_master()
    bucket = Accumulator()
    bucket.observe(sm, 100)
    sm["carState"].canValid = False
    sm["carState"].cumLagMs = 25
    sm.data["onroadEvents"] = [NS(name="selfdrivedLagging")]
    bucket.observe(sm, 100.01)
    sm["carState"].canValid = True
    sm["carState"].cumLagMs = -5
    sm.data["onroadEvents"] = []
    bucket.observe(sm, 100.02)
    result = bucket.result(sm, 1.0)
    self.assertTrue(result["car"]["canValid"])
    self.assertEqual(result["canFailures"], ["canInvalid"])
    self.assertIn("selfdrivedLagging", result["events"])
    self.assertEqual(result["metrics"]["cardLagMs"]["max"], 25)
    self.assertEqual(result["metrics"]["modelExecutionMs"]["mean"], 25)

  def test_native_communication_event_and_invalid_service_are_separate(self):
    sm = sample_master()
    sm.valid["controlsState"] = False
    sm.data["onroadEvents"] = [NS(name="commIssue")]
    active = record(sm)
    self.assertIn("controlsState:valid", active["serviceFailures"])
    result = compare([record()], [active])
    self.assertEqual(result["signals"]["newTimingCommunicationEvents"], ["commIssue"])

  def test_preexisting_panda_fault_is_not_reported_as_new(self):
    baseline = record()
    result = compare([baseline], [deepcopy(baseline)])
    self.assertEqual(result["signals"]["newPandaFailures"], [])
    self.assertEqual(result["projected"]["pandaFailures"], ["panda0:interruptRateCan2"])
    sm = sample_master()
    sm["pandaStates"][0].heartbeatLost = True
    result = compare([baseline], [record(sm)])
    self.assertEqual(result["signals"]["newPandaFailures"], ["panda0:heartbeatLost"])

  def test_process_restart_and_transient_stop_are_reported(self):
    baseline = record()
    sm = sample_master()
    bucket = Accumulator()
    sm["managerState"].processes[0].running = False
    bucket.observe(sm, 100)
    sm["managerState"].processes[0].running = True
    sm["managerState"].processes[0].pid = 456
    bucket.observe(sm, 100.5)
    result = compare([baseline], [bucket.result(sm, 1)])
    changed = result["signals"]["processChanges"]
    self.assertEqual(len(changed), 1)
    self.assertEqual(changed[0]["projected"]["pids"], [456])
    self.assertTrue(changed[0]["projected"]["unexpectedStop"])

  def test_counters_compare_against_baseline_and_report_resets(self):
    baseline = record()
    sm = sample_master()
    sm["carState"].canErrorCounter = 7
    sm["pandaStates"][0].rxBufferOverflow = 2
    active = record(sm)
    result = compare([baseline], [active])
    self.assertEqual(result["signals"]["counterIncreasesSinceBaseline"], {"car.canErrorCounter": 3, "panda0.rxBufferOverflow": 2})
    sm["carState"].canErrorCounter = 0
    reset = record(sm, 2)
    self.assertIn("car.canErrorCounter", summarize([active, reset])["counterResets"])
    self.assertIn("car.canErrorCounter", compare([baseline], [reset])["signals"]["counterResetsSinceBaseline"])

  def test_unavailable_schema_metrics_are_unknown_not_zero(self):
    sm = sample_master()
    del sm["carState"].cumLagMs
    del sm["modelV2"].modelExecutionTime
    output = record(sm)
    self.assertIsNone(output["metrics"]["cardLagMs"])
    self.assertIsNone(output["metrics"]["modelExecutionMs"])
    self.assertIsNone(summarize([output])["metrics"]["cardLagMs"])

  def test_summary_weights_by_observed_message_count(self):
    first, second = record(), record(elapsed=2)
    first["metrics"]["cardLagMs"] = {"count": 100, "mean": 2, "min": 0, "max": 4}
    second["metrics"]["cardLagMs"] = {"count": 50, "mean": 8, "min": 6, "max": 10}
    metric = summarize([first, second])["metrics"]["cardLagMs"]
    self.assertEqual(metric, {"count": 150, "mean": 4, "min": 0, "max": 10})

  def test_counter_rates_use_observed_interval_not_total_recording_duration(self):
    first, last = record(elapsed=1), record(elapsed=30)
    first["pandas"][0]["safetyTxBlocked"] = 1000
    last["pandas"][0]["safetyTxBlocked"] = 3900
    rate = summarize([first, last])["counterRates"]["panda0.safetyTxBlocked"]
    self.assertEqual(rate["spanSeconds"], 29)
    self.assertEqual(rate["perSecond"], 100)
    self.assertFalse(rate["resetObserved"])
    self.assertIsNone(summarize([first])["counterRates"]["panda0.safetyTxBlocked"]["perSecond"])

  def test_any_counter_decrease_invalidates_rate_even_if_above_original_value(self):
    captures = [record(elapsed=1), record(elapsed=2), record(elapsed=3)]
    for capture, count in zip(captures, (100, 120, 115), strict=True):
      capture["car"]["canErrorCounter"] = count
    result = summarize(captures)
    self.assertIn("car.canErrorCounter", result["counterResets"])
    self.assertIsNone(result["counterRates"]["car.canErrorCounter"]["perSecond"])

  def test_cumulative_lag_baseline_offset_does_not_change_slope(self):
    baseline = [record(elapsed=1), record(elapsed=11)]
    active = deepcopy(baseline)
    for capture, first, last, sample_start, sample_end in (
      (baseline[0], 3500, 3505, 100, 101), (baseline[1], 3545, 3550, 109, 110),
      (active[0], 4500, 4505, 200, 201), (active[1], 4545, 4550, 209, 210),
    ):
      capture["metrics"]["cardLagMs"].update(first=first, last=last, firstSampleTimeSeconds=sample_start, lastSampleTimeSeconds=sample_end)
    result = compare(baseline, active)
    self.assertEqual(result["baseline"]["cardLagTrendMs"]["changePerSecond"], 5)
    self.assertEqual(result["projected"]["cardLagTrendMs"]["first"], 4500)
    self.assertEqual(result["signals"]["cardLagSlopeChangeMsPerSecond"], 0)

  def test_old_capture_trend_is_labeled_as_bucket_mean_approximation(self):
    captures = [record(elapsed=1), record(elapsed=11)]
    for capture, mean in zip(captures, (3000, 3040), strict=True):
      capture["metrics"]["cardLagMs"] = {"count": 100, "mean": mean, "min": mean - 1, "max": mean + 1}
    trend = summarize(captures)["cardLagTrendMs"]
    self.assertEqual(trend["basis"], "bucket_means")
    self.assertEqual(trend["changePerSecond"], 4)

  def test_missing_pid_observations_do_not_claim_resource_measurement(self):
    capture = record()
    capture["proc"] = {"processes": [{"pid": 42, "missing": True}, {"pid": 43, "cpuPercentOneCore": 5, "rssMiB": 10}]}
    self.assertEqual(summarize([capture])["processResourceCoverage"], {"observations": 2, "present": 1})
    self.assertEqual(summarize([capture])["processTreeCoverage"], {"requestedSamples": 1, "complete": 0, "incomplete": 0, "unknown": 1})

  def test_children_permission_error_marks_tree_incomplete_even_when_parent_is_present(self):
    with tempfile.TemporaryDirectory() as directory:
      root = Path(directory)
      (root / "42").mkdir()
      fields = ["S"] + [str(index) for index in range(4, 53)]
      (root / "42/stat").write_text("42 (automaxxing) " + " ".join(fields))
      original = Path.read_text

      def read(path, *args, **kwargs):
        if path.name == "children":
          raise PermissionError(13, "Permission denied")
        return original(path, *args, **kwargs)

      with patch.object(Path, "read_text", read):
        sample = ProcSampler([42], root=root).sample(100)
      self.assertEqual(len(sample["processes"]), 1)
      self.assertNotIn("missing", sample["processes"][0])
      self.assertFalse(sample["treeComplete"])
      self.assertEqual(sample["treeErrors"], [{"pid": 42, "operation": "children", "errno": 13}])

  def test_readable_child_tree_and_leaf_files_are_complete(self):
    with tempfile.TemporaryDirectory() as directory:
      root = Path(directory)
      fields = ["S"] + [str(index) for index in range(4, 53)]
      for pid, children in ((42, "43"), (43, "")):
        (root / str(pid) / "task" / str(pid)).mkdir(parents=True)
        (root / str(pid) / "stat").write_text(f"{pid} (automaxxing) " + " ".join(fields))
        (root / str(pid) / "task" / str(pid) / "children").write_text(children)
      sample = ProcSampler([42], root=root).sample(100)
      self.assertTrue(sample["treeComplete"])
      self.assertEqual(sample["treeErrors"], [])
      self.assertEqual({process["pid"] for process in sample["processes"]}, {42, 43})

  @staticmethod
  def write_stat(root, pid, ppid, *, start=100, ticks=10):
    (root / str(pid)).mkdir(exist_ok=True)
    fields = ["S"] + ["0"] * 49
    fields[1], fields[11], fields[19], fields[21] = str(ppid), str(ticks), str(start), "24"
    (root / str(pid) / "stat").write_text(f"{pid} (automaxxing worker) " + " ".join(fields))

  def test_missing_children_kernel_uses_one_ppid_scan_for_whole_tree(self):
    with tempfile.TemporaryDirectory() as directory:
      root = Path(directory)
      for pid, parent in ((42, 1), (43, 42), (44, 43), (45, 42), (99, 1)):
        self.write_stat(root, pid, parent)
      sampler = ProcSampler([42], root=root)
      with patch.object(sampler, "_scan_parent_table", wraps=sampler._scan_parent_table) as scan:
        first = sampler.sample(100)
        self.assertEqual(scan.call_count, 1)
      self.assertTrue(first["treeComplete"])
      self.assertEqual(first["treeMethod"], "ppid_scan")
      self.assertEqual(first["treeErrors"], [])
      self.assertEqual({process["pid"] for process in first["processes"]}, {42, 43, 44, 45})
      self.write_stat(root, 44, 43, ticks=10 + sampler.clock_ticks)
      second = sampler.sample(101)
      worker = next(process for process in second["processes"] if process["pid"] == 44)
      self.assertEqual(worker["cpuPercentOneCore"], 100)

  def test_ppid_scan_permission_failure_keeps_known_children_but_marks_incomplete(self):
    with tempfile.TemporaryDirectory() as directory:
      root = Path(directory)
      for pid, parent in ((42, 1), (43, 42), (99, 1)):
        self.write_stat(root, pid, parent)
      original = Path.read_text

      def read(path, *args, **kwargs):
        if path == root / "99/stat":
          raise PermissionError(13, "Permission denied")
        return original(path, *args, **kwargs)

      with patch.object(Path, "read_text", read):
        sample = ProcSampler([42], root=root).sample(100)
      self.assertFalse(sample["treeComplete"])
      self.assertEqual({process["pid"] for process in sample["processes"]}, {42, 43})
      self.assertIn({"pid": 99, "operation": "ppid_scan", "errno": 13}, sample["treeErrors"])

  def test_ppid_scan_root_reuse_is_incomplete_and_does_not_attach_new_tree(self):
    with tempfile.TemporaryDirectory() as directory:
      root = Path(directory)
      self.write_stat(root, 42, 1, start=100)
      self.write_stat(root, 43, 42)
      sampler = ProcSampler([42], root=root)
      original = sampler._scan_parent_table

      def reused():
        self.write_stat(root, 42, 1, start=200)
        return original()

      with patch.object(sampler, "_scan_parent_table", reused):
        sample = sampler.sample(100)
      self.assertFalse(sample["treeComplete"])
      self.assertEqual({process["pid"] for process in sample["processes"]}, {42})
      self.assertIn({"pid": 42, "operation": "process_changed_during_ppid_scan"}, sample["treeErrors"])

  def test_ppid_scan_ignores_unrelated_process_that_exited_during_enumeration(self):
    with tempfile.TemporaryDirectory() as directory:
      root = Path(directory)
      self.write_stat(root, 42, 1)
      self.write_stat(root, 43, 42)
      (root / "99").mkdir()  # process disappeared before its stat was read
      sample = ProcSampler([42], root=root).sample(100)
      self.assertTrue(sample["treeComplete"])
      self.assertEqual({process["pid"] for process in sample["processes"]}, {42, 43})

  def test_proc_stat_parsing_handles_spaces_and_parentheses_in_comm(self):
    # Linux /proc/<pid>/stat fields 14/15,22,24: utime/stime,starttime,rss.
    fields = ["S"] + [str(index) for index in range(4, 53)]
    output = parse_proc_stat("123 (name with (parentheses)) " + " ".join(fields))
    self.assertEqual(output, {"ppid": 4, "cpuTicks": 29, "startTicks": 22, "rssPages": 24})

  def test_empty_capture_cannot_look_like_success(self):
    with self.assertRaisesRegex(ValueError, "at least one"):
      compare([], [record()])


if __name__ == "__main__":
  unittest.main()
