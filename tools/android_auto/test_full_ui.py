"""Camera/model/HUD integration and visible-source freshness regressions."""

from dataclasses import replace
import importlib.util
from pathlib import Path
import pickle
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from tools.android_auto.frame_worker import MAX_CONTROL_BYTES, frame_metadata, send_control
from tools.android_auto.live_state import LiveDisplayState
from tools.android_auto.road_state import CameraFrame, CameraGeometry, DriverState, Lead, ModelSnapshot, RoadSnapshot, RoadStateReader
from tools.android_auto.viewport import Viewport

ASSETS = Path(__file__).resolve().parents[2] / ".cache/automaxxing/ui-assets"
HAS_RENDER = all(importlib.util.find_spec(name) is not None for name in ("PIL", "numpy", "pyray")) and (ASSETS / "fonts/Inter-Bold.ttf").exists()
HAS_ENCODE = importlib.util.find_spec("av") is not None


def live_state(**changes):
  return replace(LiveDisplayState(speed=47, set_speed=65, status="engaged", started=True, age_seconds=0.04), **changes)


def road_snapshot():
  from PIL import Image
  positions = tuple((float(x), 0.0, 0.0) for x in (5, 10, 20, 30, 50, 80))
  lanes = tuple(tuple((x, y, 1.22) for x, _, _ in positions) for y in (-3.5, -1.75, 1.75, 3.5))
  edges = tuple(tuple((x, y, 1.22) for x, _, _ in positions) for y in (-5, 5))
  camera = CameraFrame(Image.new("RGB", (1344, 760), (70, 90, 110)), 100, 99.94, 99.95, 1344, 760, "narrow")
  geometry = CameraGeometry(1344, 760, ((1141.5, 0., 672.), (0., 1141.5, 380.), (0., 0., 1.)), height_m=1.22, calibrated=True)
  model = ModelSnapshot(positions, lanes, edges, (0.9,) * 4, (0.1,) * 2, (0.,) * len(positions), 99, 99, 99.9)
  return RoadSnapshot(camera=camera, geometry=geometry, model=model, leads=(Lead(30, 0, -2, True),),
                      driver_state=DriverState(True, False, (0., 0., 0.)), longitudinal_control=True,
                      allow_throttle=True, engageable=True, camera_age_seconds=0.05, model_age_seconds=0.1,
                      radar_age_seconds=0.2, driver_age_seconds=0.3, captured_at=100.)


class TestDisplayedSourceMetadata(unittest.TestCase):
  def test_live_road_still_has_expiry_when_hud_is_unavailable(self):
    metadata = frame_metadata(live_state(age_seconds=1), 100, 0.03, poll_seconds=0.02,
                              road={"camera_displayed": True, "model_displayed": False, "display_age_seconds": 0.1})
    self.assertTrue(metadata["stale"])
    self.assertIsNone(metadata["speed"])
    self.assertAlmostEqual(metadata["display_source_age_seconds"], 0.12)

  def test_hidden_road_sources_do_not_expire_a_fresh_hud(self):
    metadata = frame_metadata(live_state(), 100, 0.03, poll_seconds=0.02,
                              road={"camera_displayed": False, "model_displayed": False, "display_age_seconds": None})
    self.assertAlmostEqual(metadata["display_source_age_seconds"], 0.06)

  def test_oldest_actually_displayed_source_controls_expiry(self):
    metadata = frame_metadata(live_state(), 100, 0.03, poll_seconds=0.02,
                              road={"camera_displayed": True, "model_displayed": True, "display_age_seconds": 0.3})
    self.assertAlmostEqual(metadata["display_source_age_seconds"], 0.32)


@unittest.skipUnless(HAS_RENDER, "Prepare the isolated renderer dependencies and native assets")
class TestFullComposition(unittest.TestCase):
  def setUp(self):
    from tools.android_auto.live_render import LiveRenderer
    self.renderer = LiveRenderer(Viewport(1280, 720, 0, 240), ASSETS)
    self.road = road_snapshot()
    clock = patch("time.monotonic", return_value=100.)
    clock.start()
    self.addCleanup(clock.stop)

  def test_fresh_full_ui_composes_camera_model_driver_and_live_hud(self):
    image = self.renderer.render(live_state(), self.road)
    metadata = self.renderer.road_metadata
    self.assertTrue(metadata["camera_displayed"])
    self.assertTrue(metadata["model_displayed"])
    self.assertTrue(metadata["driver_displayed"])
    self.assertEqual(metadata["camera_frame_id"], 100)
    self.assertEqual(metadata["model_frame_id"], 99)
    self.assertEqual(image.getpixel((0, 120)), (22, 127, 64))
    self.assertIsNone(image.crop((0, 0, 1280, 120)).getbbox())
    self.assertIsNone(image.crop((0, 600, 1280, 720)).getbbox())

  def test_missing_model_preserves_live_camera_and_hud(self):
    full = self.renderer.render(live_state(), self.road)
    camera_only = self.renderer.render(live_state(), replace(self.road, model=None, leads=(), model_age_seconds=float("inf")))
    self.assertTrue(self.renderer.road_metadata["camera_displayed"])
    self.assertFalse(self.renderer.road_metadata["model_displayed"])
    self.assertEqual(camera_only.getpixel((0, 120)), (22, 127, 64))
    self.assertEqual(full.getpixel((80, 400)), camera_only.getpixel((80, 400)))
    self.assertNotEqual(full.crop((500, 360, 780, 520)).tobytes(), camera_only.crop((500, 360, 780, 520)).tobytes())

  def test_missing_camera_preserves_fresh_hud_and_removes_road_pixels(self):
    full = self.renderer.render(live_state(), self.road)
    missing = replace(self.road, camera=None, geometry=None, model=None, leads=(), driver_state=None)
    hud = self.renderer.render(live_state(), missing)
    self.assertFalse(self.renderer.road_metadata["camera_displayed"])
    self.assertFalse(self.renderer.road_metadata["model_displayed"])
    self.assertEqual(hud.getpixel((0, 120)), (22, 127, 64))
    self.assertNotEqual(full.getpixel((80, 400)), hud.getpixel((80, 400)))
    # The white speed glyphs remain identical even though their background changes.
    masks = [image.crop((560, 150, 720, 250)).convert("L").point(lambda value: 255 if value == 255 else 0) for image in (full, hud)]
    self.assertIsNotNone(masks[0].getbbox())
    self.assertEqual(masks[0].tobytes(), masks[1].tobytes())

  def test_uncalibrated_snapshot_never_draws_model_even_if_a_caller_supplies_one(self):
    road = replace(self.road, geometry=replace(self.road.geometry, calibrated=False))
    self.renderer.render(live_state(), road)
    self.assertTrue(self.renderer.road_metadata["camera_displayed"])
    self.assertFalse(self.renderer.road_metadata["model_displayed"])

  def test_unavailable_vehicle_state_keeps_camera_but_clears_active_hud_and_model(self):
    image = self.renderer.render(live_state(age_seconds=1), self.road)
    self.assertTrue(self.renderer.road_metadata["camera_displayed"])
    self.assertFalse(self.renderer.road_metadata["model_displayed"])
    self.assertEqual(image.getpixel((0, 120)), (18, 40, 57))
    metadata = frame_metadata(live_state(age_seconds=1), 100, 0.03, road=self.renderer.road_metadata)
    self.assertIsNotNone(metadata["display_source_age_seconds"])

  def test_cached_road_snapshot_expires_without_removing_fresh_vehicle_hud(self):
    with patch("time.monotonic", return_value=100.6):
      image = self.renderer.render(live_state(), self.road)
    self.assertFalse(self.renderer.road_metadata["camera_displayed"])
    self.assertFalse(self.renderer.road_metadata["model_displayed"])
    self.assertEqual(image.getpixel((0, 120)), (22, 127, 64))

  def test_driver_and_lead_age_are_included_only_when_displayed(self):
    road = replace(self.road, driver_state=None, radar_age_seconds=0.3, model_age_seconds=0.1)
    with patch("time.monotonic", return_value=100.02):
      self.renderer.render(live_state(), road)
    self.assertGreaterEqual(self.renderer.road_metadata["display_age_seconds"], 0.32 - 1e-9)
    road = replace(self.road, leads=(), driver_age_seconds=0.3, model_age_seconds=0.1)
    with patch("time.monotonic", return_value=100.02):
      self.renderer.render(live_state(), road)
    self.assertGreaterEqual(self.renderer.road_metadata["display_age_seconds"], 0.32 - 1e-9)

  def test_full_ui_metadata_fits_the_bounded_worker_control_packet(self):
    self.renderer.render(live_state(), self.road)
    metadata = frame_metadata(live_state(), 100, 0.03, road=self.renderer.road_metadata)
    packet = ("frame", 50_000, metadata)
    self.assertLessEqual(len(pickle.dumps(packet, protocol=4)), MAX_CONTROL_BYTES)
    connection = Mock()
    send_control(connection, packet)
    self.assertEqual(pickle.loads(connection.send_bytes.call_args.args[0]), packet)

  @unittest.skipUnless(HAS_ENCODE, "Install isolated PyAV for full-frame encode/decode")
  def test_full_road_frame_survives_real_h264_encode_decode(self):
    import av
    from tools.android_auto.live_encode import H264Encoder
    image = self.renderer.render(live_state(), self.road)
    encoder = H264Encoder(1280, 720)
    try:
      encoded = encoder.encode(image, force_keyframe=True)
    finally:
      encoder.close()
    decoder = av.CodecContext.create("h264", "r")
    frames = decoder.decode(av.Packet(encoded)) + decoder.decode(None)
    self.assertEqual(len(frames), 1)
    decoded = frames[0].to_image()
    self.assertEqual(decoded.size, (1280, 720))
    expected, actual = image.getpixel((80, 400)), decoded.getpixel((80, 400))
    self.assertTrue(all(abs(a - b) < 8 for a, b in zip(expected, actual, strict=True)))
    self.assertTrue(all(high <= 5 for _, high in decoded.crop((0, 0, 1280, 110)).getextrema()))


@unittest.skipUnless(HAS_RENDER, "Prepare the isolated renderer dependencies and native assets")
class TestRoadReaderComposition(unittest.TestCase):
  def setUp(self):
    from PIL import Image
    from tools.android_auto.live_render import LiveRenderer
    self.renderer = LiveRenderer(Viewport(1280, 720, 0, 240), ASSETS)
    road = road_snapshot()

    def points(line):
      return SimpleNamespace(**{axis: [point[index] for point in line] for index, axis in enumerate(("x", "y", "z"))})

    messages = {
      "deviceState": SimpleNamespace(started=True, deviceType="mici"),
      "narrowRoadCameraState": SimpleNamespace(sensor="os04c10", frameId=100, timestampEof=int(99.95e9)),
      "wideRoadCameraState": SimpleNamespace(sensor="os04c10", frameId=100, timestampEof=int(99.95e9)),
      "extrinsicsCalibration": SimpleNamespace(calStatus="calibrated", rpyCalib=[0, 0, 0], wideFromDeviceEuler=[0, 0, 0], height=[1.22]),
      "modelV2": SimpleNamespace(position=points(road.model.position), laneLines=[points(line) for line in road.model.lane_lines],
                                 roadEdges=[points(line) for line in road.model.road_edges], laneLineProbs=road.model.lane_probs,
                                 roadEdgeStds=road.model.edge_stds, acceleration=SimpleNamespace(x=road.model.acceleration_x),
                                 frameId=99, frameIdExtra=99, timestampEof=int(99.9e9)),
      "radarState": SimpleNamespace(leadOne=SimpleNamespace(present=True, dRel=30, yRel=0, vRel=-2), leadTwo=SimpleNamespace(present=False)),
      "selfdriveState": SimpleNamespace(experimentalMode=False, engageable=True),
      "carState": SimpleNamespace(vEgo=20), "carParams": SimpleNamespace(openpilotLongitudinalControl=True),
      "longitudinalPlan": SimpleNamespace(allowThrottle=True),
      "driverMonitoringState": SimpleNamespace(activePolicy="vision", isRHD=False),
      "driverStateV2": SimpleNamespace(leftDriverData=SimpleNamespace(faceOrientation=[0, 0, 0])),
    }

    class Subscriber(dict):
      def update(self, timeout):
        pass

    self.sm = Subscriber(messages)
    for name in ("seen", "alive", "valid"):
      setattr(self.sm, name, dict.fromkeys(messages, True))
    self.sm.recv_time = dict.fromkeys(messages, 99.97)
    self.sm.logMonoTime = dict.fromkeys(messages, int(99.97e9))
    buffer = SimpleNamespace(width=1344, height=760, stride=1344, uv_offset=1344 * 760, frame_id=100,
                             data=b"\x80" * (1344 * 760 * 3 // 2))
    self.client = Mock(frame_id=100, timestamp_sof=int(99.94e9), timestamp_eof=int(99.95e9))
    self.client.is_connected.return_value = self.client.connect.return_value = True
    self.client.recv.return_value = buffer
    camera = SimpleNamespace(width=1344, height=760, intrinsics=road.geometry.intrinsics)
    self.reader = RoadStateReader(sm=self.sm, params=Mock(get=Mock(return_value=0)), client_factory=lambda stream: self.client,
                                  camera_configs={("mici", "os04c10"): SimpleNamespace(narrow_road=camera, wide_road=camera)},
                                  converter=lambda data, width, height: Image.new("RGB", (width, height), (70, 90, 110)))
    self.addCleanup(self.reader.close)
    clock = patch("time.monotonic", return_value=100.)
    clock.start()
    self.addCleanup(clock.stop)

  def test_mismatched_model_frame_is_hidden_without_losing_camera_or_vehicle_hud(self):
    self.sm["modelV2"].frameId = 90
    snapshot = self.reader.poll()
    self.assertIsNotNone(snapshot.camera)
    self.assertIsNone(snapshot.model)
    image = self.renderer.render(live_state(), snapshot)
    self.assertTrue(self.renderer.road_metadata["camera_displayed"])
    self.assertFalse(self.renderer.road_metadata["model_displayed"])
    self.assertEqual(image.getpixel((0, 120)), (22, 127, 64))

  def test_unknown_intrinsics_never_fall_back_to_another_device_camera(self):
    self.sm["narrowRoadCameraState"].sensor = "unknown-new-sensor"
    snapshot = self.reader.poll()
    self.assertIsNone(snapshot.geometry)
    self.assertIsNone(snapshot.model)
    image = self.renderer.render(live_state(), snapshot)
    self.assertFalse(self.renderer.road_metadata["model_displayed"])
    self.assertEqual(image.getpixel((0, 120)), (22, 127, 64))

  def test_invalid_radar_geometry_drops_lead_without_losing_model_or_hud(self):
    self.sm["radarState"].leadOne.dRel = float("nan")
    snapshot = self.reader.poll()
    self.assertEqual(snapshot.leads, ())
    self.assertIsNotNone(snapshot.model)
    image = self.renderer.render(live_state(), snapshot)
    self.assertTrue(self.renderer.road_metadata["model_displayed"])
    self.assertEqual(image.getpixel((0, 120)), (22, 127, 64))

  def test_camera_ipc_error_degrades_to_hud_without_throwing(self):
    self.client.recv.side_effect = OSError("Camera restarted")
    snapshot = self.reader.poll()
    self.assertIsNone(snapshot.camera)
    image = self.renderer.render(live_state(), snapshot)
    self.assertFalse(self.renderer.road_metadata["camera_displayed"])
    self.assertEqual(image.getpixel((0, 120)), (22, 127, 64))


if __name__ == "__main__":
  unittest.main()
