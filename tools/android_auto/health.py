"""Read a small, identity-free vehicle/process baseline on the comma."""

import json
import time


def main():
  import openpilot.cereal.messaging as messaging

  sm = messaging.SubMaster(["carState", "selfdriveState", "deviceState", "pandaStates", "managerState"])
  deadline = time.monotonic() + 5
  while time.monotonic() < deadline:
    sm.update(200)
  device = sm["deviceState"]
  result = {
    "valid": sm.valid,
    "alive": sm.alive,
    "speed_mps": sm["carState"].vEgo,
    "can_valid": sm["carState"].canValid,
    "engaged": sm["selfdriveState"].enabled,
    "started": device.started,
    "thermal_status": str(device.thermalStatus),
    "cpu_temps_c": list(device.cpuTempC),
    "gpu_temps_c": list(device.gpuTempC),
    "memory_usage_percent": device.memoryUsagePercent,
    "pandas": [{"type": str(p.pandaType), "faults": [str(f) for f in p.faults]} for p in sm["pandaStates"]],
    "processes": [{"name": p.name, "pid": p.pid, "running": p.running, "should_run": p.shouldBeRunning}
                  for p in sm["managerState"].processes],
  }
  print(json.dumps(result, indent=2))


if __name__ == "__main__":
  main()
