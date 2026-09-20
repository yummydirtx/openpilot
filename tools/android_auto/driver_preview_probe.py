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
from unittest.mock import patch


def main():
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument("--output", type=Path, required=True)
  parser.add_argument("--layout-fixtures", action="store_true", help="Save explicitly synthetic monitoring-overlay layout checks")
  args = parser.parse_args()
  args.output.mkdir(parents=True, exist_ok=False, mode=0o700)
  from tools.android_auto.native_identity import native_identity
  native_identity(args.output)
  os.environ["BIG"] = os.environ["SUNNYPILOT_UI"] = "1"
  from openpilot.cereal import messaging
  from openpilot.common.params import Params
  from openpilot.system.ui.lib.driver_preview import DriverPreviewHost, DriverPreviewRequest, preview_sound_publisher, monitoring_fresh, preview_offroad
  from openpilot.system.ui.lib.display_handoff import read_json, write_json
  from tools.android_auto.native_renderer import NativeRenderer
  from tools.android_auto.viewport import Viewport
  from openpilot.selfdrive.ui.layouts.main import MainState
  from openpilot.selfdrive.ui.layouts.settings.device import DeviceLayout

  params = Params()
  sm = messaging.SubMaster(["deviceState", "pandaStates"])
  end = time.monotonic() + 3
  while time.monotonic() < end:
    sm.update(100)
  if not preview_offroad(sm, time.monotonic()):
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
        offroad = preview_offroad(state, now)
        dm_fresh = monitoring_fresh(state, now, demo=host.owner is not None)
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
      button = panel._driver_camera_btn.action_item._button
      target = next((t for t in renderer.input.previous if t.widget is button), None)
      if target is None:
        raise RuntimeError(f"Preview target absent: mode={renderer.main._current_mode}, stack=" +
                           f"{[type(w).__name__ for w in renderer.app._nav_stack]}, panel_enabled={panel.enabled}, " +
                           f"button_enabled={button.enabled}, allowed={panel.driver_camera_allowed()}, " +
                           f"button_rect={(button.rect.x, button.rect.y, button.rect.width, button.rect.height)}, " +
                           f"targets={[type(t.widget).__name__ for t in renderer.input.previous]}")
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
      live_since = None
      while time.monotonic() < end:
        image = frame()
        if preview.camera_displayed and preview.monitoring_displayed:
          if live_since is None:
            live_since = time.monotonic()
          if time.monotonic() - live_since < 2:
            time.sleep(.04)
            continue  # Allow exposure/IR illumination to settle before visual QA.
          image.save(args.output / "preview.png")
          result["live_camera_and_monitoring"] = True
          camera = preview._camera_view.frame
          result["camera_source_size"] = [camera.width, camera.height]
          result["online_cpus_during_preview"] = Path("/sys/devices/system/cpu/online").read_text().strip()
          result["metadata"] = renderer.metadata(time.monotonic())
          break
        live_since = None
        time.sleep(.04)
      else:
        state = renderer.state.sm
        now = time.monotonic()
        details = {"enabled": params.get_bool("IsDriverViewEnabled"), "lease_owned": host.owner is not None,
                   "preview_active": preview.active, "camera_frame": preview._camera_view.frame is not None,
                   "camera_age": now - preview._camera_view.client.timestamp_eof / 1e9,
                   "request": read_json(root / "driver-preview.json").get("active"),
                   "services": {name: {"alive": state.alive[name], "valid": state.valid[name],
                                       "recv_age": now - state.recv_time[name], "source_age": now - state.logMonoTime[name] / 1e9}
                                for name in ("deviceState", "driverStateV2", "driverMonitoringState")},
                   "processes": {p.name: p.running for p in state["managerState"].processes
                                 if p.name in ("camerad", "dmonitoringmodeld", "dmonitoringd", "selfdrived")}}
        raise RuntimeError("Preview sources unavailable: " + json.dumps(details))

      if args.layout_fixtures:
        # Change only this offscreen renderer's subscribers. Never publish
        # synthetic monitoring data or pass it to the preview sound host.
        frame()
        state = renderer.state.sm
        dm = state["driverMonitoringState"].as_builder()
        ds = state["driverStateV2"].as_builder()
        dm.activePolicy, dm.alertLevel = "vision", "three"
        dm.visionPolicyState.faceDetected = True
        dm.visionPolicyState.awarenessPercent = 42
        dm.visionPolicyState.pose.pitch, dm.visionPolicyState.pose.yaw = .15, .3
        for data in (ds.leftDriverData, ds.rightDriverData):
          data.facePosition = [-.3, .15]
          data.faceOrientationStd = [.05, .05, .05]
          data.leftEyeProb, data.rightEyeProb, data.sunglassesProb = .9, .2, .7
        fixtures = {"driverMonitoringState": dm.as_reader(), "driverStateV2": ds.as_reader()}
        with patch.object(renderer.state, "update"), patch.dict(state.data, fixtures), \
             patch.dict(state.recv_time, {}), patch.dict(state.logMonoTime, {}):
          for _ in range(3):
            for name in fixtures:
              state.recv_time[name] = time.monotonic()
              state.logMonoTime[name] = int(state.recv_time[name] * 1e9)
            image = renderer.render()
          assert preview.monitoring_displayed, "Fixture render lost its live camera/data freshness"
          image.save(args.output / "synthetic-overlay-layout.png")
        result["synthetic_overlay_layout_saved"] = True
        frame()  # Resume real subscriptions before reset and cleanup checks.

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
