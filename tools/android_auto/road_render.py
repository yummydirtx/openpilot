"""Passive CPU road composition using the native 3X camera/model geometry.

The camera transform, ribbon projection, path distance, and lead chevrons below
follow selfdrive/ui/onroad/{augmented_road_view,model_renderer}.py. They own no
UIState, sockets, Params, window, or controls. Camera and model use the same
calibrated transform, in the native 1080-high logical coordinate system.
"""

import colorsys
from functools import lru_cache
import time

import numpy as np
from PIL import Image, ImageChops, ImageDraw, ImageOps

from tools.android_auto.road_state import MAX_SOURCE_AGE

VIEW_FROM_DEVICE = np.array([[0., 1., 0.], [0., 0., 1.], [1., 0., 0.]])
GAMMA_LUT = tuple(round((value / 255) ** (1 / 1.28) * 255) for value in range(256)) * 3
THROTTLE_COLORS = ((13, 248, 122, 102), (114, 255, 92, 89), (114, 255, 92, 0))
NO_THROTTLE_COLORS = ((242, 242, 242, 102), (242, 242, 242, 89), (242, 242, 242, 0))
# Native DriverStateRenderer face landmarks, in its logical icon coordinates.
FACE_KEYPOINTS = np.array([
  [-5.98, -51.20, 8], [-17.64, -49.14, 8], [-23.81, -46.40, 8], [-29.98, -40.91, 8],
  [-32.04, -37.49, 8], [-34.10, -32.00, 8], [-36.16, -21.03, 8], [-36.16, 6.40, 8],
  [-35.47, 10.51, 8], [-32.73, 19.43, 8], [-29.30, 26.29, 8], [-24.50, 33.83, 8], [-19.01, 41.37, 8],
  [-14.21, 46.17, 8], [-12.16, 47.54, 8], [-4.61, 49.60, 8], [4.99, 49.60, 8],
  [12.53, 47.54, 8], [14.59, 46.17, 8], [19.39, 41.37, 8], [24.87, 33.83, 8],
  [29.67, 26.29, 8], [33.10, 19.43, 8], [35.84, 10.51, 8], [36.53, 6.40, 8],
  [36.53, -21.03, 8], [34.47, -32.00, 8], [32.42, -37.49, 8], [30.36, -40.91, 8],
  [24.19, -46.40, 8], [18.02, -49.14, 8], [6.36, -51.20, 8], [-5.98, -51.20, 8],
], dtype=np.float32)


def native_transform(geometry, rect, *, wide=False):
  """Return camera-pixel→logical-display and calibrated-model→display matrices."""
  x, y, w, h = rect
  intrinsic = np.asarray(geometry.intrinsics, dtype=np.float64)
  calibration = np.asarray(geometry.view_from_calib, dtype=np.float64)
  if intrinsic.shape != (3, 3) or calibration.shape != (3, 3) or not np.isfinite([intrinsic, calibration]).all():
    raise ValueError("Invalid camera calibration matrices")
  cx, cy = intrinsic[0, 2], intrinsic[1, 2]
  if cx <= 0 or cy <= 0 or intrinsic[0, 0] <= 0 or intrinsic[1, 1] <= 0:
    raise ValueError("Invalid camera intrinsics")
  if not np.allclose((2 * cx, 2 * cy), (geometry.width, geometry.height), atol=1e-3):
    raise ValueError("Camera dimensions and optical center disagree")
  zoom = max(2.0 if wide else 1.1, w / (2 * cx), h / (2 * cy))
  calibrated = intrinsic @ calibration
  infinity = calibrated @ np.array([1000., 0., 0.])
  maximum = np.maximum(0, [cx * zoom - w / 2 - 5, cy * zoom - h / 2 - 5])
  offset = np.clip((infinity[:2] / infinity[2] - [cx, cy]) * zoom, -maximum, maximum) if abs(infinity[2]) > 1e-6 else [0, 0]
  video = np.array([[zoom, 0, w / 2 + x - offset[0] - cx * zoom],
                    [0, zoom, h / 2 + y - offset[1] - cy * zoom], [0, 0, 1.]])
  return video, video @ calibrated


def path_index(points, distance):
  positions = np.flatnonzero(np.asarray(points)[:, 0] <= distance)
  return int(positions[-1]) if len(positions) else 0


def clip_polygon(points, box):
  """Clip solid overlays to the exact camera content rectangle (native scissor)."""
  result = list(points)
  for axis, bound, minimum in ((0, box[0], True), (0, box[2] - 1, False), (1, box[1], True), (1, box[3] - 1, False)):
    previous = result[-1] if result else None
    clipped = []
    for point in result:
      inside = point[axis] >= bound if minimum else point[axis] <= bound
      was_inside = previous[axis] >= bound if minimum else previous[axis] <= bound
      if inside != was_inside:
        fraction = (bound - previous[axis]) / (point[axis] - previous[axis])
        clipped.append(tuple(previous[i] + fraction * (point[i] - previous[i]) for i in range(2)))
      if inside:
        clipped.append(point)
      previous = point
    result = clipped
  # Clamp roundoff at intersections (12.999999999 must not rasterize into the
  # pixel before a 13px scissor boundary).
  return [(min(max(x, box[0]), box[2] - 1), min(max(y, box[1]), box[3] - 1)) for x, y in result]


def line_polygon(line, transform, clip, half_width, z_offset, max_index, max_distance, *, allow_invert=True):
  """Native ModelRenderer ribbon projection, with a forward-depth check added."""
  line = np.asarray(line, dtype=np.float32)
  if line.size == 0:
    return np.empty((0, 2), dtype=np.float32)
  if line.ndim != 2 or line.shape[1] != 3 or len(line) > 256 or not np.isfinite(line).all():
    raise ValueError("Invalid bounded model line")
  points = line[:max_index + 1]
  if 0 < max_index < len(line) - 1:
    p0, p1 = line[max_index], line[max_index + 1]
    point = np.array([max_distance, np.interp(max_distance, [p0[0], p1[0]], [p0[1], p1[1]]),
                      np.interp(max_distance, [p0[0], p1[0]], [p0[2], p1[2]])], dtype=points.dtype)
    points = np.concatenate((points, point[None, :]))
  points = points[points[:, 0] >= 0]
  if not len(points):
    return np.empty((0, 2), dtype=np.float32)
  offsets = np.array([[0, -half_width, z_offset], [0, half_width, z_offset]], dtype=np.float32)
  projected = (transform @ (points[None, :, :] + offsets[:, None, :]).reshape(2 * len(points), 3).T).reshape(3, 2, len(points))
  left, right = projected[:, 0, :], projected[:, 1, :]
  valid = (left[2] >= 1e-6) & (right[2] >= 1e-6)
  left, right = left[:2, valid] / left[2, valid], right[:2, valid] / right[2, valid]
  x, y, width, height = clip
  valid = ((left[0] >= x) & (left[0] <= x + width) & (left[1] >= y) & (left[1] <= y + height)
           & (right[0] >= x) & (right[0] <= x + width) & (right[1] >= y) & (right[1] <= y + height))
  left, right = left[:, valid], right[:, valid]
  if not allow_invert and left.shape[1] > 1:
    keep = left[1] == np.minimum.accumulate(left[1])
    left, right = left[:, keep], right[:, keep]
  return np.vstack((left.T, right[:, ::-1].T)).astype(np.float32)


def lead_polygons(lead, path, transform, rect, height_m, camera_offset_m):
  """Native lead-position, size, glow, and closing-speed opacity formulas."""
  if not lead.present or lead.d_rel <= 0:
    return None
  z = path[path_index(path, lead.d_rel), 2] + height_m
  projected = transform @ [lead.d_rel, -lead.y_rel + camera_offset_m, z]
  if not np.isfinite(projected).all() or projected[2] <= 1e-6:
    return None
  px, py = projected[:2] / projected[2]
  rx, ry, width, height = rect
  if not (rx - 500 <= px <= rx + width + 500 and ry - 500 <= py <= ry + height + 500):
    return None
  alpha = min(255, 255 * (1 - lead.d_rel / 40) + max(0, -lead.v_rel / 10 * 255)) if lead.d_rel < 40 else 0
  size = np.clip(750 / (lead.d_rel / 3 + 30), 15., 30.) * 2.35
  px, py = np.clip(px, 0., width - size / 2), min(py, height - size * 0.6)
  glow = [(px + size * 1.55, py + size * 1.1), (px, py - size * 0.1), (px - size * 1.55, py + size * 1.1)]
  chevron = [(px + size * 1.25, py + size), (px, py), (px - size * 1.25, py + size)]
  return glow, chevron, int(alpha)


def camera_crop(source, size, zoom, tx, ty, x0, y0):
  """Separable bilinear equivalent of the native axis-aligned inverse affine.

  Fractional source boundaries preserve pixel-center placement. The native crop
  is inside the image; rounding the output scissor can extend its edges by less
  than a source pixel. Pad just that case rather than invoking the much slower
  general affine sampler for the entire frame.
  """
  left, top = (x0 - tx) / zoom, (y0 - ty) / zoom
  right, bottom = left + size[0] / zoom, top + size[1] / zoom
  box = (left, top, right, bottom)
  if 0 <= left < right <= source.width and 0 <= top < bottom <= source.height:
    return source.resize(size, Image.Resampling.BILINEAR, box=box)
  if -1 <= left < right <= source.width + 1 and -1 <= top < bottom <= source.height + 1:
    padded = ImageOps.expand(source, border=1, fill=(0, 0, 0))
    return padded.resize(size, Image.Resampling.BILINEAR, box=tuple(v + 1 for v in box))
  return source.transform(size, Image.Transform.AFFINE, (1 / zoom, 0, left, 0, 1 / zoom, top), Image.Resampling.BILINEAR)


@lru_cache(maxsize=64)
def gradient_strip(height, colors, stops):
  """Native vertical gradient: stop zero at the bottom, one at the top."""
  locations = np.linspace(1, 0, height)
  order = np.argsort(stops)
  rgba = np.asarray(colors)[order]
  points = np.asarray(stops)[order]
  channels = [np.interp(locations, points, rgba[:, index]) for index in range(4)]
  return Image.fromarray(np.asarray(channels).T[:, None, :].astype(np.uint8), "RGBA")


class RoadRenderer:
  def __init__(self, viewport):
    self.viewport = viewport
    self.rect = (30, 30, viewport.logical_width - 60, 1020)
    self.clip = (-470, -470, viewport.logical_width + 940, 2020)
    x0, y0 = viewport.to_video(30, 30)
    x1, y1 = viewport.to_video(viewport.logical_width - 30, 1050)
    self.box = tuple(map(round, (x0, y0, x1, y1)))
    self.half_width = 0.9
    self.throttle_blend = 1.0
    self.last_render = None
    self.driver_pose = np.zeros(3)
    self.driver_fade = 0.0

  def _points(self, points):
    return [self.viewport.to_video(float(x), float(y)) for x, y in points]

  def _polygon(self, image, points, color):
    if len(points) >= 3 and color[3] > 0:
      clipped = clip_polygon(self._points(points), self.box)
      if len(clipped) >= 3:
        ImageDraw.Draw(image, "RGBA").polygon(clipped, fill=color)
        return True
    return False

  def _gradient_polygon(self, image, polygon, colors, stops):
    if len(polygon) < 3:
      return False
    x0, y0, x1, y1 = self.box
    mask = Image.new("L", (x1 - x0, y1 - y0))
    ImageDraw.Draw(mask).polygon([(x - x0, y - y0) for x, y in self._points(polygon)], fill=255)
    strip = gradient_strip(y1 - y0, tuple(colors), tuple(stops))
    # Only replicate one column horizontally; bicubic filtering wastes a full
    # content-size resample and cannot improve this already per-row gradient.
    gradient = strip.resize(mask.size, Image.Resampling.NEAREST)
    alpha = ImageChops.multiply(mask, gradient.getchannel("A"))
    if alpha.getbbox() is None:
      return False
    gradient.putalpha(alpha)
    image.paste(gradient, (x0, y0), gradient)
    return True

  def paint(self, image, road, state):
    result = {"camera_displayed": False, "model_displayed": False, "camera_frame_id": None, "model_frame_id": None,
              "display_age_seconds": None, "display_source_timestamp": None, "reason": "Camera unavailable"}
    elapsed = max(0, time.monotonic() - road.captured_at) if road is not None else 0
    if road is None or road.camera is None or road.camera_stale or road.camera_age_seconds + elapsed > MAX_SOURCE_AGE:
      return result
    camera = road.camera
    source = camera.rgb
    if source.mode != "RGB" or source.size != (camera.width, camera.height):
      result["reason"] = "Camera format unavailable"
      return result
    x0, y0, x1, y1 = self.box
    geometry = road.geometry
    if geometry is None:
      # Honest camera-only fallback while calibration/sensor metadata is absent.
      frame = ImageOps.fit(source, (x1 - x0, y1 - y0), method=Image.Resampling.BILINEAR)
      transform = None
    else:
      if source.size != (geometry.width, geometry.height):
        result["reason"] = "Camera geometry mismatch"
        return result
      try:
        video, transform = native_transform(geometry, self.rect, wide=camera.stream == "wide")
      except ValueError:
        result["reason"] = "Calibration unavailable"
        return result
      scale = self.viewport.scale
      ox, oy = self.viewport.offset
      zoom, tx, ty = video[0, 0] * scale, video[0, 2] * scale + ox, video[1, 2] * scale + oy
      frame = camera_crop(source, (x1 - x0, y1 - y0), zoom, tx, ty, x0, y0)
    image.paste(frame.point(GAMMA_LUT), (x0, y0))
    result.update(camera_displayed=True, camera_frame_id=camera.frame_id, display_age_seconds=road.camera_age_seconds + elapsed,
                  display_source_timestamp=road.captured_at - road.camera_age_seconds)
    if transform is None or not geometry.calibrated:
      result["reason"] = "Calibration unavailable"
      return result
    if state.stale or road.model is None or road.model_stale or road.model_age_seconds + elapsed > MAX_SOURCE_AGE:
      result["reason"] = "Vehicle data unavailable" if state.stale else "Model unavailable"
      return result
    model = road.model
    leads = road.leads if road.radar_age_seconds + elapsed <= MAX_SOURCE_AGE else ()
    polygons_drawn = {"path": 0, "lanes": 0, "edges": 0, "leads": 0}
    path = np.asarray(model.position, dtype=np.float32)
    if path.ndim != 2 or path.shape[1] != 3 or not 2 <= len(path) <= 256 or not np.isfinite(path).all():
      result["reason"] = "Model unavailable"
      return result
    path = path.copy()
    path[:, 1] += geometry.camera_offset_m
    max_distance = float(np.clip(path[-1, 0], 10, 100))
    lane_max_index = path_index(model.lane_lines[0], max_distance) if model.lane_lines else 0
    for index, line in enumerate(model.lane_lines[:4]):
      probability = float(np.clip(model.lane_probs[index], 0, 1))
      points = np.asarray(line, dtype=np.float32).copy()
      points[:, 1] += geometry.camera_offset_m
      polygon = line_polygon(points, transform, self.clip, 0.025 * probability, 0, lane_max_index, max_distance)
      polygons_drawn["lanes"] += self._polygon(image, polygon, (255, 255, 255, int(min(probability, 0.7) * 255)))
    for index, line in enumerate(model.road_edges[:2]):
      points = np.asarray(line, dtype=np.float32).copy()
      points[:, 1] += geometry.camera_offset_m
      polygon = line_polygon(points, transform, self.clip, 0.025, 0, lane_max_index, max_distance)
      polygons_drawn["edges"] += self._polygon(image, polygon, (255, 0, 0, int(np.clip(1 - model.edge_stds[index], 0, 1) * 255)))
    lead_used = bool(leads and leads[0].present)
    if lead_used:
      twice_distance = leads[0].d_rel * 2
      max_distance = float(np.clip(twice_distance - min(twice_distance * 0.35, 10), 0, max_distance))
    now = time.monotonic()
    dt = min(0.2, max(0, now - self.last_render)) if self.last_render is not None else 1 / 15
    self.last_render = now
    selfdrive_fresh = road.selfdrive_age_seconds + elapsed <= MAX_SOURCE_AGE
    plan_fresh = road.longitudinal_plan_age_seconds + elapsed <= MAX_SOURCE_AGE
    experimental_mode = road.experimental_mode and selfdrive_fresh
    self.half_width += dt / (0.1 + dt) * ((0.9 if state.display_status in ("engaged", "lat_only") else 0.4) - self.half_width)
    if not selfdrive_fresh or road.longitudinal_control and not plan_fresh:
      # Never fade an old positive throttle indication through source expiry.
      # Real model geometry remains visible using neutral white path colors.
      self.throttle_blend = 0.0
    else:
      self.throttle_blend += dt / (0.25 + dt) * (float(road.allow_throttle or not road.longitudinal_control) - self.throttle_blend)
    polygon = line_polygon(path, transform, self.clip, self.half_width, geometry.height_m, path_index(path, max_distance),
                           max_distance, allow_invert=False)
    colors, stops = [], []
    if experimental_mode:
      index = 0
      limit = min(len(polygon) // 2, len(model.acceleration_x))
      while index < limit:
        y = polygon[index, 1]
        if not self.rect[1] <= y <= self.rect[1] + self.rect[3]:
          index += 1
          continue
        position = 1 - (float(y) - self.rect[1]) / self.rect[3]
        acceleration = model.acceleration_x[index]
        hue = np.clip(60 + acceleration * 35, 0, 120) / 360
        saturation = min(abs(acceleration * 1.5), 1)
        lightness = np.interp(saturation, [0, 1], [0.95, 0.62])
        alpha = np.interp(position, [0.375, 0.75], [0.4, 0])
        colors.append((*[int(v * 255) for v in colorsys.hls_to_rgb(hue, lightness, saturation)], int(alpha * 255)))
        stops.append(position)
        index += 1 + (1 if index + 2 < limit else 0)
    else:
      blend = round(self.throttle_blend * 100) / 100
      colors = [tuple(int((1 - blend) * first + blend * second) for first, second in zip(a, b, strict=True))
                for a, b in zip(NO_THROTTLE_COLORS, THROTTLE_COLORS, strict=True)]
      stops = [0., 0.5, 1.]
    if state.display_status == "disengaged":
      pass  # Explicit projection preference: no path while disengaged.
    elif len(colors) > 1:
      polygons_drawn["path"] = int(self._gradient_polygon(image, polygon, colors, stops))
    else:
      polygons_drawn["path"] = int(self._polygon(image, polygon, (255, 255, 255, 30)))
    lead_used = lead_used and bool(polygons_drawn["path"])
    if road.longitudinal_control:
      for lead in leads[:2]:
        polygons = lead_polygons(lead, path, transform, self.rect, geometry.height_m, geometry.camera_offset_m)
        if polygons is not None:
          glow, chevron, alpha = polygons
          drawn = self._polygon(image, glow, (218, 202, 37, 255))
          polygons_drawn["leads"] += drawn
          lead_used = lead_used or drawn
          self._polygon(image, chevron, (201, 34, 49, alpha))
    displayed_ages = [road.camera_age_seconds]
    if any(polygons_drawn.values()):
      displayed_ages.append(road.model_age_seconds)
    if polygons_drawn["path"] and selfdrive_fresh:
      displayed_ages.append(road.selfdrive_age_seconds)
      if not experimental_mode and road.longitudinal_control and plan_fresh:
        displayed_ages.append(road.longitudinal_plan_age_seconds)
    if lead_used:
      displayed_ages.append(road.radar_age_seconds)
    result.update(model_displayed=any(polygons_drawn.values()), model_available=True, model_frame_id=model.frame_id,
                  polygons_drawn=polygons_drawn, radar_displayed=lead_used,
                  display_age_seconds=max(displayed_ages) + elapsed, display_source_timestamp=road.captured_at - max(displayed_ages), reason="")
    return result

  def draw_driver(self, image, driver, engaged):
    """Native passive face pose and pose arcs; no driver camera is displayed."""
    orientation = np.asarray(driver.face_orientation, dtype=float)
    if orientation.shape != (3,) or not np.isfinite(orientation).all():
      return
    self.driver_fade += 0.2 * ((0 if driver.active else 0.5) - self.driver_fade)
    target = orientation * np.where(orientation < 0, [0.7, 0.4, 0.4], [0.9, 0.4, 0.4])
    difference = abs(self.driver_pose - target)
    self.driver_pose = 0.8 * target + 0.2 * self.driver_pose
    sine, cosine = np.sin(self.driver_pose * (1 - self.driver_fade)), np.cos(self.driver_pose * (1 - self.driver_fade))
    sy, sx, sz = sine
    cy, cx, cz = cosine
    rotation = np.array([[cx * cz, cx * sz, -sx], [-sy * sx * cz - cy * sz, -sy * sx * sz + cy * cz, -sy * cx],
                         [cy * sx * cz - sy * sz, cy * sx * sz + sy * cz, cy * cx]])
    points = FACE_KEYPOINTS @ rotation.T
    points[:, 2] = points[:, 2] * (1 - self.driver_fade) + 8 * self.driver_fade
    center = np.array([self.viewport.logical_width - 156 if driver.is_rhd else 156, 924])
    face = points[:, :2] * ((points[:, 2] - 8) / 120 + 1)[:, None] + center
    draw = ImageDraw.Draw(image, "RGBA")
    draw.line(self._points(face), fill=(255, 255, 255, 166 if driver.active else 51), width=max(1, round(5.2 * self.viewport.scale)))
    arc_color = (26, 242, 66) if engaged else (139, 139, 139)
    for horizontal, index in ((True, 1), (False, 0)):
      delta = -sine[index] * 133 / 2
      size = abs(delta)
      if size <= 1e-6:
        continue
      x, y = center + ([0, -133 / 2] if horizontal else [-133 / 2, 0])
      start = (90 if sine[index] > 0 else -90) if horizontal else (0 if sine[index] > 0 else 180)
      x, y = (min(x + delta, x), y) if horizontal else (x, min(y + delta, y))
      width, height = (size, 133) if horizontal else (133, size)
      angles = np.linspace(0, np.pi, 37) + np.deg2rad(start)
      arc = np.column_stack((x + width / 2 + np.cos(angles) * width / 2, y + height / 2 - np.sin(angles) * height / 2))
      thickness = (6.7 + 12 * min(1, difference[index] * 5)) * self.viewport.scale
      draw.line(self._points(arc), fill=(*arc_color, int(102 * (1 - self.driver_fade))), width=max(1, round(thickness)))
