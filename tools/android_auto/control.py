"""Fixed-command display supervisor. Only local/project selection is accepted.

Installed explicitly as a system service; never part of vehicle control/manager.
UI heartbeats and acknowledged fresh frames are both required to blank the UI.
"""

import os
import pwd
import signal
import subprocess
import time
import uuid

from openpilot.system.ui.lib.display_handoff import STATE_DIR, REQUEST_DIR, fresh, read_json, write_json
from tools.android_auto.service import ROOT, PYTHON, UNIT


def valid_request(request, now):
  token = request.get("token")
  return (fresh(request, now) and request.get("mode") in ("local", "project")
          and isinstance(token, str) and len(token) == 32 and all(c in "0123456789abcdef" for c in token))


def prepare_directories():
  if os.geteuid() != 0:
    raise RuntimeError("Display supervisor must run as root")
  user = pwd.getpwnam("comma")
  for path in (STATE_DIR, REQUEST_DIR):
    if path.is_symlink():
      raise RuntimeError("Unexpected display-state symlink")
    path.mkdir(mode=0o755, exist_ok=True)
  os.chown(STATE_DIR, 0, 0)
  os.chmod(STATE_DIR, 0o755)
  os.chown(REQUEST_DIR, user.pw_uid, user.pw_gid)
  os.chmod(REQUEST_DIR, 0o700)


def main():
  prepare_directories()
  stopping = False

  def stop(*_):
    nonlocal stopping
    stopping = True

  for sig in (signal.SIGINT, signal.SIGTERM):
    signal.signal(sig, stop)
  token, output, process = None, None, None
  mode, phase = "local", "local"
  # A daemon restart must not reinterpret the previous UI request as a click.
  started_token = read_json(REQUEST_DIR / "request.json").get("token")
  try:
    while not stopping:
      request = read_json(REQUEST_DIR / "request.json")
      now = time.monotonic()
      if valid_request(request, now):
        token, mode = request["token"], request["mode"]
      else:
        mode = "local"
      intent = {"at": now, "token": token, "mode": mode}
      write_json(STATE_DIR / "intent.json", intent)
      if mode == "project" and token != started_token:
        # Only a new explicit selection may start/restart. No focus-stealing retry
        # after a failed attempt, an expired UI heartbeat, or service duration end.
        active = subprocess.run(["systemctl", "is-active", "--quiet", UNIT], timeout=2).returncode == 0
        if not active and process is None:
          output = ROOT / f"interactive-{uuid.uuid4().hex[:12]}"
          process = subprocess.Popen([PYTHON, "-m", "tools.android_auto.service", "start", "--managed", "--once",
                                      "--duration", "7200", "--view", "native", "--fps", "30", "--output", output.name], cwd=ROOT)
          phase = "connecting"
        started_token = token
      if process is not None and process.poll() is not None:
        if process.returncode:
          phase = "failed"
        process = None
      projection = read_json(STATE_DIR / "projection.json")
      now = time.monotonic()
      matched = fresh(projection, now) and projection.get("token") == token
      ready = mode == "project" and matched and projection.get("ready") is True
      if matched:
        phase = projection.get("phase", "local")
      if output is not None:
        runtime = read_json(output / "status.json")
        if runtime.get("phase") in ("stopped", "disconnected"):
          phase, ready = "failed", False
      if mode == "local":
        phase, ready = "local", False
      write_json(STATE_DIR / "status.json", {"at": time.monotonic(), "token": token, "ready": ready, "phase": phase,
                                            "local_requested": matched and projection.get("local_requested") is True,
                                            "ready_until": projection.get("ready_until", 0) if ready else 0}, mode=0o644)
      time.sleep(0.1)
  finally:
    write_json(STATE_DIR / "status.json", {"at": time.monotonic(), "ready": False, "phase": "stopped"}, mode=0o644)
    subprocess.run(["systemctl", "stop", UNIT], timeout=10, check=False)


if __name__ == "__main__":
  main()
