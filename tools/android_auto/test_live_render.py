"""Renderer/encoder integration checks; optional dependencies live in the cache venv."""

from dataclasses import replace
import importlib.util
from pathlib import Path
import time
import unittest
from unittest.mock import patch

from tools.android_auto.live_state import DisplayAlert, LiveDisplayState
from tools.android_auto.viewport import Viewport

ASSETS = Path(__file__).resolve().parents[2] / ".cache/automaxxing/ui-assets"
HAS_RENDER = all(importlib.util.find_spec(name) is not None for name in ("PIL", "pyray")) and (ASSETS / "fonts/Inter-Bold.ttf").exists()
HAS_ENCODE = importlib.util.find_spec("av") is not None and importlib.util.find_spec("PIL") is not None


@unittest.skipUnless(HAS_RENDER, "Prepare native assets and install the isolated renderer dependencies")
class TestLiveRenderer(unittest.TestCase):
  def setUp(self):
    from tools.android_auto.live_render import LiveRenderer
    self.renderer = LiveRenderer(Viewport(1280, 720, 0, 240), ASSETS)
    self.state = LiveDisplayState(speed=47, set_speed=65, status="engaged", started=True, age_seconds=0)

  def test_cpu_render_preserves_symmetric_black_margins(self):
    import pyray as rl
    with patch.object(rl, "init_window", side_effect=AssertionError("Must not create a window")):
      image = self.renderer.render(self.state)
    self.assertEqual((image.mode, image.size), ("RGB", (1280, 720)))
    self.assertIsNone(image.crop((0, 0, 1280, 120)).getbbox())
    self.assertIsNone(image.crop((0, 600, 1280, 720)).getbbox())
    self.assertEqual(image.getpixel((0, 120)), (22, 127, 64))

  def test_stale_state_clears_active_border_and_invalidate_cached_image(self):
    active = self.renderer.render(self.state)
    stale = self.renderer.render(replace(self.state, age_seconds=1))
    self.assertIsNot(active, stale)
    self.assertEqual(stale.getpixel((0, 120)), (18, 40, 57))
    self.assertNotEqual(active.crop((560, 140, 720, 260)).tobytes(), stale.crop((560, 140, 720, 260)).tobytes())

  def test_visual_cache_does_not_repaint_for_fresh_age_only(self):
    image = self.renderer.render(self.state)
    self.assertIs(image, self.renderer.render(replace(self.state, age_seconds=0.1)))
    self.assertIsNot(image, self.renderer.render(replace(self.state, speed=48)))

  def test_cached_text_preserves_subpixel_glyph_layout(self):
    from PIL import ImageChops, ImageDraw
    from tools.android_auto.live_render import Vector2
    backend = self.renderer.backend
    for text in ("47", "–", "km/h", "MAX", "Take control"):
      backend.begin()
      backend.image.paste((80, 90, 100), (0, 0, 1280, 720))
      reference = backend.image.copy()
      position = Vector2(100.5, 72.8)
      ImageDraw.Draw(reference, "RGBA").text(backend.viewport.to_video(position.x, position.y), text,
                                             font=backend.font("Bold", 100), fill=(255, 255, 255, 255), anchor="lt")
      backend.text("Bold", text, position, 100, 0, (255, 255, 255, 255))
      self.assertIsNone(ImageChops.difference(reference, backend.image).getbbox())

  def test_text_mask_cache_is_bounded_and_respects_native_opacity(self):
    from tools.android_auto.live_render import Vector2
    backend = self.renderer.backend
    backend.begin()
    for index in range(140):
      backend.text("Bold", str(index), Vector2(300, 300), 100, 0, (255, 255, 255, 200))
    self.assertEqual(len(backend._text_masks), 128)
    backend.begin()
    backend.text("Bold", "47", Vector2(300, 300), 100, 0, (255, 255, 255, 200))
    self.assertEqual(backend.image.getextrema(), ((0, 200), (0, 200), (0, 200)))

  def test_mads_colors_keep_partial_engagement_distinct(self):
    lateral = self.renderer.render(replace(self.state, status="lat_only"))
    longitudinal = self.renderer.render(replace(self.state, status="long_only"))
    self.assertEqual(lateral.getpixel((0, 120)), (0, 200, 200))
    self.assertEqual(longitudinal.getpixel((0, 120)), (150, 28, 168))

  def test_critical_alert_remains_visible_when_other_data_is_stale(self):
    alert = DisplayAlert("TAKE CONTROL IMMEDIATELY", "System Unresponsive", 3, 2)
    image = self.renderer.render(replace(self.state, age_seconds=1, alert=alert))
    self.assertEqual(image.getpixel((20, 570)), (190, 33, 47))  # Native critical color at alpha 241 over the background.
    self.assertIsNone(image.crop((0, 600, 1280, 720)).getbbox())

  def test_can_load_deployed_shared_painter_without_replacing_openpilot(self):
    from tools.android_auto.live_render import LiveRenderer
    path = Path(__file__).resolve().parents[2] / "openpilot/selfdrive/ui/onroad/hud_drawing.py"
    deployed = LiveRenderer(self.renderer.viewport, ASSETS, hud_path=path)
    self.assertEqual(deployed.render(self.state).tobytes(), self.renderer.render(self.state).tobytes())

  @unittest.skipUnless(importlib.util.find_spec("numpy"), "Road rendering also uses NumPy")
  def test_camera_pixels_update_even_when_hud_state_is_identical(self):
    from PIL import Image
    from tools.android_auto.road_state import CameraFrame, RoadSnapshot
    def road(color, frame_id):
      return RoadSnapshot(camera=CameraFrame(Image.new("RGB", (1344, 760), color), frame_id, 0, 0, 1344, 760, "narrow"),
                          camera_age_seconds=.01, captured_at=time.monotonic())
    first = self.renderer.render(self.state, road=road((100, 0, 0), 1))
    second = self.renderer.render(self.state, road=road((0, 0, 100), 2))
    self.assertIsNot(first, second)
    self.assertNotEqual(first.getpixel((640, 400)), second.getpixel((640, 400)))
    self.assertEqual(self.renderer.road_metadata["camera_frame_id"], 2)
    self.assertFalse(self.renderer.road_metadata["model_displayed"])
    fallback = self.renderer.render(self.state, road=RoadSnapshot())
    self.assertFalse(self.renderer.road_metadata["camera_displayed"])
    self.assertNotEqual(second.getpixel((640, 400)), fallback.getpixel((640, 400)))

  @unittest.skipUnless(importlib.util.find_spec("numpy"), "Road rendering also uses NumPy")
  def test_displayed_driver_state_contributes_to_source_age(self):
    from PIL import Image
    from tools.android_auto.road_state import CameraFrame, DriverState, RoadSnapshot
    road = RoadSnapshot(camera=CameraFrame(Image.new("RGB", (1344, 760)), 1, 0, 0, 1344, 760, "narrow"),
                        camera_age_seconds=.01, driver_age_seconds=.2, driver_state=DriverState(True, False, (0., 0., 0.)), captured_at=100.)
    with patch("tools.android_auto.live_render.time.monotonic", return_value=100.05):
      self.renderer.render(self.state, road)
    self.assertTrue(self.renderer.road_metadata["driver_displayed"])
    self.assertAlmostEqual(self.renderer.road_metadata["display_age_seconds"], .25)
    self.assertAlmostEqual(self.renderer.road_metadata["display_source_timestamp"], 99.8)

  @unittest.skipUnless(importlib.util.find_spec("numpy"), "Road rendering also uses NumPy")
  def test_expired_mode_flags_render_neutral_wheel(self):
    from tools.android_auto.road_state import RoadSnapshot
    first = self.renderer.render(self.state, RoadSnapshot(experimental_mode=True, engageable=True))
    self.assertFalse(self.renderer.road_metadata["wheel_state_displayed"])
    self.assertIsNone(self.renderer.road_metadata["display_source_timestamp"])
    second = self.renderer.render(self.state, RoadSnapshot(experimental_mode=False, engageable=False))
    self.assertEqual(first.tobytes(), second.tobytes())

  @unittest.skipUnless(importlib.util.find_spec("numpy"), "Road rendering also uses NumPy")
  def test_wheel_decision_age_is_bounded_even_when_vehicle_hud_is_unavailable(self):
    from tools.android_auto.road_state import RoadSnapshot
    road = RoadSnapshot(experimental_mode=True, engageable=True, captured_at=100, selfdrive_age_seconds=.2)
    with patch("tools.android_auto.live_render.time.monotonic", return_value=100.05):
      self.renderer.render(replace(self.state, age_seconds=1), road)
    self.assertTrue(self.renderer.road_metadata["wheel_state_displayed"])
    self.assertAlmostEqual(self.renderer.road_metadata["display_age_seconds"], .25)
    self.assertAlmostEqual(self.renderer.road_metadata["display_source_timestamp"], 99.8)


@unittest.skipUnless(HAS_ENCODE, "Install isolated PyAV/Pillow to exercise the real H.264 codec")
class TestLiveEncoder(unittest.TestCase):
  def test_each_call_returns_a_decodable_access_unit_and_forced_idr(self):
    import av
    from PIL import Image
    from tools.android_auto.live_encode import H264Encoder
    from tools.android_auto.video import access_units, nal_units
    encoder = H264Encoder(800, 480)
    units = []
    try:
      for index in range(6):
        units.append(encoder.encode(Image.new("RGB", (800, 480), (index * 30, 50, 100)), force_keyframe=index == 3))
    finally:
      encoder.close()
    self.assertEqual(access_units(b"".join(units)), units)
    self.assertTrue({5, 7, 8}.issubset({kind for kind, _ in nal_units(units[3])}))
    decoder = av.CodecContext.create("h264", "r")
    decoded = [frame for unit in units for frame in decoder.decode(av.Packet(unit))]
    decoded.extend(decoder.decode(None))
    self.assertEqual(len(decoded), 6)
    self.assertTrue(all((frame.width, frame.height) == (800, 480) for frame in decoded))
    # Inspect decoded pixels so a syntactically valid empty/constant stream fails.
    for index, frame in enumerate(decoded):
      actual = frame.to_image().getpixel((400, 240))
      self.assertTrue(all(abs(a - b) < 8 for a, b in zip(actual, (index * 30, 50, 100), strict=True)))

  def test_wrong_image_is_rejected_before_encode(self):
    from PIL import Image
    from tools.android_auto.live_encode import H264Encoder
    encoder = H264Encoder(800, 480)
    try:
      for image in (Image.new("RGB", (400, 240)), Image.new("RGBA", (800, 480))):
        with self.assertRaises(ValueError):
          encoder.encode(image)
    finally:
      encoder.close()
    with self.assertRaises(RuntimeError):
      encoder.encode(Image.new("RGB", (800, 480)))

  def test_gpu_rgba_readback_encodes_with_correct_orientation_and_colors(self):
    import av
    from PIL import Image
    from tools.android_auto.live_encode import H264Encoder
    image = Image.new("RGBA", (800, 480), (15, 35, 160, 255))
    image.paste((180, 30, 20, 255), (0, 0, 800, 240))
    encoder = H264Encoder(800, 480)
    try:
      packet = encoder.encode_rgba(image.tobytes(), force_keyframe=True)
      with self.assertRaises(ValueError):
        encoder.encode_rgba(b"short")
    finally:
      encoder.close()
    decoder = av.CodecContext.create("h264", "r")
    decoded = decoder.decode(av.Packet(packet))[0].to_image()
    for point in ((400, 100), (400, 380)):
      self.assertTrue(all(abs(a-b) < 8 for a, b in zip(decoded.getpixel(point), image.getpixel(point)[:3], strict=True)))


if __name__ == "__main__":
  unittest.main()
