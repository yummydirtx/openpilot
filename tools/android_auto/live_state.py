"""Read-only, immutable display snapshots of the running openpilot instance.

Only ``LiveStateReader`` imports Cereal/Params, and only subscriber sockets and
parameter reads are used. Importing this module never starts the native UI or
any control process. Call ``poll`` from the projection worker's own thread.

The pure adapter deliberately checks both receipt and publisher timestamps.
High-rate data becomes unavailable within 500 ms (or sooner if SubMaster marks
it dead). ``stale_after`` can reserve part of that budget for rendering and
transport, but cannot extend it. deviceState and onroadEvents need longer bounds because they publish
at 2 Hz and 1 Hz. No stale message can retain an engaged presentation.
"""

from dataclasses import dataclass, field
import math
import time


STALE_SECONDS = 0.5
SERVICE_DEADLINES = {
  "carState": STALE_SECONDS,
  "selfdriveState": STALE_SECONDS,
  "selfdriveStateSP": STALE_SECONDS,
  "controlsState": STALE_SECONDS,
  "pandaStates": STALE_SECONDS,
  "deviceState": 1.5,
  "onroadEvents": 2.5,
}
BASE_SERVICES = ("carState", "selfdriveState", "controlsState", "pandaStates", "deviceState")
SUNNYPILOT_SERVICES = ("selfdriveStateSP", "onroadEvents")
MS_TO_KPH = 3.6
MS_TO_MPH = 3.6 / 1.609344
KM_TO_MILE = 0.621371  # Match native HudRenderer's set-speed conversion.


def enum_name(value):
  """Cereal enum readers stringify to schema names; tests can use strings."""
  return str(value)


@dataclass(frozen=True)
class DisplayAlert:
  text1: str
  text2: str
  size: int
  status: int


@dataclass(frozen=True)
class LiveDisplayState:
  speed: float | None = None
  set_speed: float | None = None
  status: str = "disengaged"
  is_metric: bool = False
  started: bool = False
  age_seconds: float = math.inf
  missing_services: tuple[str, ...] = ()
  alert: DisplayAlert | None = None
  hide_speed: bool = False
  is_cruise_available: bool = True
  source: str = field(default="live", init=False)
  # Alerts may remain visible when an unrelated HUD source is unavailable.
  # Keep their selfdriveState freshness separate from the aggregate HUD age.
  alert_age_seconds: float = math.inf
  steering_angle: float = 0.0

  @property
  def stale(self):
    return not self.started or bool(self.missing_services) or self.age_seconds > STALE_SECONDS or self.speed is None

  @property
  def display_status(self):
    return "disengaged" if self.stale else self.status

  @property
  def speed_text(self):
    return "–" if self.stale else str(round(max(0, self.speed)))

  @property
  def unit_text(self):
    return "km/h" if self.is_metric else "mph"

  @property
  def is_cruise_set(self):
    return not self.stale and self.set_speed is not None

  @property
  def alert_text_1(self):
    return self.alert.text1 if self.alert is not None else ""

  @property
  def alert_text_2(self):
    return self.alert.text2 if self.alert is not None else ""

  @property
  def alert_status(self):
    return self.alert.status if self.alert is not None else 0


def display_status(ss, ss_sp=None, events=()):
  """Same branch order as UIStateSP.update_status, without importing UIState.

  In particular MADS lateral-only is not full engagement, and a lateral
  override with longitudinal still active is distinct from a gas override.
  """
  state = enum_name(ss.state)
  if state == "preEnabled":
    return "override"
  if ss_sp is None:
    return "override" if state == "overriding" else "engaged" if ss.enabled else "disengaged"
  mads = ss_sp.mads
  if state == "overriding" and (not mads.available or any(event.overrideLongitudinal for event in events)):
    return "override"
  if enum_name(mads.state) in ("paused", "overriding"):
    return "override"
  if not mads.available:
    return "engaged" if ss.enabled else "disengaged"
  if mads.enabled and ss.enabled:
    return "engaged"
  if mads.enabled:
    return "lat_only"
  return "long_only" if ss.enabled else "disengaged"


def finite_number(value):
  value = float(value)
  if not math.isfinite(value):
    raise ValueError("Non-finite display value")
  return value


def alert_from_state(ss):
  # Enum names avoid importing capnp, and work across schema integer wrappers.
  sizes = {"none": 0, "small": 1, "mid": 2, "full": 3}
  statuses = {"normal": 0, "userPrompt": 1, "critical": 2}
  size = sizes.get(enum_name(ss.alertSize), getattr(ss.alertSize, "raw", ss.alertSize))
  status = statuses.get(enum_name(ss.alertStatus), getattr(ss.alertStatus, "raw", ss.alertStatus))
  if size == 0:
    return None
  if size not in (1, 2, 3) or status not in (0, 1, 2):
    raise ValueError("Unknown alert presentation")
  return DisplayAlert(str(ss.alertText1), str(ss.alertText2), int(size), int(status))


class LiveStateAdapter:
  """Pure adapter accepting the SubMaster interface; no socket or GUI ownership."""

  def __init__(self, *, sunnypilot=True, stale_after=STALE_SECONDS):
    if not math.isfinite(stale_after) or not 0 < stale_after <= STALE_SECONDS:
      raise ValueError("stale_after must be positive and no more than 0.5 seconds")
    self.sunnypilot = sunnypilot
    self.stale_after = stale_after
    self.deadlines = {name: min(deadline, stale_after) if deadline <= STALE_SECONDS else deadline
                      for name, deadline in SERVICE_DEADLINES.items()}
    self.cluster_seen = False
    self.ignition = False
    self.last_can_ignition = None
    self.started_previous = False
    self.started_at = None

  @staticmethod
  def age(sm, service, now):
    if not sm.seen.get(service, False):
      return math.inf
    received = float(sm.recv_time[service])
    published = float(sm.logMonoTime[service]) / 1e9
    if not math.isfinite(received) or not math.isfinite(published) or published <= 0 or max(received, published) > now + 0.05:
      return math.inf
    return max(0.0, now - received, now - published)

  def fresh(self, sm, service, now):
    return (bool(sm.alive.get(service, False)) and bool(sm.valid.get(service, False))
            and self.age(sm, service, now) <= self.deadlines[service])

  def _update_ignition(self, pandas, now):
    # Same bounded CAN-ignition hold as sunnypilot.common.ignition, with state
    # owned by this reader instead of affecting the UI's module-level state.
    valid = [panda for panda in pandas if enum_name(panda.pandaType) != "unknown"]
    if not valid:
      self.last_can_ignition = None
      self.ignition = False
    elif any(panda.ignitionCan for panda in valid):
      self.last_can_ignition = now
      self.ignition = True
    elif not any(panda.ignitionLine for panda in valid):
      self.ignition = False
    else:
      self.ignition = self.last_can_ignition is None or now - self.last_can_ignition < 5.0

  def snapshot(self, sm, *, now=None, is_metric=False, true_speed=False, hide_speed=False):
    now = time.monotonic() if now is None else now
    services = ["carState", "selfdriveState", "pandaStates", "deviceState"]
    if self.sunnypilot:
      services += list(SUNNYPILOT_SERVICES)
    fresh = {name: self.fresh(sm, name, now) for name in services}
    missing = [name for name in services if not fresh[name]]
    if fresh["pandaStates"] and sm.updated["pandaStates"]:
      self._update_ignition(sm["pandaStates"], now)
    started = bool(fresh["deviceState"] and fresh["pandaStates"] and sm["deviceState"].started and self.ignition)
    if started and not self.started_previous:
      self.started_at = now
      self.cluster_seen = False
    self.started_previous = started

    # Drop queued data from the prior drive during an offroad->onroad transition.
    if started:
      for name in ("carState", "selfdriveState") + (("selfdriveStateSP",) if self.sunnypilot else ()):
        if fresh[name] and sm.recv_time[name] < self.started_at:
          fresh[name] = False
          missing.append(name)
    high_rate = [name for name in services if SERVICE_DEADLINES[name] == STALE_SECONDS]
    age = max(self.age(sm, name, now) for name in high_rate)
    alert = alert_from_state(sm["selfdriveState"]) if started and fresh["selfdriveState"] else None
    alert_age = self.age(sm, "selfdriveState", now) if alert is not None else math.inf
    speed = set_speed = None
    status = "disengaged"
    cruise_available = True
    steering_angle = 0.0
    if started and not missing:
      cs = sm["carState"]
      steering_angle = finite_number(getattr(cs, "steeringAngleDeg", 0.0))
      cluster = finite_number(cs.vEgoCluster)
      self.cluster_seen = self.cluster_seen or cluster != 0.0
      velocity = cluster if self.cluster_seen and not true_speed else finite_number(cs.vEgo)
      speed = max(0.0, velocity * (MS_TO_KPH if is_metric else MS_TO_MPH))
      cruise = finite_number(cs.vCruiseCluster)
      if cruise == 0.0:
        if self.fresh(sm, "controlsState", now) and sm.recv_time["controlsState"] >= self.started_at:
          cruise = finite_number(sm["controlsState"].deprecated.vCruise)
          age = max(age, self.age(sm, "controlsState", now))
        else:
          missing.append("controlsState")
      cruise_available = cruise != -1
      set_speed = cruise * (1 if is_metric else KM_TO_MILE) if 0 < cruise < 255 else None
      status = display_status(sm["selfdriveState"], sm["selfdriveStateSP"] if self.sunnypilot else None,
                              sm["onroadEvents"] if self.sunnypilot else ())
    if missing or not started:
      speed = set_speed = None
      status = "disengaged"
    return LiveDisplayState(speed=speed, set_speed=set_speed, status=status, is_metric=is_metric, started=started,
                            age_seconds=age, missing_services=tuple(missing), alert=alert, hide_speed=hide_speed,
                            is_cruise_available=cruise_available, alert_age_seconds=alert_age, steering_angle=steering_angle)


class LiveStateReader:
  """Lazily open only subscriber sockets and read-only parameter accessors."""

  def __init__(self, *, sunnypilot=True, stale_after=STALE_SECONDS, sm=None, params=None):
    self.adapter = LiveStateAdapter(sunnypilot=sunnypilot, stale_after=stale_after)
    if sm is None:
      from openpilot.cereal import messaging
      services = list(BASE_SERVICES + (SUNNYPILOT_SERVICES if sunnypilot else ()))
      sm = messaging.SubMaster(services)
    if params is None:
      from openpilot.common.params import Params
      params = Params()
    self.sm = sm
    self.params = params
    self.params_read_at = -math.inf
    self.settings = {}

  def poll(self):
    self.sm.update(0)
    now = time.monotonic()
    if now - self.params_read_at >= 1.0:
      self.settings = {"is_metric": self.params.get_bool("IsMetric"),
                       "true_speed": self.params.get_bool("TrueVEgoUI") if self.adapter.sunnypilot else False,
                       "hide_speed": self.params.get_bool("HideVEgoUI") if self.adapter.sunnypilot else False}
      self.params_read_at = now
    return self.adapter.snapshot(self.sm, now=now, **self.settings)
