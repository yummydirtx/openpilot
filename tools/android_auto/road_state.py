"""Read-only, bounded road camera/model snapshots for a CPU projection renderer.

VisionIPC buffers are shared and reused by camerad. Copy only the latest packet,
verify its frame ID before and after the copy, then convert owned NV12 bytes.
No UI singleton, GPU context, camera server, or publisher is created here.
"""

from dataclasses import dataclass, field
import math
import time
from typing import Any


ROAD_SERVICES = ("deviceState", "narrowRoadCameraState", "wideRoadCameraState", "extrinsicsCalibration", "modelV2", "radarState",
                 "selfdriveState", "carState", "carParams", "longitudinalPlan", "driverMonitoringState", "driverStateV2")
MAX_NV12_BYTES = 16 * 1024 * 1024
MAX_POINTS = 128
MAX_SOURCE_AGE = 0.35
MAX_MODEL_CAMERA_SKEW = 0.15
VIEW_FROM_DEVICE = ((0.0, 1.0, 0.0), (0.0, 0.0, 1.0), (1.0, 0.0, 0.0))


@dataclass(frozen=True)
class CameraFrame:
  rgb: Any = field(repr=False, compare=False)
  frame_id: int
  timestamp_sof: float
  timestamp_eof: float
  width: int
  height: int
  stream: str


@dataclass(frozen=True)
class CameraGeometry:
  width: int
  height: int
  intrinsics: tuple[tuple[float, ...], ...]
  view_from_calib: tuple[tuple[float, ...], ...] = VIEW_FROM_DEVICE
  height_m: float = 0.0
  camera_offset_m: float = 0.0
  calibrated: bool = False


@dataclass(frozen=True)
class ModelSnapshot:
  position: tuple[tuple[float, float, float], ...]
  lane_lines: tuple[tuple[tuple[float, float, float], ...], ...]
  road_edges: tuple[tuple[tuple[float, float, float], ...], ...]
  lane_probs: tuple[float, ...]
  edge_stds: tuple[float, ...]
  acceleration_x: tuple[float, ...]
  frame_id: int
  frame_id_extra: int
  timestamp_eof: float


@dataclass(frozen=True)
class Lead:
  d_rel: float
  y_rel: float
  v_rel: float
  present: bool


@dataclass(frozen=True)
class DriverState:
  active: bool
  is_rhd: bool
  face_orientation: tuple[float, float, float]


@dataclass(frozen=True)
class RoadSnapshot:
  camera: CameraFrame | None = None
  geometry: CameraGeometry | None = None
  model: ModelSnapshot | None = None
  leads: tuple[Lead, ...] = ()
  driver_state: DriverState | None = None
  experimental_mode: bool = False
  allow_throttle: bool = False
  longitudinal_control: bool = False
  engageable: bool = False
  camera_age_seconds: float = math.inf
  model_age_seconds: float = math.inf
  radar_age_seconds: float = math.inf
  driver_age_seconds: float = math.inf
  captured_at: float = 0.0
  reasons: tuple[str, ...] = ()
  # Decision topics can affect the wheel icon and path color independently of
  # camera/model geometry. Infinity means the decision must use an unavailable
  # fallback, not extend a previously live presentation.
  selfdrive_age_seconds: float = math.inf
  longitudinal_plan_age_seconds: float = math.inf

  @property
  def camera_stale(self):
    return self.camera is None

  @property
  def model_stale(self):
    return self.model is None

  @property
  def display_age_seconds(self):
    ages = ([self.camera_age_seconds] if self.camera is not None else [])
    if self.model is not None:
      ages.append(self.model_age_seconds)
    if self.leads:
      ages.append(self.radar_age_seconds)
    if self.driver_state is not None:
      ages.append(self.driver_age_seconds)
    if math.isfinite(self.selfdrive_age_seconds):
      ages.append(self.selfdrive_age_seconds)
    if self.model is not None and self.longitudinal_control and not self.experimental_mode and math.isfinite(self.longitudinal_plan_age_seconds):
      ages.append(self.longitudinal_plan_age_seconds)
    return max(ages, default=0.0)

  def metadata(self, now=None):
    elapsed = max(0, (time.monotonic() if now is None else now) - self.captured_at)
    return {"camera_available": self.camera is not None, "model_available": self.model is not None,
            "camera_frame_id": self.camera.frame_id if self.camera else None, "model_frame_id": self.model.frame_id if self.model else None,
            "camera_stream": self.camera.stream if self.camera else None, "driver_available": self.driver_state is not None,
            "display_age_seconds": self.display_age_seconds + elapsed
            if self.camera is not None or self.driver_state is not None or math.isfinite(self.selfdrive_age_seconds) else 0.0,
            "selfdrive_age_seconds": self.selfdrive_age_seconds + elapsed if math.isfinite(self.selfdrive_age_seconds) else None,
            "longitudinal_plan_age_seconds": self.longitudinal_plan_age_seconds + elapsed if math.isfinite(self.longitudinal_plan_age_seconds) else None,
            "reasons": list(self.reasons)}


def finite_tuple(items, count=None, maximum=MAX_POINTS, bound=10000):
  if count is not None and len(items) != count or len(items) > maximum:
    raise ValueError("Unexpected road data dimensions")
  result = tuple(float(item) for item in items)
  if any(not math.isfinite(item) or abs(item) > bound for item in result):
    raise ValueError("Invalid road data values")
  return result


def matrix_product(a, b):
  return tuple(tuple(sum(a[i][k] * b[k][j] for k in range(3)) for j in range(3)) for i in range(3))


def rotation_from_euler(angles):
  """Rz(yaw) Ry(pitch) Rx(roll), matching native euler2rot_single."""
  roll, pitch, yaw = finite_tuple(angles, count=3, bound=math.pi)
  cr, sr, cp, sp, cy, sy = math.cos(roll), math.sin(roll), math.cos(pitch), math.sin(pitch), math.cos(yaw), math.sin(yaw)
  return ((cp * cy, sr * sp * cy - cr * sy, cr * sp * cy + sr * sy),
          (cp * sy, sr * sp * sy + cr * cy, cr * sp * sy - sr * cy), (-sp, sr * cp, cr * cp))


def model_points(points):
  x, y, z = (finite_tuple(getattr(points, axis)) for axis in ("x", "y", "z"))
  if not 2 <= len(x) <= MAX_POINTS or len(x) != len(y) or len(x) != len(z):
    raise ValueError("Invalid model polyline")
  return tuple(zip(x, y, z, strict=True))


def model_snapshot(model):
  if len(model.laneLines) != 4 or len(model.roadEdges) != 2:
    raise ValueError("Expected four lane lines and two road edges")
  return ModelSnapshot(model_points(model.position), tuple(model_points(line) for line in model.laneLines),
                       tuple(model_points(line) for line in model.roadEdges), finite_tuple(model.laneLineProbs, count=4),
                       finite_tuple(model.roadEdgeStds, count=2), finite_tuple(model.acceleration.x),
                       int(model.frameId), int(model.frameIdExtra), float(model.timestampEof) / 1e9)


def source_age(sm, name, now):
  if not sm.seen.get(name, False):
    return math.inf
  published, received = sm.logMonoTime[name] / 1e9, sm.recv_time[name]
  if not math.isfinite(published) or not math.isfinite(received) or published <= 0 or max(published, received) > now + 0.05:
    return math.inf
  return max(0, now - published, now - received)


def fresh(sm, name, now, deadline=MAX_SOURCE_AGE):
  return bool(sm.valid.get(name, False) and sm.alive.get(name, False)) and source_age(sm, name, now) <= deadline


def copy_nv12(buffer, frame_id):
  """Return owned contiguous NV12, rejecting shared buffers overwritten mid-copy."""
  import numpy as np
  width, height, stride, uv_offset = (int(getattr(buffer, key)) for key in ("width", "height", "stride", "uv_offset"))
  data = buffer.data
  if (not 0 < width <= 4096 or not 0 < height <= 2160 or width % 2 or height % 2 or stride < width
      or stride > 8192 or uv_offset < stride * height or len(data) > MAX_NV12_BYTES
      or uv_offset + stride * (height // 2) > len(data)):
    raise ValueError("Unsupported NV12 dimensions or padding")
  if int(buffer.frame_id) != frame_id:
    raise ValueError("VisionIPC buffer already overwritten")
  # Copy all producer bytes once, then strip row/plane padding from owned bytes.
  owned = bytes(data)
  if int(buffer.frame_id) != frame_id:
    raise ValueError("VisionIPC buffer overwritten during copy")
  # NumPy executes these strided plane copies in C. The output bytearray owns
  # its storage; neither it nor these temporary arrays reference camerad memory.
  packed = bytearray(width * height * 3 // 2)
  destination = np.frombuffer(packed, dtype=np.uint8).reshape(height * 3 // 2, width)
  destination[:height] = np.ndarray((height, width), dtype=np.uint8, buffer=owned, strides=(stride, 1))
  destination[height:] = np.ndarray((height // 2, width), dtype=np.uint8, buffer=owned, offset=uv_offset, strides=(stride, 1))
  return packed, width, height


def nv12_to_rgb(data, width, height):
  """Convert owned tightly packed NV12 without initializing a hardware decoder."""
  import av
  from PIL import Image
  if not 0 < width <= 4096 or not 0 < height <= 2160 or width % 2 or height % 2 or len(data) != width * height * 3 // 2:
    raise ValueError("Invalid packed NV12 frame")
  frame = av.VideoFrame(width, height, "nv12")
  cursor = 0
  source = memoryview(data)
  for plane, rows in zip(frame.planes, (height, height // 2), strict=True):
    end = cursor + width * rows
    if plane.line_size == width and plane.buffer_size == width * rows:
      plane.update(source[cursor:end])
    else:
      # Narrow/non-aligned test frames can have padding in PyAV's destination.
      import numpy as np
      packed = bytearray(plane.buffer_size)
      destination = np.ndarray((rows, plane.line_size), dtype=np.uint8, buffer=packed)
      destination[:, :width] = np.frombuffer(source[cursor:end], dtype=np.uint8).reshape(rows, width)
      plane.update(packed)
    cursor = end
  converted = frame.reformat(format="rgb24")
  rgb = converted.planes[0]
  # to_image() copies through an intermediate RGB ndarray. Pillow can directly
  # copy the already-converted plane while respecting FFmpeg's row padding.
  return Image.frombytes("RGB", (width, height), rgb, "raw", "RGB", rgb.line_size, 1)


class RoadStateReader:
  """Own one conflated camera client and one subscriber-only telemetry bundle.

  Native VisionIPC connect(False) avoids its retry loop but its FD exchange is
  still native IPC; the outer FrameWorker's killable-process watchdog bounds it.
  Camera acquisition uses recv(0). Cache at most one owned RGB image.
  """

  def __init__(self, *, sm=None, params=None, client_factory=None, camera_configs=None, converter=nv12_to_rgb, sunnypilot=True):
    if sm is None:
      from openpilot.cereal import messaging
      sm = messaging.SubMaster(list(ROAD_SERVICES))
    if params is None:
      from openpilot.common.params import Params
      params = Params()
    if camera_configs is None:
      from openpilot.common.transformations.camera import DEVICE_CAMERAS
      camera_configs = DEVICE_CAMERAS
    if client_factory is None:
      from msgq.visionipc import VisionIpcClient
      from openpilot.cereal.visionipc import VisionStreamType
      def client_factory(stream):
        enum = VisionStreamType.VISION_STREAM_NARROW_ROAD if stream == "narrow" else VisionStreamType.VISION_STREAM_WIDE_ROAD
        return VisionIpcClient("camerad", enum, True)
    self.sm, self.params = sm, params
    self.sunnypilot = sunnypilot
    self.camera_configs, self.client_factory, self.converter = camera_configs, client_factory, converter
    self.client = None
    self.stream = "narrow"
    self.camera = None
    self.last_connect = -math.inf
    self.camera_offset = 0.0
    self.longitudinal_control = False
    self.last_params = -math.inf
    self.settings_error = None

  def close(self):
    # VisionIpcClient has no Python close method; dropping it releases its FDs.
    self.client = None
    self.camera = None

  def _settings(self, now):
    if now - self.last_params < 5:
      return
    self.last_params = now
    try:
      self.camera_offset = float(self.params.get("CameraOffset", return_default=True) or 0) if self.sunnypilot else 0.0
      if not math.isfinite(self.camera_offset) or abs(self.camera_offset) > 2:
        raise ValueError("Invalid CameraOffset parameter")
      self.settings_error = None
    except (ValueError, TypeError, KeyError, OSError):
      self.camera_offset = 0.0
      self.settings_error = "camera offset unavailable"
    if self.sm.seen["carParams"] and self.sm.valid["carParams"]:
      self.longitudinal_control = bool(self.sm["carParams"].openpilotLongitudinalControl)
    elif not self.longitudinal_control:
      # carParams publishes only every 50 seconds; use the same current-drive
      # Params value read by native ModelRenderer while awaiting its topic.
      try:
        raw = self.params.get("CarParams")
        if raw:
          from openpilot.cereal import messaging
          from opendbc.car.structs import car
          self.longitudinal_control = bool(messaging.log_from_bytes(raw, car.CarParams).openpilotLongitudinalControl)
      except (OSError, ValueError, TypeError, RuntimeError):
        self.longitudinal_control = False

  def _camera(self, now, reasons):
    ss_fresh, cs_fresh = fresh(self.sm, "selfdriveState", now), fresh(self.sm, "carState", now)
    target = self.stream
    if ss_fresh and self.sm["selfdriveState"].experimentalMode and fresh(self.sm, "wideRoadCameraState", now):
      if cs_fresh and self.sm["carState"].vEgo < 10:
        target = "wide"
      elif cs_fresh and self.sm["carState"].vEgo > 15:
        target = "narrow"
    else:
      target = "narrow"
    if target != self.stream:
      self.close()
      self.stream, self.last_connect = target, -math.inf
    service = "narrowRoadCameraState" if self.stream == "narrow" else "wideRoadCameraState"
    if not fresh(self.sm, service, now):
      self.camera = None
      reasons.append("cameraState unavailable")
      return None
    if self.client is None or not self.client.is_connected():
      self.camera = None
      if now - self.last_connect < 0.5:
        reasons.append("camera reconnect pending")
        return None
      self.last_connect = now
      self.client = self.client_factory(self.stream)
      if not self.client.connect(False):
        reasons.append("camera disconnected")
        return None
    buffer = self.client.recv(0)
    now = time.monotonic()
    if buffer is not None:
      # VIPC.valid is unused and always false in this camerad producer. The
      # separate CameraState event above supplies the actual validity check.
      frame_id, sof, eof = int(self.client.frame_id), self.client.timestamp_sof / 1e9, self.client.timestamp_eof / 1e9
      if not math.isfinite(eof) or not math.isfinite(sof) or not 0 < sof <= eof <= now + 0.05 or now - eof > MAX_SOURCE_AGE:
        self.camera = None
        reasons.append("camera timestamp invalid")
        return None
      if self.camera is None or self.camera.frame_id != frame_id:
        try:
          data, width, height = copy_nv12(buffer, frame_id)
          rgb = self.converter(data, width, height)
          self.camera = CameraFrame(rgb, frame_id, sof, eof, width, height, self.stream)
        except ValueError:
          self.camera = None
          reasons.append("camera buffer invalid")
          return None
    if self.camera is None or now - self.camera.timestamp_eof > MAX_SOURCE_AGE:
      self.camera = None
      reasons.append("camera frame unavailable")
      return None
    cs = self.sm[service]
    if abs(int(cs.frameId) - self.camera.frame_id) > 3 or abs(cs.timestampEof / 1e9 - self.camera.timestamp_eof) > MAX_MODEL_CAMERA_SKEW:
      self.camera = None
      reasons.append("camera metadata mismatch")
    return self.camera

  def _geometry(self, camera, now, reasons):
    if camera is None or not fresh(self.sm, "deviceState", now, 1.5):
      return None
    service = "narrowRoadCameraState" if camera.stream == "narrow" else "wideRoadCameraState"
    key = str(self.sm["deviceState"].deviceType), str(self.sm[service].sensor)
    config = self.camera_configs.get(key)
    if config is None:
      reasons.append("unknown camera intrinsics")
      return None
    selected = config.narrow_road if camera.stream == "narrow" else config.wide_road
    if (selected.width, selected.height) != (camera.width, camera.height):
      reasons.append("camera dimensions mismatch")
      return None
    intrinsics = tuple(tuple(float(item) for item in row) for row in selected.intrinsics)
    result = CameraGeometry(camera.width, camera.height, intrinsics, camera_offset_m=self.camera_offset)
    if self.settings_error is not None:
      reasons.append(self.settings_error)
      return result
    if not fresh(self.sm, "extrinsicsCalibration", now, 2.5) or str(self.sm["extrinsicsCalibration"].calStatus) != "calibrated":
      reasons.append("calibration unavailable")
      return result
    calibration = self.sm["extrinsicsCalibration"]
    rotation = rotation_from_euler(calibration.rpyCalib)
    if camera.stream == "wide":
      rotation = matrix_product(rotation_from_euler(calibration.wideFromDeviceEuler), rotation)
    view = matrix_product(VIEW_FROM_DEVICE, rotation)
    height = finite_tuple(calibration.height, count=1)[0]
    if not 0.5 <= height <= 3:
      raise ValueError("Invalid calibrated camera height")
    return CameraGeometry(camera.width, camera.height, intrinsics, tuple(tuple(float(item) for item in row) for row in view),
                          height, self.camera_offset, True)

  def poll(self):
    try:
      self.sm.update(0)
    except OSError:
      return RoadSnapshot(captured_at=time.monotonic(), reasons=("road telemetry unavailable",))
    now = time.monotonic()
    reasons = []
    self._settings(now)
    if not fresh(self.sm, "deviceState", now, 1.5) or not self.sm["deviceState"].started:
      self.camera = None
      return RoadSnapshot(captured_at=now, reasons=("offroad or deviceState unavailable",))
    try:
      camera = self._camera(now, reasons)
    except (OSError, ValueError, RuntimeError):
      self.close()
      camera = None
      reasons.append("camera transport unavailable")
    now = time.monotonic()  # Include conversion work in every acquisition-age gate.
    if camera is not None and now - camera.timestamp_eof > MAX_SOURCE_AGE:
      camera = None
      reasons.append("camera expired during conversion")
    geometry = None
    try:
      geometry = self._geometry(camera, now, reasons)
    except ValueError:
      reasons.append("calibration invalid")
    model = None
    model_age = math.inf
    if camera is not None and geometry is not None and geometry.calibrated and fresh(self.sm, "modelV2", now):
      raw = self.sm["modelV2"]
      model_age = max(source_age(self.sm, "modelV2", now), now - raw.timestampEof / 1e9)
      selected_id = raw.frameIdExtra if camera.stream == "wide" else raw.frameId
      if (0 < raw.timestampEof / 1e9 <= now + 0.05 and model_age <= MAX_SOURCE_AGE
          and abs(camera.timestamp_eof - raw.timestampEof / 1e9) <= MAX_MODEL_CAMERA_SKEW and abs(camera.frame_id - selected_id) <= 3):
        try:
          model = model_snapshot(raw)
        except ValueError:
          reasons.append("model geometry invalid")
      else:
        reasons.append("model/camera not synchronized")
    if model is None:
      reasons.append("model unavailable")
    leads = ()
    radar_age = math.inf
    if model is not None and self.longitudinal_control and fresh(self.sm, "radarState", now):
      radar_age = source_age(self.sm, "radarState", now)
      raw = self.sm["radarState"]
      candidates = []
      for lead in (raw.leadOne, raw.leadTwo):
        if lead.present:
          try:
            d, y, v = finite_tuple((lead.dRel, lead.yRel, lead.vRel), count=3, bound=500)
            if d > 0:
              candidates.append(Lead(d, y, v, True))
          except ValueError:
            reasons.append("radar lead invalid")
      leads = tuple(candidates)
    driver = None
    driver_age = math.inf
    if fresh(self.sm, "driverMonitoringState", now) and fresh(self.sm, "driverStateV2", now):
      dm, ds = self.sm["driverMonitoringState"], self.sm["driverStateV2"]
      data = ds.rightDriverData if dm.isRHD else ds.leftDriverData
      try:
        driver = DriverState(str(dm.activePolicy) == "vision", bool(dm.isRHD), finite_tuple(data.faceOrientation, count=3, bound=math.pi))
        driver_age = max(source_age(self.sm, service, now) for service in ("driverMonitoringState", "driverStateV2"))
      except ValueError:
        reasons.append("driver pose invalid")
    ss_ok = fresh(self.sm, "selfdriveState", now)
    plan_ok = fresh(self.sm, "longitudinalPlan", now)
    throttle = not self.longitudinal_control or (plan_ok and self.sm["longitudinalPlan"].allowThrottle)
    return RoadSnapshot(camera, geometry, model, leads, driver, bool(ss_ok and self.sm["selfdriveState"].experimentalMode),
                        bool(throttle), self.longitudinal_control, bool(ss_ok and self.sm["selfdriveState"].engageable),
                        max(0, now - camera.timestamp_eof) if camera else math.inf, model_age, radar_age, driver_age,
                        now, tuple(reasons), selfdrive_age_seconds=source_age(self.sm, "selfdriveState", now) if ss_ok else math.inf,
                        longitudinal_plan_age_seconds=source_age(self.sm, "longitudinalPlan", now) if plan_ok else math.inf)
