"""Render a scalable comma 3X HUD with an explicitly synthetic road scene.

Uses the native landscape HUD paint functions and existing Inter/icon assets.
The road, path, lead, and demo transitions are fixtures, not ModelRenderer or a
camera feed. Writes a local H.264 clip for the independent Android Auto sender.
"""

import argparse
from pathlib import Path
import math
import subprocess

import pyray as rl

from openpilot.selfdrive.ui.onroad.hud_drawing import COLORS, UI_CONFIG, draw_current_speed, draw_set_speed
from tools.android_auto.viewport import Viewport, demo_state

ASSETS = Path(__file__).resolve().parents[2] / ".cache/automaxxing/ui-assets"
FONT_SCALE = 1.242  # Native BIG=1 landscape font convention in application.py.
BORDER_COLORS = {"disengaged": (18, 40, 57, 255), "override": (137, 146, 141, 255), "engaged": (22, 127, 64, 255)}


class PreviewRenderer:
  def __init__(self, viewport):
    self.viewport = viewport
    self.target = rl.load_render_texture(viewport.width, viewport.height)
    glyphs = rl.ffi.new("int[]", list(range(32, 127)) + [8211])
    self.fonts = {name: rl.load_font_ex(str(ASSETS / "fonts" / f"Inter-{name}.ttf"), 200, rl.ffi.cast("int *", glyphs), len(glyphs))
                  for name in ("SemiBold", "Bold", "Medium")}
    self.icons = {name: rl.load_texture(str(ASSETS / "icons" / f"{name}.png")) for name in ("chffr_wheel", "driver_face")}
    if any(texture.id == 0 for texture in self.icons.values()) or any(font.texture.id == rl.get_font_default().texture.id for font in self.fonts.values()):
      raise ValueError("Native assets could not be loaded; run python3 -m tools.android_auto.prepare_assets first")
    for font in self.fonts.values():
      rl.gen_texture_mipmaps(font.texture)
      rl.set_texture_filter(font.texture, rl.TextureFilter.TEXTURE_FILTER_TRILINEAR)

  @staticmethod
  def measure(font, text, size):
    # The native helper imports GuiApplication and live hardware dependencies.
    return rl.measure_text_ex(font, text, size * FONT_SCALE, 0)  # noqa: TID251

  @staticmethod
  def text(font, text, position, size, spacing, color):
    rl.draw_text_ex(font, text, position, size * FONT_SCALE, spacing, color)

  @staticmethod
  def polygon(points, color):
    # Normalize winding so fixtures with either vertex order remain visible.
    for index in range(1, len(points) - 1):
      a, b, c = points[0], points[index], points[index + 1]
      if (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0]) > 0:
        b, c = c, b
      rl.draw_triangle(rl.Vector2(*a), rl.Vector2(*b), rl.Vector2(*c), color)

  def draw_road(self, width, seconds, status, stale):
    horizon = 460
    center = width / 2
    bend = math.sin(seconds * 0.4) * 100
    rl.draw_rectangle_gradient_v(0, 0, round(width), horizon + 80, rl.Color(49, 68, 89, 255), rl.Color(143, 153, 158, 255))
    rl.draw_rectangle(0, horizon, round(width), 1080 - horizon, rl.Color(64, 78, 67, 255))
    self.polygon([(center - 100 + bend, horizon), (center + 100 + bend, horizon), (width, 1080), (0, 1080)], rl.Color(49, 53, 57, 255))
    for side in (-1, 1):
      for index in range(9):
        depth = ((index / 9 + seconds * 0.35) % 1) ** 2
        next_depth = min(1, depth + 0.055)
        def point(d, offset, side=side):
          return (center + bend * (1 - d) + side * (65 + d * width * 0.38) + offset, horizon + d * 620)
        self.polygon([point(depth, -3), point(depth, 3), point(next_depth, 8), point(next_depth, -8)], rl.Color(226, 225, 205, 220))
    if not stale:
      path_color = rl.Color(30, 220, 117, 150) if status == "engaged" else rl.Color(205, 213, 211, 100)
      for index in range(30):
        d0, d1 = index / 30, (index + 1) / 30
        def edge(d, side):
          return (center + bend * (1 - d) ** 2 + side * (18 + d * 145), horizon + 30 + d * 590)
        self.polygon([edge(d0, -1), edge(d0, 1), edge(d1, 1), edge(d1, -1)], path_color)
      lead_x = center + bend * 0.6
      lead_y = 565 + 10 * math.sin(seconds)
      self.polygon([(lead_x - 42, lead_y), (lead_x, lead_y + 38), (lead_x + 42, lead_y)], rl.Color(225, 52, 58, 255))

  def icon(self, name, x, y, size, tint=rl.WHITE):
    texture = self.icons[name]
    rl.draw_texture_pro(texture, rl.Rectangle(0, 0, texture.width, texture.height), rl.Rectangle(x, y, size, size), rl.Vector2(0, 0), 0, tint)

  def render(self, seconds):
    state = demo_state(seconds)
    viewport = self.viewport
    width = viewport.logical_width
    rl.begin_texture_mode(self.target)
    rl.clear_background(rl.BLACK)
    ox, oy = viewport.offset
    rl.begin_scissor_mode(round(ox), round(oy), viewport.width - viewport.margin_width, viewport.height - viewport.margin_height)
    rl.rl_push_matrix()
    rl.rl_translatef(ox, oy, 0)
    rl.rl_scalef(viewport.scale, viewport.scale, 1)
    self.draw_road(width, seconds, state.display_status, state.stale)
    rect = rl.Rectangle(30, 30, width - 60, 1020)
    rl.draw_rectangle_gradient_v(30, 30, round(width - 60), UI_CONFIG.header_height, COLORS.HEADER_GRADIENT_START, COLORS.HEADER_GRADIENT_END)
    draw_set_speed(rect, is_metric=False, status=state.display_status, is_cruise_set=not state.stale,
                   set_speed=state.set_speed, font_semi_bold=self.fonts["SemiBold"], font_bold=self.fonts["Bold"], max_text="MAX",
                   draw_text=self.text, measure_text=self.measure)
    draw_current_speed(rect, speed_text=state.speed_text, unit_text="mph", font_bold=self.fonts["Bold"], font_medium=self.fonts["Medium"],
                       draw_text=self.text, measure_text=self.measure)
    rl.draw_circle(round(width - 156), 156, 96, COLORS.BLACK_TRANSLUCENT)
    self.icon("chffr_wheel", width - 228, 84, 144, rl.Color(255, 255, 255, 100 if state.stale else 255))
    rl.draw_circle(156, 910, 96, rl.Color(0, 0, 0, 110))
    self.icon("driver_face", 84, 838, 144, rl.Color(255, 255, 255, 160))
    border = rl.Color(*BORDER_COLORS[state.display_status])
    rl.draw_rectangle_lines_ex(rl.Rectangle(0, 0, width, 1080), 30, border)
    if state.stale:
      rl.draw_rectangle_rounded(rl.Rectangle(290, 740, width - 580, 200), 0.15, 10, rl.Color(21, 21, 21, 245))
      label = "Data unavailable"
      size = self.measure(self.fonts["Bold"], label, 65)
      self.text(self.fonts["Bold"], label, rl.Vector2((width - size.x) / 2, 790), 65, 0, rl.WHITE)
    # The source label persists through every transition, including stale data.
    label = f"SYNTHETIC DEMO  |  COMMA 3X HUD  |  {seconds:04.1f}s"
    size = self.measure(self.fonts["Medium"], label, 29)
    rl.draw_rectangle(30, 1000, round(width - 60), 50, rl.Color(0, 0, 0, 215))
    self.text(self.fonts["Medium"], label, rl.Vector2((width - size.x) / 2, 1005), 29, 0, rl.WHITE)
    rl.rl_pop_matrix()
    rl.end_scissor_mode()
    rl.end_texture_mode()

  def image(self):
    image = rl.load_image_from_texture(self.target.texture)
    rl.image_flip_vertical(image)
    return image

  def close(self):
    for font in self.fonts.values():
      rl.unload_font(font)
    for texture in self.icons.values():
      rl.unload_texture(texture)
    rl.unload_render_texture(self.target)


def main():
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument("--width", type=int, default=800)
  parser.add_argument("--height", type=int, default=480)
  parser.add_argument("--margin-width", type=int, default=0)
  parser.add_argument("--margin-height", type=int, default=0)
  parser.add_argument("--seconds", type=float, default=12)
  parser.add_argument("--fps", type=int, default=30)
  parser.add_argument("--output", type=Path, default=Path(".cache/automaxxing/preview"))
  args = parser.parse_args()
  if args.width % 2 or args.height % 2 or args.width > 1920 or args.height > 1080 or not 0 < args.seconds <= 60 or args.fps not in (30, 60):
    parser.error("Use even dimensions up to 1920x1080, 30/60 fps, and a duration up to 60 seconds")
  viewport = Viewport(args.width, args.height, args.margin_width, args.margin_height)
  if viewport.logical_width < 1400:
    parser.error("Preview needs a landscape usable viewport")
  args.output.mkdir(parents=True, exist_ok=True)
  if not (ASSETS / "fonts/Inter-Bold.ttf").exists():
    parser.error("Run python3 -m tools.android_auto.prepare_assets first")
  rl.set_trace_log_level(rl.TraceLogLevel.LOG_WARNING)
  rl.set_config_flags(rl.ConfigFlags.FLAG_WINDOW_HIDDEN)
  rl.init_window(args.width, args.height, "Automaxxing synthetic renderer")
  renderer = PreviewRenderer(viewport)
  level = ("4.0" if args.fps == 30 else "4.2") if args.height > 720 else ("3.1" if args.fps == 30 else "3.2")
  encoder = subprocess.Popen(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgba",
                              "-s", f"{args.width}x{args.height}", "-r", str(args.fps), "-i", "pipe:0", "-an", "-c:v", "libx264",
                              "-preset", "ultrafast", "-tune", "zerolatency", "-profile:v", "baseline", "-level:v", level,
                              "-pix_fmt", "yuv420p", "-x264-params", f"aud=1:repeat-headers=1:keyint={args.fps}:scenecut=0",
                              "-f", "h264", str(args.output / "preview.h264")], stdin=subprocess.PIPE)
  try:
    for index in range(round(args.seconds * args.fps)):
      renderer.render(index / args.fps)
      image = renderer.image()
      try:
        if index in {0, args.fps * 2, args.fps * 7, args.fps * 10}:
          rl.export_image(image, str(args.output / f"preview-{index // args.fps:02d}s.png"))
        encoder.stdin.write(bytes(rl.ffi.buffer(image.data, args.width * args.height * 4)))
      finally:
        rl.unload_image(image)
    encoder.stdin.close()
    if encoder.wait(timeout=15) != 0:
      raise RuntimeError("H.264 encoder failed")
  finally:
    if encoder.poll() is None:
      encoder.kill()
      encoder.wait()
    renderer.close()
    rl.close_window()
  print(args.output / "preview.h264")


if __name__ == "__main__":
  main()
