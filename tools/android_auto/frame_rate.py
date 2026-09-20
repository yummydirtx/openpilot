"""Select the highest sustainable projection cadence within the CPU budget."""


class FrameRate:
  def __init__(self, maximum):
    self.maximum = maximum
    self.current = maximum
    self.headroom_samples = 0

  def sample(self, cores):
    old = self.current
    rates = [rate for rate in (8, 10, 15, 20, 25, 30) if rate <= self.maximum]
    if cores > .85:
      target = self.current * .80 / cores
      self.current = max([rate for rate in rates if rate <= target], default=rates[0])
      self.headroom_samples = 0
    else:
      self.headroom_samples = self.headroom_samples + 1 if cores < .6 else 0
      higher = next((rate for rate in rates if rate > self.current), None)
      if self.headroom_samples >= 10 and higher and cores * higher / self.current < .8:
        self.current = higher
        self.headroom_samples = 0
    return self.current != old
