"""Fail-open display selection shared by the native UI and projection supervisor.

Only display selection crosses this mailbox. No Params or vehicle command
publishing. Runtime state is volatile and expires using the device's monotonic
clock. The privileged supervisor owns status; the UI owns requests.
"""

import json
import math
import os
import stat
from pathlib import Path
import time
import uuid

STATE_DIR = Path("/run/automaxxing-display")
REQUEST_DIR = Path("/run/automaxxing-display-request")
LEASE_SECONDS = 0.5


def read_json(path):
  try:
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    with os.fdopen(fd) as f:
      if not stat.S_ISREG(os.fstat(f.fileno()).st_mode):
        return {}
      raw = f.read(4097)
    if len(raw) > 4096:
      return {}
    value = json.loads(raw)
    return value if isinstance(value, dict) else {}
  except (OSError, ValueError):
    return {}


def write_json(path, value, *, mode=0o600):
  path = Path(path)
  temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
  # Unlink first: never follow a pre-existing temporary symlink as root.
  temporary.unlink(missing_ok=True)
  fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
  try:
    with os.fdopen(fd, "w") as f:
      json.dump(value, f, allow_nan=False)
    temporary.replace(path)
  finally:
    temporary.unlink(missing_ok=True)


def fresh(value, now, ttl=LEASE_SECONDS):
  stamp = value.get("at")
  return type(stamp) in (int, float) and math.isfinite(stamp) and 0 <= now - stamp < ttl


class HandoffClient:
  def __init__(self, state_dir=STATE_DIR, request_dir=REQUEST_DIR, starter=None):
    self.state_dir, self.request_dir = Path(state_dir), Path(request_dir)
    self.token = uuid.uuid4().hex
    self.mode = "local"
    self.suppressed = False
    self.available = False
    self.phase = "local"
    self._last_write = -1.0
    self._gesture_slots = set()
    self.starter = starter

  @property
  def enabled(self):
    return self.available or self.starter is not None

  @property
  def label(self):
    return "Cancel Android Auto" if self.mode == "project" else "Use Mazda display"

  def select(self, mode):
    if mode not in ("local", "project"):
      raise ValueError("Invalid display mode")
    self.mode = mode
    self.token = uuid.uuid4().hex
    self._last_write = -1.0
    if mode == "local":
      self.suppressed = False
    elif not self.available and self.starter is not None:
      try:
        self.starter()
      except OSError:
        self.mode = "local"

  def tick(self, events=(), *, critical=False, now=None):
    now = time.monotonic() if now is None else now
    status = read_json(self.state_dir / "status.json")
    was_available = self.available
    self.available = fresh(status, now) and status.get("phase") in ("local", "connecting", "projecting", "failed")
    self.phase = status.get("phase", "local") if self.available else "unavailable"
    was_suppressed = self.suppressed
    if critical and self.mode == "project":
      self.select("local")
    if self.phase == "failed" and self.mode == "project":
      self.select("local")
    if self.available and status.get("token") == self.token and status.get("local_requested") is True:
      self.select("local")
    # A first touch is a display switch, never a click through to a native widget.
    waking = was_suppressed and any(e.left_down or e.left_pressed for e in events)
    if waking:
      self.select("local")
    filtered = []
    for event in events:
      if waking or self._gesture_slots:
        if event.left_down or event.left_pressed:
          self._gesture_slots.add(event.slot)
        if event.left_released:
          self._gesture_slots.discard(event.slot)
      else:
        filtered.append(event)
    until = status.get("ready_until", 0)
    self.suppressed = (self.available and not critical and self.mode == "project"
                       and status.get("token") == self.token and status.get("ready") is True
                       and type(until) in (int, float) and now < until <= now + LEASE_SECONDS)
    if (was_suppressed or was_available) and not self.available:
      self.select("local")
    if self.available and now - self._last_write >= 0.1:
      try:
        write_json(self.request_dir / "request.json", {"at": now, "token": self.token, "mode": self.mode})
        self._last_write = now
      except OSError:
        self.suppressed = False
        self.available = False
    return filtered
