"""Rotary-operated, projection-only controls. Does not alter driving settings."""

from dataclasses import dataclass
from functools import lru_cache

ITEMS = (("road", "Road view"), ("hud", "Speed and alerts"), ("local", "Use comma display"), ("exit", "Mazda Connect"))


@dataclass
class ProjectionMenu:
  opened: bool = False
  selected: int = 0
  view: str = "road"

  def reset(self):
    self.opened = False
    self.selected = 0

  def handle(self, action):
    name = action.name
    if name in ("music", "navigation"):
      self.reset()
      return "exit"  # Yield to Mazda; never impersonate an unimplemented app.
    if name == "home":
      self.reset()
      self.view = "road"
      return None
    if name == "back":
      if self.opened:
        self.reset()
      else:
        self.opened, self.selected = True, len(ITEMS) - 1
      return None
    if name not in ("rotate", "up", "down", "left", "right", "select", "menu"):
      return None
    if not self.opened:
      self.opened = True
      self.selected = 0
      return None
    if name in ("rotate", "up", "down", "left", "right"):
      delta = action.steps if name == "rotate" else -1 if name in ("up", "left") else 1
      self.selected = max(0, min(len(ITEMS) - 1, self.selected + delta))
    elif name == "select":
      command = ITEMS[self.selected][0]
      self.reset()
      if command in ("road", "hud"):
        self.view = command
      else:
        return command
    return None

  def snapshot(self):
    return {"opened": self.opened, "selected": self.selected, "view": self.view}


@lru_cache(maxsize=8)
def _font(assets, size):
  from PIL import ImageFont
  from pathlib import Path
  return ImageFont.truetype(str(Path(assets) / "fonts/Inter-Medium.ttf"), size)


def draw_menu(image, viewport, snapshot, assets):
  """Draw after copying cached HUD pixels; never cover an active native alert."""
  from PIL import ImageDraw
  image = image.copy()
  draw = ImageDraw.Draw(image)
  ox, oy = viewport.offset
  width, height = viewport.width - 2 * ox, viewport.height - 2 * oy
  size = max(15, round(height * 0.043))
  font = _font(str(assets), size)
  if not snapshot["opened"]:
    text = "Press knob for menu"
    box = draw.textbbox((0, 0), text, font=font)
    x, y = ox + width - (box[2] - box[0]) - 25, oy + height - size - 22
    draw.rounded_rectangle((x - 9, y - 5, ox + width - 16, y + size + 9), radius=7, fill=(20, 26, 32))
    draw.text((x, y), text, font=font, fill="white")
    return image
  panel_width = min(width - 40, max(320, width * 0.32))
  x, y = ox + width - panel_width - 30, oy + height * 0.32
  row = height * 0.135
  draw.rounded_rectangle((x - 12, y - 12, x + panel_width + 12, y + row * 4 + 12), radius=14, fill=(14, 20, 26))
  for i, (key, label) in enumerate(ITEMS):
    top = y + i * row
    selected = i == snapshot["selected"]
    draw.rounded_rectangle((x, top, x + panel_width, top + row - 5), radius=8,
                           fill=(40, 115, 188) if selected else (33, 43, 51))
    if key == snapshot["view"]:
      label += "  *"
    draw.text((x + 14, top + (row - size) / 2 - 3), label, font=font, fill="white")
  return image
