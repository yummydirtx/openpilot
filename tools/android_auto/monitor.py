"""Read-only coexistence measurements; parked observations are not a road test.

Poll Cereal at 100 Hz and aggregate into one-second JSONL records. This records
native card cumulative timing offset (carState.cumLagMs), model execution/drop metrics,
and native lag/communication events. controlsState has no currently published
loop-execution/deadline field; subscriber arrival intervals cannot prove its
100 Hz control deadlines. No GPS, vehicle identity, message payloads, process
command lines, or parameter values are collected.

Examples, on the device from the isolated deployment directory:
  python3 -m tools.android_auto.monitor record --seconds 30 --output baseline.jsonl
  python3 -m tools.android_auto.monitor record --seconds 60 --pid 1234 --output projection.jsonl
  python3 -m tools.android_auto.monitor compare baseline.jsonl projection.jsonl
"""

import argparse
from dataclasses import dataclass
import errno
import json
import math
import os
from pathlib import Path
import time


SERVICES = ("carState", "selfdriveState", "deviceState", "pandaStates", "managerState", "controlsState", "modelV2", "onroadEvents")
PANDA_COUNTERS = ("rxBufferOverflow", "txBufferOverflow", "spiErrorCount", "safetyRxInvalid", "safetyTxBlocked")
BUS_COUNTERS = ("busOffCnt", "totalErrorCnt", "totalTxLostCnt", "totalRxLostCnt", "canCoreResetCnt")
TIMING_EVENTS = {"selfdrivedLagging", "controlsdLagging", "modeldLagging", "commIssue", "commIssueAvgFreq",
                 "processNotRunning", "canError", "canBusMissing", "cameraMalfunction", "cameraFrameRate"}


def value(obj, name, default=None):
  """Missing schema fields remain unknown, including fields on older devices."""
  schema = getattr(obj, "schema", None)
  if schema is not None and name not in schema.fields:
    return default
  return getattr(obj, name, default)


def number(item, scale=1):
  if item is None:
    return None
  result = float(item) * scale
  return result if math.isfinite(result) else None


def numeric_list(obj, name):
  items = value(obj, name)
  return [number(item) for item in items] if items is not None else None


@dataclass
class Metric:
  count: int = 0
  total: float = 0
  minimum: float = math.inf
  maximum: float = -math.inf
  first: float | None = None
  last: float | None = None
  first_time: float | None = None
  last_time: float | None = None

  def add(self, item, sample_time=None):
    item = number(item)
    if item is not None:
      if not self.count:
        self.first, self.first_time = item, sample_time
      self.last, self.last_time = item, sample_time
      self.count += 1
      self.total += item
      self.minimum = min(self.minimum, item)
      self.maximum = max(self.maximum, item)

  def result(self):
    return ({"count": self.count, "min": self.minimum, "max": self.maximum, "mean": self.total / self.count,
             "first": self.first, "last": self.last, "firstSampleTimeSeconds": self.first_time, "lastSampleTimeSeconds": self.last_time}
            if self.count else None)


def pandas_snapshot(pandas):
  result = []
  for index, panda in enumerate(pandas):
    entry = {"index": index, "type": str(panda.pandaType), "faults": sorted(str(fault) for fault in panda.faults),
             "heartbeatLost": value(panda, "heartbeatLost"), "safetyRxChecksInvalid": value(panda, "safetyRxChecksInvalid"),
             **{name: value(panda, name) for name in PANDA_COUNTERS}, "buses": []}
    for bus in range(3):
      state = value(panda, f"canState{bus}")
      if state is not None:
        entry["buses"].append({"bus": bus, "busOff": value(state, "busOff"), "errorPassive": value(state, "errorPassive"),
                               **{name: value(state, name) for name in BUS_COUNTERS}})
    result.append(entry)
  return result


class Accumulator:
  """Keep extrema and transient failure/event names between one-second writes."""

  def __init__(self):
    self.metrics = {name: Metric() for name in ("cardLagMs", "modelExecutionMs", "modelFrameDropPercent")}
    self.events = set()
    self.alert_types = set()
    self.service_failures = set()
    self.can_failures = set()
    self.panda_failures = set()
    self.process_states = set()
    self.publisher_age_max_ms = {}
    self.polls = 0

  def observe(self, sm, now):
    self.polls += 1
    for service in SERVICES:
      if not sm.seen[service]:
        self.service_failures.add(f"{service}:unseen")
        continue
      for check in ("alive", "valid"):
        if not getattr(sm, check)[service]:
          self.service_failures.add(f"{service}:{check}")
      age = max(0, now - sm.logMonoTime[service] / 1e9) * 1000
      self.publisher_age_max_ms[service] = max(age, self.publisher_age_max_ms.get(service, 0))
    if sm.updated["carState"]:
      cs = sm["carState"]
      self.metrics["cardLagMs"].add(value(cs, "cumLagMs"), sm.logMonoTime["carState"] / 1e9)
      if not cs.canValid:
        self.can_failures.add("canInvalid")
      if value(cs, "canTimeout", False):
        self.can_failures.add("canTimeout")
    if sm.updated["modelV2"]:
      self.metrics["modelExecutionMs"].add(number(value(sm["modelV2"], "modelExecutionTime"), 1000), sm.logMonoTime["modelV2"] / 1e9)
      self.metrics["modelFrameDropPercent"].add(value(sm["modelV2"], "frameDropPerc"), sm.logMonoTime["modelV2"] / 1e9)
    if sm.updated["onroadEvents"]:
      self.events.update(str(event.name) for event in sm["onroadEvents"])
    if sm.updated["selfdriveState"]:
      alert = str(sm["selfdriveState"].alertType)
      if alert:
        self.alert_types.add(alert)
    if sm.updated["pandaStates"]:
      for panda in pandas_snapshot(sm["pandaStates"]):
        self.panda_failures.update(f"panda{panda['index']}:{fault}" for fault in panda["faults"])
        for key in ("heartbeatLost", "safetyRxChecksInvalid"):
          if panda[key]:
            self.panda_failures.add(f"panda{panda['index']}:{key}")
        for bus in panda["buses"]:
          if bus["busOff"]:
            self.panda_failures.add(f"panda{panda['index']}:bus{bus['bus']}:busOff")
    if sm.updated["managerState"]:
      self.process_states.update((str(proc.name), int(proc.pid), bool(proc.running), bool(proc.shouldBeRunning))
                                 for proc in sm["managerState"].processes)

  def result(self, sm, elapsed):
    cs, ds, ss = (sm[name] for name in ("carState", "deviceState", "selfdriveState"))
    return {
      "version": 1, "elapsedSeconds": elapsed, "polls": self.polls,
      "services": {name: {"seen": sm.seen[name], "alive": sm.alive[name], "valid": sm.valid[name],
                           "publisherAgeMaxMs": self.publisher_age_max_ms.get(name)} for name in SERVICES},
      "serviceFailures": sorted(self.service_failures), "canFailures": sorted(self.can_failures),
      "pandaFailures": sorted(self.panda_failures), "events": sorted(self.events), "alertTypes": sorted(self.alert_types),
      "metrics": {name: metric.result() for name, metric in self.metrics.items()},
      "car": {"speedMps": number(cs.vEgo), "canValid": cs.canValid, "canTimeout": value(cs, "canTimeout"),
              "canErrorCounter": value(cs, "canErrorCounter")},
      "selfdrive": {"state": str(ss.state), "enabled": ss.enabled, "alertSize": str(ss.alertSize), "alertStatus": str(ss.alertStatus)},
      "device": {"started": ds.started, "thermalStatus": str(ds.thermalStatus), "memoryUsagePercent": number(ds.memoryUsagePercent),
                 "cpuUsagePercent": numeric_list(ds, "cpuUsagePercent"), "cpuTempC": numeric_list(ds, "cpuTempC"),
                 "gpuTempC": numeric_list(ds, "gpuTempC")},
      "pandas": pandas_snapshot(sm["pandaStates"]),
      "processStates": [{"name": name, "pid": pid, "running": running, "shouldBeRunning": expected}
                        for name, pid, running, expected in sorted(self.process_states)],
    }


def parse_proc_stat(text):
  # comm can include spaces or parentheses; fields after its final ')' are fixed.
  fields = text[text.rindex(")") + 2:].split()
  return {"ppid": int(fields[1]), "cpuTicks": int(fields[11]) + int(fields[12]), "startTicks": int(fields[19]), "rssPages": int(fields[21])}


class ProcSampler:
  def __init__(self, pids=(), root=Path("/proc")):
    self.pids = tuple(pids)
    self.root = root
    self.previous = {}
    self.cpu_previous = None
    self.clock_ticks = os.sysconf("SC_CLK_TCK")
    self.page_size = os.sysconf("SC_PAGE_SIZE")

  def _scan_parent_table(self):
    """One bounded PPID snapshot for kernels without task/*/children files.

    Read stat only, never cmdline/environ. Unrelated processes exiting during
    enumeration are normal; permission failures leave possible descendants
    unknown and therefore make coverage explicitly incomplete.
    """
    parents, records, errors = {}, {}, []
    try:
      entries = self.root.iterdir()
      scanned = 0
      for entry in entries:
        if not entry.name.isdecimal():
          continue
        if scanned >= 4096:
          errors.append({"operation": "ppid_scan_limit"})
          break
        scanned += 1
        pid = int(entry.name)
        try:
          stat = parse_proc_stat((entry / "stat").read_text())
        except OSError as error:
          if error.errno not in (errno.ENOENT, errno.ESRCH):
            errors.append({"pid": pid, "operation": "ppid_scan", "errno": error.errno})
          continue
        except (ValueError, IndexError):
          errors.append({"pid": pid, "operation": "ppid_scan", "errno": None})
          continue
        records[pid] = stat
        parents.setdefault(stat["ppid"], []).append(pid)
    except OSError as error:
      errors.append({"operation": "ppid_scan", "errno": error.errno})
    # Keep diagnostic output bounded even under a restrictive procfs mount.
    if len(errors) > 16:
      errors = errors[:16] + [{"operation": "ppid_scan_additional_errors", "count": len(errors) - 16}]
    return parents, records, errors

  def sample(self, now):
    result = {"busyPercent": None, "processes": [], "treeRootPids": list(self.pids),
              "treeComplete": True if self.pids else None, "treeErrors": [], "treeMethod": "children" if self.pids else None}
    try:
      fields = [int(item) for item in (self.root / "stat").read_text().splitlines()[0].split()[1:9]]
      total, idle = sum(fields), fields[3] + fields[4]
      if self.cpu_previous is not None:
        total_delta, idle_delta = total - self.cpu_previous[0], idle - self.cpu_previous[1]
        if total_delta > 0:
          result["busyPercent"] = 100 * (1 - idle_delta / total_delta)
      self.cpu_previous = total, idle
    except (OSError, ValueError, IndexError):
      pass
    pending, seen = list(self.pids), set()
    parent_table, stat_cache = None, {}
    while pending and len(seen) < 128:
      pid = pending.pop()
      if pid in seen:
        continue
      seen.add(pid)
      try:
        current = stat_cache.get(pid)
        if current is None:
          current = parse_proc_stat((self.root / str(pid) / "stat").read_text())
        old = self.previous.get(pid)
        cpu = None
        if old is not None and old[1]["startTicks"] == current["startTicks"] and now > old[0]:
          cpu = 100 * (current["cpuTicks"] - old[1]["cpuTicks"]) / self.clock_ticks / (now - old[0])
        self.previous[pid] = now, current
        result["processes"].append({"pid": pid, "rssMiB": current["rssPages"] * self.page_size / (1024 ** 2),
                                    "cpuPercentOneCore": cpu})
        fallback = parent_table is not None
        if not fallback:
          try:
            children = (self.root / str(pid) / "task" / str(pid) / "children").read_text().split()
            pending.extend(int(child) for child in children)
          except (OSError, ValueError) as error:
            if getattr(error, "errno", None) == errno.ENOENT:
              parent_table, stat_cache, scan_errors = self._scan_parent_table()
              result["treeMethod"] = "ppid_scan"
              result["treeErrors"].extend(scan_errors)
              if scan_errors:
                result["treeComplete"] = False
              fallback = True
            else:
              result["treeComplete"] = False
              result["treeErrors"].append({"pid": pid, "operation": "children", "errno": getattr(error, "errno", None)})
        if fallback:
          scanned = stat_cache.get(pid)
          if scanned is None or scanned["startTicks"] != current["startTicks"]:
            result["treeComplete"] = False
            result["treeErrors"].append({"pid": pid, "operation": "process_changed_during_ppid_scan"})
          else:
            pending.extend(parent_table.get(pid, ()))
      except (OSError, ValueError, IndexError) as error:
        result["processes"].append({"pid": pid, "missing": True})
        result["treeComplete"] = False
        result["treeErrors"].append({"pid": pid, "operation": "stat", "errno": getattr(error, "errno", None)})
    if pending:
      result["treeComplete"] = False
      result["treeErrors"].append({"operation": "process_limit"})
    self.previous = {pid: info for pid, info in self.previous.items() if pid in seen}
    return result


def counter_values(record):
  result = {"car.canErrorCounter": record["car"].get("canErrorCounter")}
  for panda in record["pandas"]:
    prefix = f"panda{panda['index']}"
    result.update({f"{prefix}.{name}": panda.get(name) for name in PANDA_COUNTERS})
    for bus in panda["buses"]:
      result.update({f"{prefix}.bus{bus['bus']}.{name}": bus.get(name) for name in BUS_COUNTERS})
  return {name: count for name, count in result.items() if count is not None}


def metric_trend(records, name):
  observed = [(record["elapsedSeconds"], record["metrics"].get(name)) for record in records if record["metrics"].get(name) is not None]
  if not observed:
    return None
  first_elapsed, first = observed[0]
  last_elapsed, last = observed[-1]
  # New captures retain exact metric endpoints and publisher times. Older
  # captures only stored bucket means; label that approximation explicitly.
  exact = first.get("firstSampleTimeSeconds") is not None and last.get("lastSampleTimeSeconds") is not None
  first_value, last_value = (first["first"], last["last"]) if exact else (first["mean"], last["mean"])
  span = last["lastSampleTimeSeconds"] - first["firstSampleTimeSeconds"] if exact else last_elapsed - first_elapsed
  delta = last_value - first_value
  return {"basis": "sample_endpoints" if exact else "bucket_means", "first": first_value, "last": last_value,
          "delta": delta, "spanSeconds": span, "changePerSecond": delta / span if span > 0 else None}


def counter_rates(records):
  observed = {}
  for record in records:
    for name, count in counter_values(record).items():
      observed.setdefault(name, []).append((record["elapsedSeconds"], count))
  result = {}
  for name, values in observed.items():
    first_time, first = values[0]
    last_time, last = values[-1]
    reset = any(current[1] < previous[1] for previous, current in zip(values, values[1:], strict=False))
    span = last_time - first_time
    result[name] = {"first": first, "last": last, "spanSeconds": span, "resetObserved": reset,
                    "perSecond": (last - first) / span if span > 0 and not reset else None}
  return result


def summarize(records):
  records = list(records)
  if not records:
    raise ValueError("A comparison needs at least one record in each capture")
  result = {"samples": len(records), "seconds": records[-1]["elapsedSeconds"], "metrics": {}, "processes": {},
            "counterIncreases": {}, "counterResets": []}
  for name in ("serviceFailures", "canFailures", "pandaFailures", "events", "alertTypes"):
    result[name] = sorted({item for record in records for item in record[name]})
  for name in records[0]["metrics"]:
    buckets = [record["metrics"][name] for record in records if record["metrics"].get(name) is not None]
    result["metrics"][name] = ({"max": max(bucket["max"] for bucket in buckets), "min": min(bucket["min"] for bucket in buckets),
                                "mean": sum(bucket["mean"] * bucket["count"] for bucket in buckets) / sum(bucket["count"] for bucket in buckets),
                                "count": sum(bucket["count"] for bucket in buckets)} if buckets else None)
  result["cardLagTrendMs"] = metric_trend(records, "cardLagMs")
  result["counterRates"] = counter_rates(records)
  result["counterResets"] = sorted(name for name, rate in result["counterRates"].items() if rate["resetObserved"])
  first_counters = counter_values(records[0])
  for record in records:
    for proc in record["processStates"]:
      entry = result["processes"].setdefault(proc["name"], {"pids": [], "unexpectedStop": False})
      if proc["running"] and proc["pid"] not in entry["pids"]:
        entry["pids"].append(proc["pid"])
      entry["unexpectedStop"] |= proc["shouldBeRunning"] and not proc["running"]
    for name, count in counter_values(record).items():
      first = first_counters.setdefault(name, count)
      if count > first:
        result["counterIncreases"][name] = max(result["counterIncreases"].get(name, 0), count - first)
  result["firstCounters"] = first_counters
  result["lastCounters"] = counter_values(records[-1])
  result["maxSpeedMps"] = max(record["car"]["speedMps"] or 0 for record in records)
  result["thermalStatuses"] = sorted({record["device"]["thermalStatus"] for record in records})
  for key, metric in (("maxCpuTempC", "cpuTempC"), ("maxGpuTempC", "gpuTempC")):
    values = [temp for record in records for temp in (record["device"].get(metric) or []) if temp is not None]
    result[key] = max(values) if values else None
  memory = [record["device"]["memoryUsagePercent"] for record in records if record["device"]["memoryUsagePercent"] is not None]
  result["maxMemoryUsagePercent"] = max(memory) if memory else None
  tracked = [process for record in records for process in record.get("proc", {}).get("processes", [])]
  result["processResourceCoverage"] = {"observations": len(tracked), "present": sum(not process.get("missing", False) for process in tracked)}
  tree_samples = [record["proc"] for record in records if record.get("proc", {}).get("treeRootPids") or record.get("proc", {}).get("processes")]
  result["processTreeCoverage"] = {"requestedSamples": len(tree_samples),
                                   "complete": sum(sample.get("treeComplete") is True for sample in tree_samples),
                                   "incomplete": sum(sample.get("treeComplete") is False for sample in tree_samples),
                                   "unknown": sum(sample.get("treeComplete") is None for sample in tree_samples)}
  return result


def compare(baseline, projected):
  base, active = summarize(baseline), summarize(projected)
  signals = {}
  for key in ("serviceFailures", "canFailures", "pandaFailures", "events", "alertTypes"):
    signals["new" + key[0].upper() + key[1:]] = sorted(set(active[key]) - set(base[key]))
  signals["newTimingCommunicationEvents"] = sorted(set(signals["newEvents"]) & TIMING_EVENTS)
  signals["processChanges"] = []
  for name in sorted(set(base["processes"]) | set(active["processes"])):
    before, after = base["processes"].get(name), active["processes"].get(name)
    if before != after:
      signals["processChanges"].append({"name": name, "baseline": before, "projected": after})
  signals["counterIncreasesSinceBaseline"] = {name: count - base["lastCounters"][name] for name, count in active["lastCounters"].items()
                                               if name in base["lastCounters"] and count > base["lastCounters"][name]}
  signals["counterResetsSinceBaseline"] = [name for name, count in active["lastCounters"].items()
                                           if name in base["lastCounters"] and count < base["lastCounters"][name]]
  signals["counterRateComparisons"] = {}
  for name in sorted(set(base["counterRates"]) & set(active["counterRates"])):
    before, after = base["counterRates"][name]["perSecond"], active["counterRates"][name]["perSecond"]
    if before != 0 or after != 0:
      signals["counterRateComparisons"][name] = {"baselinePerSecond": before, "projectedPerSecond": after,
                                                "changePerSecond": after - before if before is not None and after is not None else None}
  before, after = base["cardLagTrendMs"], active["cardLagTrendMs"]
  signals["cardLagSlopeChangeMsPerSecond"] = (after["changePerSecond"] - before["changePerSecond"]
                                             if before and after and before["changePerSecond"] is not None and after["changePerSecond"] is not None
                                             else None)
  return {"baseline": base, "projected": active, "signals": signals,
          "limitations": ["Parked coexistence evidence only; no onroad safety conclusion.",
                          "cardLagMs is cumulative phase offset from a fixed 100 Hz schedule, not instantaneous execution time or message latency.",
                          "Compare card lag slopes as well as extrema; existing phase offset does not reset at capture start.",
                          "controlsState has no currently published control-loop execution or deadline metric.",
                          "Polling conflated subscriptions can miss messages; counts are observed, not publisher counts.",
                          "Counter rates use each capture's first-to-last observed counter interval; resets make a rate unavailable.",
                          "Counter increases since baseline include any gap between captures and cannot be attributed to projection alone.",
                          "Present PIDs alone do not prove process-tree coverage; run the monitor with permission to read all descendant children files.",
                          "Compare adjacent captures from the same device boot and running control session."]}


def record(args):
  from openpilot.cereal import messaging
  sm = messaging.SubMaster(list(SERVICES))
  sampler = ProcSampler(args.pid)
  # Warm subscriptions before evaluating liveness, never persist default messages.
  warmup_end = time.monotonic() + args.warmup
  while time.monotonic() < warmup_end:
    sm.update(10)
  start = time.monotonic()
  sampler.sample(start)
  next_sample = start + 1.0
  bucket = Accumulator()
  with args.output.open("x") as output:
    while True:
      sm.update(0)
      now = time.monotonic()
      bucket.observe(sm, now)
      if now >= min(next_sample, start + args.seconds):
        result = bucket.result(sm, now - start)
        result["proc"] = sampler.sample(now)
        output.write(json.dumps(result, allow_nan=False) + "\n")
        output.flush()
        if now >= start + args.seconds:
          break
        bucket = Accumulator()
        next_sample = now + 1.0
      time.sleep(max(0, 0.01 - (time.monotonic() - now)))


def main():
  parser = argparse.ArgumentParser(description=__doc__)
  commands = parser.add_subparsers(dest="command", required=True)
  capture = commands.add_parser("record")
  capture.add_argument("--seconds", type=float, default=30)
  capture.add_argument("--warmup", type=float, default=2)
  capture.add_argument("--pid", action="append", type=int, default=[])
  capture.add_argument("--output", type=Path, required=True)
  comparison = commands.add_parser("compare")
  comparison.add_argument("baseline", type=Path)
  comparison.add_argument("projected", type=Path)
  args = parser.parse_args()
  if args.command == "record":
    if not 0 < args.seconds <= 3600 or not 0 <= args.warmup <= 30 or any(pid <= 0 for pid in args.pid):
      parser.error("Use a duration up to one hour, a warmup of up to 30 seconds, and positive PIDs")
    record(args)
  else:
    captures = [[json.loads(line) for line in path.read_text().splitlines() if line.strip()] for path in (args.baseline, args.projected)]
    print(json.dumps(compare(*captures), indent=2, allow_nan=False))


if __name__ == "__main__":
  main()
