"""Bounded on-device native-menu navigation probe; never changes a setting."""

import argparse
import json
import os
from pathlib import Path
import time


def main():
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument("--output", type=Path, required=True)
  args = parser.parse_args()
  args.output.mkdir(parents=True, exist_ok=False, mode=0o700)
  from tools.android_auto.native_identity import native_identity
  native_identity(args.output)
  os.environ["BIG"] = "1"
  from tools.android_auto.native_renderer import NativeRenderer
  from tools.android_auto.viewport import Viewport
  from openpilot.selfdrive.ui.layouts.main import MainState
  renderer = NativeRenderer(Viewport(1280, 720, 0, 240))
  visited = []
  try:
    for _ in range(25):
      image = renderer.render()
      time.sleep(.03)
    image.save(args.output / "road.png")
    image = renderer.render([("select", 1)])
    if renderer.main._current_mode != MainState.SETTINGS:
      raise RuntimeError("Probe requires started/parked car for road-to-settings navigation")
    settings = renderer.main._layouts[MainState.SETTINGS]
    assert settings._current_panel == 99
    image.save(args.output / "android-auto-settings.png")
    # Exercise every original settings category, including those that require
    # scrolling. Activate ONLY category navigation, never a parameter/button.
    for panel in settings._panels:
      targets = renderer.input.previous
      target_index = next(i for i, t in enumerate(targets) if getattr(t.widget, "panel_type", None) == panel)
      current = next(i for i, t in enumerate(targets) if t.key == renderer.input.selected)
      delta = target_index - current
      while delta:
        step = min(20, max(-20, delta))
        renderer.render([("rotate", step)])
        delta -= step
      image = renderer.render([("select", 1)])
      assert settings._current_panel == panel, (panel, settings._current_panel)
      visited.append(settings._panels[panel].name)
      if settings._panels[panel].name == "Toggles":
        image.save(args.output / "toggles.png")
    renderer.render([("back", 1)])
    assert renderer.main._current_mode == MainState.ONROAD
    renderer.render([("back", 1)])
    assert renderer.command == "exit"
    (args.output / "result.json").write_text(json.dumps({"visited_categories": visited, "back_to_road": True, "back_to_oem": True}, indent=2))
  finally:
    renderer.close()


if __name__ == "__main__":
  main()
