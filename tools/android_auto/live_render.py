"""CPU-only landscape road/HUD renderer for a read-only projection process.

Pillow implements the small drawing surface used by the actual native speed/MAX
painters. Importing pyray for their color constants does not create a window;
this renderer never initializes raylib, EGL, DRM, or an openpilot UI instance.
RoadSnapshot optionally supplies the actual camera, model, and driver state.
No synthetic scenery is used when those sources are unavailable.
"""

from dataclasses import dataclass
import importlib
import importlib.util
import math
from pathlib import Path
import sys
import time

from PIL import Image, ImageDraw, ImageFont

FONT_SCALE = 1.242  # Native BIG=1 landscape convention.
BORDER_COLORS = {"disengaged": (18, 40, 57), "override": (137, 146, 141), "engaged": (22, 127, 64),
                 "lat_only": (0, 200, 200), "long_only": (150, 28, 168)}
STATUS_LABELS = {"disengaged": "Disengaged", "override": "Driver override", "engaged": "Engaged",
                 "lat_only": "Lateral control", "long_only": "Longitudinal control"}
ALERT_COLORS = {0: (21, 21, 21, 241), 1: (218, 111, 37, 241), 2: (201, 34, 49, 241)}


@dataclass(frozen=True)
class Rectangle:
  x: float
  y: float
  width: float
  height: float


@dataclass(frozen=True)
class Vector2:
  x: float
  y: float


def rgba(color):
  return (color.r, color.g, color.b, color.a) if hasattr(color, "r") else tuple(color)


class PillowBackend:
  Rectangle = Rectangle
  Vector2 = Vector2

  def __init__(self, viewport, assets):
    self.viewport = viewport
    self.assets = Path(assets)
    self.image = None
    self.draw = None
    self._fonts = {}
    self._text_masks = {}
    self._measurements = {}
    for name in ("Bold", "Medium", "SemiBold"):
      # Fail before a stream starts if assets are missing or still LFS pointers.
      self.font(name, 40)

  def font(self, name, size):
    pixels = max(1, round(size * FONT_SCALE * self.viewport.scale))
    key = name, pixels
    if key not in self._fonts:
      if len(self._fonts) >= 64:
        self._fonts.pop(next(iter(self._fonts)))
      self._fonts[key] = ImageFont.truetype(str(self.assets / "fonts" / f"Inter-{name}.ttf"), pixels)
    return self._fonts[key]

  def begin(self):
    self.image = Image.new("RGB", (self.viewport.width, self.viewport.height))
    self.draw = ImageDraw.Draw(self.image, "RGBA")

  def bounds(self, rect):
    x, y = self.viewport.to_video(rect.x, rect.y)
    return (round(x), round(y), round(x + rect.width * self.viewport.scale), round(y + rect.height * self.viewport.scale))

  def draw_rectangle_rounded(self, rect, roundness, _segments, color):
    self.draw.rounded_rectangle(self.bounds(rect), radius=min(rect.width, rect.height) * roundness * self.viewport.scale / 2, fill=rgba(color))

  def draw_rectangle_rounded_lines_ex(self, rect, roundness, _segments, thickness, color):
    self.draw.rounded_rectangle(self.bounds(rect), radius=min(rect.width, rect.height) * roundness * self.viewport.scale / 2,
                                outline=rgba(color), width=max(1, round(thickness * self.viewport.scale)))

  def measure(self, font, text, size):
    key = font, text, size
    if key not in self._measurements:
      if len(self._measurements) >= 256:
        self._measurements.pop(next(iter(self._measurements)))
      self._measurements[key] = Vector2(self.font(font, size).getlength(text) / self.viewport.scale, size * FONT_SCALE)
    return self._measurements[key]

  def text(self, font, text, position, size, spacing, color):
    if spacing:
      raise ValueError("The HUD text adapter supports native zero-spacing text only")
    red, green, blue, alpha = rgba(color)
    x, y = self.viewport.to_video(position.x, position.y)
    floor_x, floor_y = math.floor(x), math.floor(y)
    phase_x, phase_y = x - floor_x, y - floor_y
    # Pillow's glyph placement uses the subpixel origin; include it so caching
    # cannot shift individual characters in centered speed/unit labels.
    key = font, text, size, alpha, phase_x, phase_y
    if key not in self._text_masks:
      face = self.font(font, size)
      left, top, right, bottom = face.getbbox(text, anchor="lt")
      if right <= left or bottom <= top:
        return
      mask = Image.new("L", (right - left + 2, bottom - top + 2))
      ImageDraw.Draw(mask).text((phase_x - left, phase_y - top), text, font=face, fill=alpha, anchor="lt")
      if len(self._text_masks) >= 128:
        self._text_masks.pop(next(iter(self._text_masks)))
      self._text_masks[key] = (mask, left, top)
    mask, left, top = self._text_masks[key]
    self.image.paste((red, green, blue), (floor_x + left, floor_y + top), mask)

  def centered_text(self, text, y, size, color=(255, 255, 255, 255), font="Medium"):
    width = self.measure(font, text, size).x
    self.text(font, text, Vector2((self.viewport.logical_width - width) / 2, y), size, 0, color)


class LiveRenderer:
  """Render LiveDisplayState to a cached PIL RGB frame at negotiated dimensions.

  The caller owns pacing and transport. Returned images must not be mutated;
  identical visual state reuses the same image without repainting or rereading
  assets. Live ages are intentionally not rendered as rapidly changing text.
  """

  def __init__(self, viewport, assets, hud_path=None):
    if viewport.width > 1920 or viewport.height > 1080 or viewport.logical_width < 1400:
      raise ValueError("Live HUD requires a landscape viewport no larger than 1920x1080")
    self.viewport = viewport
    self.backend = PillowBackend(viewport, assets)
    if hud_path is None:
      self.hud = importlib.import_module("openpilot.selfdrive.ui.onroad.hud_drawing")
    else:
      # Deploy a copy of this one tracked native source file outside the running
      # openpilot checkout. Never replace its UI modules or monkeypatch raylib.
      spec = importlib.util.spec_from_file_location("_automaxxing_native_hud", Path(hud_path))
      if spec is None or spec.loader is None:
        raise ValueError("Could not load the shared native HUD source")
      self.hud = importlib.util.module_from_spec(spec)
      sys.modules[spec.name] = self.hud
      spec.loader.exec_module(self.hud)
    self._signature = None
    self._image = None
    self._road_renderer = None
    self._icons = {}
    self.road_metadata = {}

  def render(self, state, road=None):
    alert = state.alert
    signature = (state.speed_text, state.set_speed, state.is_metric, state.is_cruise_set, state.display_status,
                 state.stale, state.started, state.hide_speed, state.source,
                 None if alert is None else (alert.text1, alert.text2, alert.size, alert.status))
    if road is None and self._road_renderer is None and signature == self._signature:
      return self._image
    if state.source != "live":
      raise ValueError("Live renderer requires an explicitly live state source")
    b = self.backend
    b.begin()
    width = self.viewport.logical_width
    usable = Rectangle(0, 0, width, 1080)
    # Opaque fills do not need ImageDraw's per-pixel RGBA blend path.
    b.image.paste((9, 13, 16), b.bounds(usable))
    rect = Rectangle(30, 30, width - 60, 1020)
    self.road_metadata = {}
    if road is not None:
      if self._road_renderer is None:
        from tools.android_auto.road_render import RoadRenderer
        self._road_renderer = RoadRenderer(self.viewport)
      self.road_metadata = self._road_renderer.paint(b.image, road, state)
      if self.road_metadata["camera_displayed"]:
        self._header_gradient(rect)
    if state.is_cruise_available:
      self.hud.draw_set_speed(rect, is_metric=state.is_metric, status=state.display_status, is_cruise_set=state.is_cruise_set,
                             set_speed=state.set_speed, font_semi_bold="SemiBold", font_bold="Bold", max_text="MAX",
                             draw_text=b.text, measure_text=b.measure, backend=b)
    if not state.hide_speed:
      self.hud.draw_current_speed(rect, speed_text=state.speed_text, unit_text=state.unit_text, font_bold="Bold", font_medium="Medium",
                         draw_text=b.text, measure_text=b.measure, backend=b)
    status = state.display_status
    color = BORDER_COLORS.get(status, BORDER_COLORS["disengaged"])
    border_width = max(1, round(30 * self.viewport.scale))
    # Keep each symmetric negotiated margin fully black. Pillow rectangles have
    # inclusive right/bottom edges, so subtract one physical pixel here.
    x0, y0, x1, y1 = b.bounds(usable)
    b.draw.rectangle((x0, y0, x1 - 1, y1 - 1), outline=color, width=border_width)
    camera_displayed = self.road_metadata.get("camera_displayed", False)
    if road is not None:
      selfdrive_age = road.selfdrive_age_seconds + max(0, time.monotonic() - road.captured_at)
      selfdrive_fresh = selfdrive_age <= 0.35
      self._icon("experimental" if selfdrive_fresh and road.experimental_mode else "chffr_wheel", width - 156, 156,
                 180 if state.stale or not selfdrive_fresh or not road.engageable else 255)
      self.road_metadata["wheel_state_displayed"] = selfdrive_fresh
      if selfdrive_fresh:
        self.road_metadata["display_age_seconds"] = max(self.road_metadata.get("display_age_seconds") or 0, selfdrive_age)
        self._include_road_source(road.captured_at - road.selfdrive_age_seconds)
      if (camera_displayed and road.driver_state is not None and alert is None
          and road.driver_age_seconds + max(0, time.monotonic() - road.captured_at) <= 0.35):
        driver = road.driver_state
        self._icon("driver_face", width - 156 if driver.is_rhd else 156, 924, 166 if driver.active else 51, background_alpha=70)
        self._road_renderer.draw_driver(b.image, driver, state.display_status in ("engaged", "lat_only"))
        self.road_metadata["driver_displayed"] = True
        self.road_metadata["display_age_seconds"] = max(self.road_metadata["display_age_seconds"],
                                                        road.driver_age_seconds + max(0, time.monotonic() - road.captured_at))
        self._include_road_source(road.captured_at - road.driver_age_seconds)
    if state.stale:
      label, sublabel = "Data unavailable", "Check the comma display"
    elif not state.started:
      label, sublabel = "Waiting for drive", "Live vehicle display"
    else:
      label, sublabel = STATUS_LABELS.get(status, "Disengaged"), "Live vehicle display"
    if not camera_displayed or state.stale:
      if camera_displayed:
        b.draw_rectangle_rounded(Rectangle(width / 2 - 600, 440, 1200, 300), 0.1, 10, (0, 0, 0, 195))
      if road is not None and not state.stale and not camera_displayed:
        sublabel = self.road_metadata.get("reason", "Camera unavailable")
      b.centered_text(label, 500, 84, font="Bold")
      b.centered_text(sublabel, 650, 34, (177, 185, 188, 255))
    if camera_displayed:
      reason = self.road_metadata.get("reason")
      if reason and not state.stale:
        b.draw_rectangle_rounded(Rectangle(width / 2 - 420, 940, 840, 90), 0.15, 10, (0, 0, 0, 195))
        b.centered_text(reason, 955, 36)
    else:
      b.centered_text("AUTOMAXXING  |  LIVE HUD", 980, 28, (150, 162, 168, 255))
    if alert is not None and alert.size:
      self._alert(alert)
    # Clamp all drawing, including unusually long alert text, to the video area.
    if y0:
      b.image.paste((0, 0, 0), (0, 0, self.viewport.width, y0))
    if y1 < self.viewport.height:
      b.image.paste((0, 0, 0), (0, y1, self.viewport.width, self.viewport.height))
    if x0:
      b.image.paste((0, 0, 0), (0, 0, x0, self.viewport.height))
    if x1 < self.viewport.width:
      b.image.paste((0, 0, 0), (x1, 0, self.viewport.width, self.viewport.height))
    self._signature = signature
    self._image = b.image
    return self._image

  def _include_road_source(self, timestamp):
    """Keep source time independent of time spent polling or drawing this frame."""
    previous = self.road_metadata.get("display_source_timestamp")
    self.road_metadata["display_source_timestamp"] = timestamp if previous is None else min(previous, timestamp)

  def _header_gradient(self, rect):
    from tools.android_auto.road_render import gradient_strip
    box = self.backend.bounds(Rectangle(rect.x, rect.y, rect.width, 300))
    gradient = gradient_strip(box[3] - box[1], ((0, 0, 0, 0), (0, 0, 0, 114)), (0., 1.)).resize(
      (box[2] - box[0], box[3] - box[1]), Image.Resampling.NEAREST)
    self.backend.image.paste(gradient, box[:2], gradient)

  def _icon(self, name, x, y, opacity, *, background_alpha=166):
    b = self.backend
    if name not in self._icons:
      path = b.assets / "icons" / f"{name}.png"
      try:
        self._icons[name] = Image.open(path).convert("RGBA").resize((round(144 * self.viewport.scale),) * 2, Image.Resampling.LANCZOS)
      except (FileNotFoundError, OSError):
        # The wheel/face are required for the full display. An absent optional
        # experimental asset is explicitly labelled instead of implying normal mode.
        if name != "experimental":
          raise
        self._icons[name] = None
    b.draw.ellipse(b.bounds(Rectangle(x - 96, y - 96, 192, 192)), fill=(0, 0, 0, background_alpha))
    icon = self._icons[name]
    if icon is None:
      px, py = self.viewport.to_video(x, y - 25)
      font = b.font("Bold", 35)
      b.draw.text((px - font.getlength("EXP") / 2, py), "EXP", font=font, fill=(255, 255, 255, opacity))
    else:
      if opacity < 255:
        icon = icon.copy()
        icon.putalpha(icon.getchannel("A").point([round(v * opacity / 255) for v in range(256)]))
      self.backend.image.paste(icon, tuple(round(v) for v in self.viewport.to_video(x - 72, y - 72)), icon)

  def _alert(self, alert):
    b = self.backend
    # These are mirrored text alerts; native audio/controls remain on the comma.
    # Match native AlertRenderer's 40px margins, 271/420px heights, and full
    # content-area critical alert; shrink only when a translated message needs it.
    full = alert.size == 3
    height = 1020 if full else {1: 271, 2: 420}.get(alert.size, 420) - 80
    width = self.viewport.logical_width - (60 if full else 140)
    y = 30 if full else 1010 - height
    rect = Rectangle(30 if full else 70, y, width, height)
    b.draw_rectangle_rounded(rect, 0 if full else 60 / min(width, height), 10, ALERT_COLORS.get(alert.status, ALERT_COLORS[0]))
    sizes = (132 if len(alert.text1) > 15 else 177, 88) if full else (88, 66) if alert.size == 2 else (74, 0)
    # Fit both complete messages, wrapping words and shrinking if necessary.
    texts = (alert.text1[:512], alert.text2[:512] if alert.size != 1 else "")
    for factor in (1, 0.85, 0.7, 0.55, 0.4):
      blocks = [(self._wrap(text, max(1, round(size * factor)), width - 120), max(1, round(size * factor)))
                for text, size in zip(texts, sizes, strict=True)]
      total = sum(len(lines) * size * FONT_SCALE * 1.18 for lines, size in blocks)
      if total <= height - 40:
        break
    top = y + max(20, (height - total) / 2)
    for lines, size in blocks:
      for line in lines:
        b.centered_text(line, top, size, font="Bold")
        top += size * FONT_SCALE * 1.18

  def _wrap(self, text, size, max_width):
    lines = []
    for paragraph in text.splitlines():
      line = ""
      for word in paragraph.split():
        candidate = f"{line} {word}".strip()
        if line and self.backend.measure("Bold", candidate, size).x > max_width:
          lines.append(line)
          line = word
        else:
          line = candidate
      if line:
        lines.append(line)
    return lines
