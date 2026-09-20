"""Bounded offroad check of the real projected Device-menu camera preview.

Uses private handoff mailboxes and the normal preview host, without USB. Starts
the cabin camera/monitoring through IsDriverViewEnabled and always releases it.
Images are private device diagnostics, never committed or uploaded automatically.
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
  os.environ["BIG"] = os.environ["SUNNYPILOT_UI"] = "1"
  from openpilot.cereal import messaging
  from openpilot.common.params import Params
  from openpilot.system.ui.lib.driver_preview import DriverPreviewHost, DriverPreviewRequest, preview_sound_publisher
  from openpilot.system.ui.lib.display_handoff import read_json, write_json
  from tools.android_auto.native_renderer import NativeRenderer
  from tools.android_auto.viewport import Viewport
  from openpilot.selfdrive.ui.layouts.main import MainState
  from openpilot.selfdrive.ui.layouts.settings.device import DeviceLayout

  params = Params()
  sm = messaging.SubMaster(["deviceState"])
  for _ in range(4):
    sm.update(1000)
  if not sm.alive["deviceState"] or not sm.valid["deviceState"] or sm["deviceState"].started:
    raise RuntimeError("Probe requires fresh offroad state")
  if params.get_bool("IsDriverViewEnabled"):
    raise RuntimeError("Close the existing driver preview before probing")

  renderer = NativeRenderer(Viewport(1280, 720, 0, 240))
  host = DriverPreviewHost(params, preview_sound_publisher)
  result = {}
  try:
    with TemporaryDirectory(prefix="automaxxing-preview-") as temporary:
      root = Path(temporary)
      token = "d" * 32
      renderer.driver_preview_request = DriverPreviewRequest(root, root)

      def frame(actions=()):
        write_json(root / "intent.json", {"at": time.monotonic(), "mode": "project", "token": token})
        image = renderer.render(actions)
        state = renderer.state.sm
        now = time.monotonic()
        offroad = (state.alive["deviceState"] and state.valid["deviceState"] and not state["deviceState"].started
                   and now - state.recv_time["deviceState"] < 1.5)
        dm_fresh = state.alive["driverMonitoringState"] and state.valid["driverMonitoringState"]
        host.update(read_json(root / "driver-preview.json"), token=token, offroad=offroad, projecting=True,
                    now=now, dm_state=state["driverMonitoringState"] if dm_fresh else None)
        if not offroad:
          raise RuntimeError("Offroad state lost during preview probe")
        return image

      for _ in range(20):
        frame()
        time.sleep(.03)
      settings = renderer.main._layouts[MainState.SETTINGS]
      panel_type, panel = next((key, info.instance) for key, info in settings._panels.items() if isinstance(info.instance, DeviceLayout))
      renderer.main.open_settings(panel_type)
      frame()
      button = panel._quiet_mode_and_dcam.action_item.right_button
      target = next(t for t in renderer.input.previous if t.widget is button)
      renderer.input.reveal(target)
      for _ in range(20):
        frame()
        time.sleep(.03)
      renderer.input.selected = target.key
      renderer.input.wake()
      frame([("select", 1)])
      preview = renderer._driver_preview()
      assert preview is not None, "Device-menu Commander click did not open preview"
      result["device_menu_knob_open"] = True
      baseline_callbacks = len(renderer.state._offroad_transition_callbacks) - 1
      frame().save(args.output / "starting.png")
      end = time.monotonic() + 30
      while time.monotonic() < end:
        image = frame()
        if preview.camera_displayed and preview.monitoring_displayed:
          image.save(args.output / "preview.png")
          result["live_camera_and_monitoring"] = True
          result["metadata"] = renderer.metadata(time.monotonic())
          break
        time.sleep(.04)
      else:
        raise RuntimeError("Cabin camera and monitoring did not become fresh within 30 seconds")

      target = next(t for t in renderer.input.previous if t.widget is preview._reset)
      renderer.input.selected = target.key
      renderer.input.wake()
      previous_reset = preview.reset_id
      frame([("select", 1)])
      frame()
      assert preview.reset_id != previous_reset and not params.get_bool("DriverTooDistracted")
      result["knob_reset"] = True

      host.update(read_json(root / "driver-preview.json"), token=token, offroad=True, projecting=True, now=time.monotonic() + 1)
      assert not params.get_bool("IsDriverViewEnabled") and host.publisher is None
      result["lease_expiry_cleanup"] = True
      frame([("back", 1)])
      assert renderer._driver_preview() is None and not params.get_bool("IsDriverViewEnabled")
      assert renderer.main._current_mode == MainState.SETTINGS
      assert len(renderer.state._offroad_transition_callbacks) == baseline_callbacks
      result["back_and_callback_cleanup"] = True

      for name in ("idle", "home", "music"):
        panel._show_driver_camera()
        frame()
        preview = renderer._driver_preview()
        assert preview is not None
        if name == "idle":
          preview.last_input_at -= 301
          frame()
        else:
          frame([(name, 1)])
        assert renderer._driver_preview() is None and not params.get_bool("IsDriverViewEnabled")
        assert len(renderer.state._offroad_transition_callbacks) == baseline_callbacks
        result[f"{name}_cleanup"] = True
  finally:
    renderer.close()
    host.close()
  result["camera_disabled_after_test"] = not params.get_bool("IsDriverViewEnabled")
  (args.output / "result.json").write_text(json.dumps(result, indent=2))
  print(json.dumps(result, indent=2))


if __name__ == "__main__":
  main()
