"""Power requirements of the offroad cabin-camera preview."""

from pathlib import Path


def preview_core_ready():
  # dmonitoringmodeld pins itself to core 7. hardwared restores that core after
  # IsDriverViewEnabled is set; manager must wait before starting the model.
  try:
    return Path("/sys/devices/system/cpu/cpu7/online").read_text().strip() == "1"
  except OSError:
    return False


def should_power_save(ignition, screen_brightness, driver_view):
  return not ignition and screen_brightness < 1e-3 and not driver_view
