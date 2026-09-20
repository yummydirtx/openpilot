"""Render and tap the real four launcher in isolation, using private mailboxes.

No physical display/input, supervisor start, or Android Auto session is touched.
Run on a parked device under the same bounded service limits as other probes.
"""

import argparse
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import time


def main():
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument("--output", type=Path, required=True)
  args = parser.parse_args()
  args.output.mkdir(parents=True, exist_ok=False, mode=0o700)
  from tools.android_auto.native_identity import native_identity
  native_identity(args.output)
  os.environ["BIG"] = "0"
  os.environ["SUNNYPILOT_UI"] = "1"
  from tools.android_auto.native_egl import HeadlessContext
  context = HeadlessContext(536, 240)
  import pyray as rl
  from openpilot.system.ui.lib.application import gui_app, MouseEvent, MousePos
  from openpilot.system.ui.lib.display_handoff import HandoffClient, write_json, read_json
  from openpilot.selfdrive.ui.sunnypilot.mici.layouts.settings import AndroidAutoBigButton
  gui_app._width, gui_app._height, gui_app._scale = 536, 240, 1.
  gui_app._load_fonts()
  gui_app._patch_text_functions()
  gui_app._patch_scissor_mode()
  texture = rl.load_render_texture(536, 240)
  states = {}
  try:
    with TemporaryDirectory(prefix="automaxxing-launcher-") as temporary:
      root = Path(temporary)
      client = HandoffClient(root, root, starter=lambda: None)
      gui_app.display_handoff = client
      button = AndroidAutoBigButton()
      rect = rl.Rectangle(67, 30, 402, 180)

      def render(name=None, events=()):
        gui_app._mouse_events = list(events)
        rl.begin_texture_mode(texture)
        rl.clear_background(rl.BLACK)
        button.render(rect)
        rl.end_texture_mode()
        gui_app._mouse_events = []
        if name:
          image = rl.load_image_from_texture(texture.texture)
          try:
            rl.image_flip_vertical(image)
            assert rl.export_image(image, str(args.output / f"{name}.png"))
          finally:
            rl.unload_image(image)
          states[name] = button.get_value()

      def tap():
        # Press/release delivered together: no hold duration is required.
        pos = MousePos(268, 120)
        now = time.monotonic()
        render(events=[MouseEvent(pos, 0, True, False, True, now), MouseEvent(pos, 0, False, True, False, now)])

      def reply(phase, ready=False):
        now = time.monotonic()
        write_json(root / "status.json", {"at": now, "token": client.token, "phase": phase,
                                           "ready": ready, "ready_until": now + .3})
        client.tick()

      render("ready")
      tap()
      assert client.mode == "project" and button.get_value() == "starting..."
      render("starting")
      reply("connecting")
      render("connecting")
      reply("projecting", True)
      render("connected")
      assert button.get_value() == "connected" and button._rotate_icon_t is None
      reply("failed")
      render("failed")
      assert button.get_value() == "failed - retry"
      tap()
      client.tick()  # Previous token's failed reply must not cancel this tap.
      render("retry")
      assert client.mode == "project" and read_json(root / "request.json")["mode"] == "project"
      assert button.get_value() == "starting..." and button._rotate_icon_t is not None
      tap()
      assert client.mode == "local" and button.get_value() == "tap to start"
      (args.output / "result.json").write_text(json.dumps({"states": states, "short_tap": True,
                                                          "retry_after_failure": True, "cancel": True}, indent=2))
  finally:
    rl.unload_render_texture(texture)
    context.close()


if __name__ == "__main__":
  main()
