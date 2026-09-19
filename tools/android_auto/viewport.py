"""Uniform scaling into the head unit's usable video rectangle."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Viewport:
  width: int
  height: int
  margin_width: int = 0
  margin_height: int = 0

  def __post_init__(self):
    if not (0 <= self.margin_width < self.width and 0 <= self.margin_height < self.height):
      raise ValueError("Margins must leave a positive video area")

  @property
  def scale(self):
    # Same x/y scale: maintain native HUD proportions and adapt its logical width.
    return (self.height - self.margin_height) / 1080

  @property
  def logical_width(self):
    return (self.width - self.margin_width) / self.scale

  @property
  def offset(self):
    return self.margin_width / 2, self.margin_height / 2

  def to_video(self, x, y):
    ox, oy = self.offset
    return ox + x * self.scale, oy + y * self.scale


@dataclass(frozen=True)
class DisplayState:
  speed: float | None
  set_speed: float | None
  status: str
  source: str = "synthetic"
  age_seconds: float = 0

  @property
  def stale(self):
    return self.age_seconds > 0.5 or self.speed is None

  @property
  def display_status(self):
    return "disengaged" if self.stale else self.status

  @property
  def speed_text(self):
    return "–" if self.stale else str(round(max(0, self.speed)))


def demo_state(seconds):
  """Deterministic fixture, never passed off as a real car/model state."""
  import math
  phase = seconds % 12
  status = "disengaged" if phase < 1 else "override" if 6 <= phase < 8 else "engaged"
  return DisplayState(55 + 2 * math.sin(seconds * 0.7), 65, status, age_seconds=1 if 9 <= phase < 11 else 0)
