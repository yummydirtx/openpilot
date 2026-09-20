"""Projection side of the optional native-display handoff."""

import time
import uuid

from openpilot.system.ui.lib.display_handoff import STATE_DIR, fresh, read_json, write_json


class DisplayControl:
  def __init__(self, state_dir=STATE_DIR):
    self.root = state_dir
    self.token = None
    self.mode = None
    self.last_write = -1.0
    self.ack_baseline = 0

  def sync(self, session):
    intent = read_json(self.root / "intent.json")
    now = time.monotonic()
    if not fresh(intent, now) or intent.get("mode") not in ("local", "project"):
      raise RuntimeError("Display supervisor heartbeat expired")
    if self.token != intent.get("token") or self.mode != intent["mode"]:
      first = self.token is None
      self.token = intent["token"]
      self.mode = intent["mode"]
      self.ack_baseline = session.acked
      if intent["mode"] == "local":
        session.request_native(resume_from_head_unit=False)
      elif not first or not session.focused:
        session.request_projection()

  def publish(self, session, *, valid_until, stale):
    now = time.monotonic()
    if now - self.last_write < 0.1:
      return
    ready = (session.focused and not stale and session.acked > self.ack_baseline and session.epoch_acked > 0
             and now - session.last_ack_at < 0.5 and valid_until is not None and now < valid_until)
    until = min(now + 0.3, valid_until or now, session.last_ack_at + 0.5)
    write_json(self.root / "projection.json", {"at": now, "token": self.token, "ready": ready, "ready_until": until,
                                             "local_requested": not session.allow_projection and not session.resume_from_head_unit,
                                             "phase": "projecting" if ready else "connecting" if session.focused else "local"})
    self.last_write = now

  def clear(self):
    write_json(self.root / "projection.json", {"at": time.monotonic(), "token": self.token, "ready": False, "phase": "local"})

  def bookmark(self):
    write_json(self.root / "action.json", {"at": time.monotonic(), "token": self.token, "action": "bookmark", "id": uuid.uuid4().hex}, mode=0o644)
