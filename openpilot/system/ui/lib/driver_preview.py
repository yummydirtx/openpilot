"""Lease the offroad driver preview to the projected UI.

The physical UI owns the camera-enable parameter and the preview sound publisher,
just as it does for its own preview. A stopped renderer cannot leave either alive.
"""

import math
import time

from openpilot.system.ui.lib.display_handoff import STATE_DIR, REQUEST_DIR, fresh, read_json, write_json


def service_fresh(sm, name, now, ttl):
  return (sm.alive[name] and sm.valid[name] and 0 <= now - sm.recv_time[name] < ttl
          and 0 <= now - sm.logMonoTime[name] / 1e9 < ttl)


def preview_offroad(sm, now):
  return service_fresh(sm, "deviceState", now, 1.5) and not sm["deviceState"].started


def preview_allowed(sm, now=None):
  now = time.monotonic() if now is None else now
  if not service_fresh(sm, "deviceState", now, 1.5):
    return False
  if not sm["deviceState"].started:
    return True
  if not all(service_fresh(sm, name, now, .5) for name in ("carState", "selfdriveState", "carControl")):
    return False
  cs, controls = sm["carState"], sm["carControl"]
  return (cs.canValid and math.isfinite(cs.vEgo) and abs(cs.vEgo) < .01 and str(cs.gearShifter) == "park"
          and not sm["selfdriveState"].enabled and not controls.latActive and not controls.longActive)


class DriverPreviewRequest:
  def __init__(self, state_dir=STATE_DIR, request_dir=REQUEST_DIR):
    self.state_dir, self.request_dir = state_dir, request_dir
    self.token = None

  def update(self, active, reset):
    intent = read_json(self.state_dir / "intent.json")
    now = time.monotonic()
    if active:
      if not fresh(intent, now) or intent.get("mode") != "project":
        return
      self.token = intent.get("token")
    if self.token is not None and self.request_dir.is_dir():
      write_json(self.request_dir / "driver-preview.json",
                 {"at": now, "token": self.token, "active": active, "reset": reset})


class DriverPreviewHost:
  def __init__(self, params, publisher_factory):
    self.params = params
    self.publisher_factory = publisher_factory
    self.publisher = None
    self.owner = None
    self.reset_id = None

  def update(self, request, *, token, projecting, offroad, now, dm_state=None):
    valid = (offroad and projecting and token is not None and request.get("token") == token
             and request.get("active") is True and fresh(request, now))
    if not valid or self.owner is not None and self.owner != token:
      self.close(silence=offroad)
    if not valid:
      return
    if self.owner is None:
      # A preview already opened on the physical UI retains ownership.
      if self.params.get_bool("IsDriverViewEnabled"):
        return
      try:
        self.publisher = self.publisher_factory()
      except Exception:
        # A preview failure must not crash the physical UI (e.g. another
        # selfdriveState publisher took ownership during an ignition change).
        return
      self.owner = token
      try:
        self.params.put_bool("IsDriverViewEnabled", True, block=True)
        self.params.remove("DriverTooDistracted")
      except BaseException:
        self.close()
        raise
    reset_id = request.get("reset")
    if isinstance(reset_id, str) and reset_id != self.reset_id:
      self.params.remove("DriverTooDistracted")
      self.reset_id = reset_id
    try:
      self.publisher(dm_state)
    except Exception:
      self.close(silence=False)

  def close(self, *, silence=True):
    if self.owner is None:
      return
    publisher, self.publisher = self.publisher, None
    self.owner, self.reset_id = None, None
    try:
      if publisher is not None and silence:
        publisher(None)
    finally:
      self.params.put_bool("IsDriverViewEnabled", False, block=True)


def preview_sound_publisher():
  from openpilot.cereal import log, messaging
  pm = messaging.PubMaster(['selfdriveState'])
  audible = log.SelfdriveState.AudibleAlert
  sounds = {'one': audible.preAlert, 'two': audible.promptDistracted, 'three': audible.warningImmediate}

  def publish(dm_state):
    message = messaging.new_message('selfdriveState')
    message.selfdriveState.alertSound = sounds.get(str(dm_state.alertLevel), audible.none) if dm_state is not None else audible.none
    pm.send('selfdriveState', message)

  return publish
