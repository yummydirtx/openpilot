"""Landscape HUD painting shared by the native UI and projection experiments.

Callers supply already-resolved display state, fonts, and text functions so this
module does not subscribe to Cereal, read Params, or own interactive controls.
"""

from dataclasses import dataclass
import pyray as rl

CRUISE_DISABLED_CHAR = '–'


@dataclass(frozen=True)
class UIConfig:
  header_height: int = 300
  border_size: int = 30
  button_size: int = 192
  set_speed_width_metric: int = 200
  set_speed_width_imperial: int = 172
  set_speed_height: int = 204
  wheel_icon_size: int = 144


@dataclass(frozen=True)
class FontSizes:
  current_speed: int = 176
  speed_unit: int = 66
  max_speed: int = 40
  set_speed: int = 90


@dataclass(frozen=True)
class Colors:
  WHITE = rl.WHITE
  DISENGAGED = rl.Color(145, 155, 149, 255)
  OVERRIDE = rl.Color(145, 155, 149, 255)
  ENGAGED = rl.Color(128, 216, 166, 255)
  DISENGAGED_BG = rl.Color(0, 0, 0, 153)
  OVERRIDE_BG = rl.Color(145, 155, 149, 204)
  ENGAGED_BG = rl.Color(128, 216, 166, 204)
  GREY = rl.Color(166, 166, 166, 255)
  DARK_GREY = rl.Color(114, 114, 114, 255)
  BLACK_TRANSLUCENT = rl.Color(0, 0, 0, 166)
  WHITE_TRANSLUCENT = rl.Color(255, 255, 255, 200)
  BORDER_TRANSLUCENT = rl.Color(255, 255, 255, 75)
  HEADER_GRADIENT_START = rl.Color(0, 0, 0, 114)
  HEADER_GRADIENT_END = rl.BLANK


UI_CONFIG = UIConfig()
FONT_SIZES = FontSizes()
COLORS = Colors()


def draw_set_speed(rect, *, is_metric, status, is_cruise_set, set_speed, font_semi_bold, font_bold, max_text, draw_text, measure_text, backend=rl):
  set_speed_width = UI_CONFIG.set_speed_width_metric if is_metric else UI_CONFIG.set_speed_width_imperial
  x = rect.x + 60 + (UI_CONFIG.set_speed_width_imperial - set_speed_width) // 2
  y = rect.y + 45
  set_speed_rect = backend.Rectangle(x, y, set_speed_width, UI_CONFIG.set_speed_height)
  backend.draw_rectangle_rounded(set_speed_rect, 0.35, 10, COLORS.BLACK_TRANSLUCENT)
  backend.draw_rectangle_rounded_lines_ex(set_speed_rect, 0.35, 10, 6, COLORS.BORDER_TRANSLUCENT)

  max_color = COLORS.GREY
  set_speed_color = COLORS.DARK_GREY
  if is_cruise_set:
    set_speed_color = COLORS.WHITE
    max_color = {"engaged": COLORS.ENGAGED, "disengaged": COLORS.DISENGAGED, "override": COLORS.OVERRIDE}.get(status, max_color)

  max_text_width = measure_text(font_semi_bold, max_text, FONT_SIZES.max_speed).x
  draw_text(font_semi_bold, max_text, backend.Vector2(x + (set_speed_width - max_text_width) / 2, y + 27),
            FONT_SIZES.max_speed, 0, max_color)
  set_speed_text = CRUISE_DISABLED_CHAR if not is_cruise_set else str(round(set_speed))
  speed_text_width = measure_text(font_bold, set_speed_text, FONT_SIZES.set_speed).x
  draw_text(font_bold, set_speed_text, backend.Vector2(x + (set_speed_width - speed_text_width) / 2, y + 77),
            FONT_SIZES.set_speed, 0, set_speed_color)


def draw_current_speed(rect, *, speed_text, unit_text, font_bold, font_medium, draw_text, measure_text, backend=rl):
  speed_text_size = measure_text(font_bold, speed_text, FONT_SIZES.current_speed)
  speed_pos = backend.Vector2(rect.x + rect.width / 2 - speed_text_size.x / 2, 180 - speed_text_size.y / 2)
  draw_text(font_bold, speed_text, speed_pos, FONT_SIZES.current_speed, 0, COLORS.WHITE)
  unit_text_size = measure_text(font_medium, unit_text, FONT_SIZES.speed_unit)
  unit_pos = backend.Vector2(rect.x + rect.width / 2 - unit_text_size.x / 2, 290 - unit_text_size.y / 2)
  draw_text(font_medium, unit_text, unit_pos, FONT_SIZES.speed_unit, 0, COLORS.WHITE_TRANSLUCENT)
