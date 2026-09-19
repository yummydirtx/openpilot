"""Projection parity against native source and pixel checks for road composition."""

import ast
from dataclasses import replace
import importlib.util
from pathlib import Path
from types import SimpleNamespace
import time
import unittest
from unittest.mock import patch

HAS_RENDER = all(importlib.util.find_spec(name) is not None for name in ("numpy", "PIL"))
REPO = Path(__file__).resolve().parents[2]
if HAS_RENDER:
  import numpy as np
  from PIL import Image, ImageDraw
  from tools.android_auto.live_state import LiveDisplayState
  from tools.android_auto.road_render import FACE_KEYPOINTS, RoadRenderer, VIEW_FROM_DEVICE, camera_crop, lead_polygons, line_polygon, native_transform
  from tools.android_auto.road_state import CameraFrame, CameraGeometry, Lead, ModelSnapshot, RoadSnapshot
  from tools.android_auto.viewport import Viewport


def native_methods(path, class_name, names, namespace):
  """Execute only pure methods under test, never the native UI module/imports."""
  tree = ast.parse((REPO / path).read_text())
  native_class = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == class_name)
  methods = [node for node in native_class.body if isinstance(node, ast.FunctionDef) and node.name in names]
  cls = ast.ClassDef(name="Reference", bases=[], keywords=[], body=methods, decorator_list=[])
  module = ast.fix_missing_locations(ast.Module(body=[cls], type_ignores=[]))
  exec(compile(module, str(path), "exec"), namespace)
  return namespace["Reference"]


@unittest.skipUnless(HAS_RENDER, "NumPy and Pillow are optional renderer dependencies")
class TestRoadProjection(unittest.TestCase):
  def setUp(self):
    self.geometry = CameraGeometry(1344, 760, ((1141.5, 0, 672), (0, 1141.5, 380), (0, 0, 1)), height_m=1.22, calibrated=True)
    self.viewport = Viewport(1280, 720, 0, 240)
    self.rect = (30, 30, self.viewport.logical_width - 60, 1020)

  def test_camera_and_model_transform_match_native_across_sensors_and_viewports(self):
    reference = native_methods("openpilot/selfdrive/ui/onroad/augmented_road_view.py", "AugmentedRoadView", {"_calc_frame_matrix"},
                               {"np": np, "rl": SimpleNamespace(Rectangle=object), "ui_state": SimpleNamespace(sm=SimpleNamespace(
                                recv_frame={"extrinsicsCalibration": 1})), "WIDE_CAM": "wide", "INF_POINT": np.array([1000., 0., 0.])})
    cases = 0
    for width, height, focal in ((1344, 760, 1141.5), (1928, 1208, 2648.)):
      for logical_width in (1800., 2160., 2880.):
        for wide in (False, True):
          for pitch in (-0.06, 0, 0.08):
            rotation = np.array([[np.cos(pitch), 0, np.sin(pitch)], [0, 1, 0], [-np.sin(pitch), 0, np.cos(pitch)]])
            intrinsics = np.array([[focal / (2.684 if wide else 1), 0, width / 2], [0, focal / (2.684 if wide else 1), height / 2], [0, 0, 1.]])
            geometry = CameraGeometry(width, height, tuple(map(tuple, intrinsics)), tuple(map(tuple, VIEW_FROM_DEVICE @ rotation)), calibrated=True)
            rect = (30., 30., logical_width - 60, 1020.)
            obj = reference()
            obj._content_rect = SimpleNamespace(x=rect[0], y=rect[1], width=rect[2], height=rect[3])
            obj._matrix_cache_key, obj._cached_matrix = None, None
            obj.stream_type = "wide" if wide else "narrow"
            obj.device_camera = SimpleNamespace(narrow_road=SimpleNamespace(intrinsics=intrinsics), wide_road=SimpleNamespace(intrinsics=intrinsics))
            obj.view_from_calib = obj.view_from_wide_calib = np.asarray(geometry.view_from_calib)
            transforms = []
            obj.model_renderer = SimpleNamespace(set_transform=transforms.append)
            obj._calc_frame_matrix(obj._content_rect)
            video, model = native_transform(geometry, rect, wide=wide)
            np.testing.assert_allclose(model, transforms[0], atol=1e-8)
            # The camera image and model projection use the same crop/scale.
            np.testing.assert_allclose(video @ intrinsics @ np.asarray(geometry.view_from_calib), model)
            cases += 1
    self.assertEqual(cases, 36)

  def test_ribbons_and_leads_match_native_on_valid_forward_geometry(self):
    reference = native_methods("openpilot/selfdrive/ui/onroad/model_renderer.py", "ModelRenderer",
                               {"_map_line_to_polygon", "_update_lead_vehicle"}, {"np": np, "LeadVehicle": SimpleNamespace})
    obj = reference()
    _, transform = native_transform(self.geometry, self.rect)
    obj._car_space_transform = transform
    obj._clip_region = SimpleNamespace(x=-470, y=-470, width=self.viewport.logical_width + 940, height=2020)
    x = np.linspace(0, 100, 33)
    path = np.column_stack((x, 0.002 * x ** 2, -0.0004 * x ** 2)).astype(np.float32)
    for width, z_offset, inversion in ((0.9, 1.22, False), (0.4, 1.22, False), (0.025, 0., True)):
      for index, distance in ((10, 33.), (25, 80.), (32, 100.)):
        expected = obj._map_line_to_polygon(path, width, z_offset, index, distance, inversion)
        actual = line_polygon(path, transform, (-470, -470, self.viewport.logical_width + 940, 2020), width, z_offset, index, distance,
                              allow_invert=inversion)
        np.testing.assert_allclose(actual, expected, atol=1e-3)
    lead = Lead(30, -0.5, -3, True)
    index = np.flatnonzero(path[:, 0] <= lead.d_rel)[-1]
    point = transform @ [lead.d_rel, -lead.y_rel, path[index, 2] + 1.22]
    expected = obj._update_lead_vehicle(lead.d_rel, lead.v_rel, point[:2] / point[2], SimpleNamespace(width=self.rect[2], height=self.rect[3]))
    glow, chevron, alpha = lead_polygons(lead, path, transform, self.rect, 1.22, 0)
    np.testing.assert_allclose(glow, expected.glow)
    np.testing.assert_allclose(chevron, expected.chevron)
    self.assertEqual(alpha, expected.fill_alpha)

  def test_driver_landmarks_are_exact_native_coordinates(self):
    tree = ast.parse((REPO / "openpilot/selfdrive/ui/onroad/driver_state.py").read_text())
    assignment = next(node for node in tree.body if isinstance(node, ast.Assign)
                      and any(isinstance(target, ast.Name) and target.id == "DEFAULT_FACE_KPTS_3D" for target in node.targets))
    scope = {"np": np}
    exec(compile(ast.Module(body=[assignment], type_ignores=[]), "native-face", "exec"), scope)
    np.testing.assert_array_equal(FACE_KEYPOINTS, scope["DEFAULT_FACE_KPTS_3D"])

  def test_reject_unknown_geometry_and_points_behind_camera(self):
    with self.assertRaises(ValueError):
      native_transform(replace(self.geometry, width=1928), self.rect)
    points = np.array([[2., 0, 0], [4, 0, 0], [6, 0, 0]])
    transform = np.array([[0., 1, 0], [0, 0, 1], [-1, 0, 0]])
    self.assertEqual(len(line_polygon(points, transform, (0, 0, 100, 100), .9, 1.22, 2, 6)), 0)

  def test_separable_camera_crop_retains_native_affine_pixel_centers(self):
    source = Image.new("RGB", (1344, 760))
    markers = ((200, 300), (650, 450), (1100, 550))
    for x, y in markers:
      ImageDraw.Draw(source).rectangle((x - 4, y - 4, x + 4, y + 4), fill=(255, 255, 255))
    video, _ = native_transform(self.geometry, self.rect)
    scale = self.viewport.scale
    ox, oy = self.viewport.offset
    zoom, tx, ty = video[0, 0] * scale, video[0, 2] * scale + ox, video[1, 2] * scale + oy
    x0, y0, x1, y1 = RoadRenderer(self.viewport).box
    size = (x1 - x0, y1 - y0)
    reference = source.transform(size, Image.Transform.AFFINE, (1 / zoom, 0, (x0 - tx) / zoom, 0, 1 / zoom, (y0 - ty) / zoom),
                                  Image.Resampling.BILINEAR)
    actual = camera_crop(source, size, zoom, tx, ty, x0, y0)
    for x, y in markers:
      center_x, center_y = round(x * zoom + tx - x0), round(y * zoom + ty - y0)
      box = (center_x - 8, center_y - 8, center_x + 9, center_y + 9)
      centers = []
      for image in (reference, actual):
        pixels = np.asarray(image.crop(box))[:, :, 0].astype(float)
        rows, columns = np.indices(pixels.shape)
        centers.append((float((columns * pixels).sum() / pixels.sum()), float((rows * pixels).sum() / pixels.sum())))
      # Resize uses a separable bilinear downsampling filter; centroids, and
      # therefore alignment, agree despite small antialiasing differences.
      np.testing.assert_allclose(centers[0], centers[1], atol=.15)


@unittest.skipUnless(HAS_RENDER, "NumPy and Pillow are optional renderer dependencies")
class TestRoadPixels(unittest.TestCase):
  setUp = TestRoadProjection.setUp

  def snapshot(self, *, color=(50, 50, 50)):
    camera = CameraFrame(Image.new("RGB", (1344, 760), color), 10, 100, 100, 1344, 760, "narrow")
    def line(y, z=0):
      return tuple((x, y, z) for x in range(0, 101, 3))
    model = ModelSnapshot(line(0), tuple(line(y, 1.22) for y in (-5.4, -1.8, 1.8, 5.4)),
                          tuple(line(y, 1.22) for y in (-6, 6)), (.1, .9, .9, .1), (.1, .1), (0.,) * 34, 10, 10, 100)
    return RoadSnapshot(camera=camera, geometry=self.geometry, model=model, camera_age_seconds=.01,
                        model_age_seconds=.02, captured_at=time.monotonic(), allow_throttle=True,
                        selfdrive_age_seconds=.02, longitudinal_plan_age_seconds=.02)

  def paint(self, road, *, stale=False):
    renderer = RoadRenderer(self.viewport)
    image = Image.new("RGB", (1280, 720))
    state = LiveDisplayState(speed=45, set_speed=65, status="engaged", started=True, age_seconds=1 if stale else .01)
    return image, renderer.paint(image, road, state)

  def test_camera_landmark_uses_same_transform_as_model(self):
    road = self.snapshot(color=(0, 0, 0))
    ImageDraw.Draw(road.camera.rgb).rectangle((644, 444, 656, 456), fill=(0, 255, 0))
    image, metadata = self.paint(road, stale=True)
    video, _ = native_transform(self.geometry, self.rect)
    location = video @ [650, 450, 1]
    px, py = map(round, self.viewport.to_video(*location[:2]))
    self.assertTrue(metadata["camera_displayed"])
    self.assertFalse(metadata["model_displayed"])
    self.assertGreater(image.getpixel((px, py))[1], 240)

  def test_polygons_and_camera_stay_inside_native_content_scissor(self):
    image, metadata = self.paint(self.snapshot())
    self.assertTrue(metadata["model_displayed"])
    x0, y0, x1, y1 = RoadRenderer(self.viewport).box
    for box in ((0, 0, 1280, y0), (0, y1, 1280, 720), (0, 0, x0, 720), (x1, 0, 1280, 720)):
      self.assertIsNone(image.crop(box).getbbox())
    red, green, blue = image.getpixel((640, 570))
    self.assertGreater(green, red + 25)
    self.assertGreater(green, blue + 25)

  def test_uncalibrated_camera_never_renders_supplied_model(self):
    road = self.snapshot()
    _, metadata = self.paint(replace(road, geometry=replace(road.geometry, calibrated=False)))
    self.assertTrue(metadata["camera_displayed"])
    self.assertFalse(metadata["model_displayed"])
    self.assertEqual(metadata["reason"], "Calibration unavailable")

  def test_expired_snapshot_drops_camera_before_painting(self):
    image, metadata = self.paint(replace(self.snapshot(), captured_at=time.monotonic() - 1))
    self.assertFalse(metadata["camera_displayed"])
    self.assertIsNone(metadata["display_source_timestamp"])
    self.assertIsNone(image.getbbox())

  def test_expired_model_keeps_camera_and_reports_model_unavailable(self):
    _, metadata = self.paint(replace(self.snapshot(), model_age_seconds=1))
    self.assertTrue(metadata["camera_displayed"])
    self.assertFalse(metadata["model_displayed"])
    self.assertEqual(metadata["reason"], "Model unavailable")

  def test_display_age_includes_radar_that_clips_the_path(self):
    road = replace(self.snapshot(), leads=(Lead(25, 0, 0, True),), radar_age_seconds=.2, captured_at=100.)
    with patch("tools.android_auto.road_render.time.monotonic", return_value=100.05):
      _, metadata = self.paint(road)
    self.assertTrue(metadata["radar_displayed"])
    self.assertAlmostEqual(metadata["display_age_seconds"], .25)
    self.assertAlmostEqual(metadata["display_source_timestamp"], 99.8)

  def test_expired_throttle_state_removes_previous_green_without_fading(self):
    renderer = RoadRenderer(self.viewport)
    road = replace(self.snapshot(), longitudinal_control=True, longitudinal_plan_age_seconds=float("inf"))
    image = Image.new("RGB", (1280, 720))
    state = LiveDisplayState(speed=45, status="engaged", started=True, age_seconds=.01)
    renderer.paint(image, road, state)
    self.assertEqual(renderer.throttle_blend, 0.)
    self.assertEqual(len(set(image.getpixel((640, 570)))), 1)

  def test_display_age_includes_path_decision_flags(self):
    road = replace(self.snapshot(), longitudinal_control=True, longitudinal_plan_age_seconds=.2, selfdrive_age_seconds=.1, captured_at=100.)
    with patch("tools.android_auto.road_render.time.monotonic", return_value=100.05):
      _, metadata = self.paint(road)
    self.assertEqual(metadata["polygons_drawn"]["path"], 1)
    self.assertAlmostEqual(metadata["display_age_seconds"], .25)
    self.assertAlmostEqual(metadata["display_source_timestamp"], 99.8)

  def test_model_available_but_fully_clipped_is_not_reported_as_displayed(self):
    road = self.snapshot()
    def offscreen(line):
      return tuple((x, y + 200, z) for x, y, z in line)
    model = replace(road.model, position=offscreen(road.model.position), lane_lines=tuple(map(offscreen, road.model.lane_lines)),
                    road_edges=tuple(map(offscreen, road.model.road_edges)))
    road = replace(road, model=model, model_age_seconds=.2, captured_at=100.)
    with patch("tools.android_auto.road_render.time.monotonic", return_value=100.05):
      _, metadata = self.paint(road)
    self.assertTrue(metadata["model_available"])
    self.assertFalse(metadata["model_displayed"])
    self.assertEqual(metadata["polygons_drawn"], {"path": 0, "lanes": 0, "edges": 0, "leads": 0})
    self.assertAlmostEqual(metadata["display_age_seconds"], .06)
    self.assertAlmostEqual(metadata["display_source_timestamp"], 99.99)

  def test_camera_only_source_timestamp_does_not_count_capture_or_paint_delay_twice(self):
    road = replace(self.snapshot(), camera_age_seconds=.1, captured_at=100.)
    variants = (replace(road, geometry=None), replace(road, geometry=replace(road.geometry, calibrated=False)), replace(road, model=None))
    for variant in variants:
      for paint_time in (100.05, 100.2):
        with self.subTest(calibrated=variant.geometry is not None, model=variant.model is not None, paint_time=paint_time):
          with patch("tools.android_auto.road_render.time.monotonic", return_value=paint_time):
            _, metadata = self.paint(variant)
          self.assertTrue(metadata["camera_displayed"])
          self.assertAlmostEqual(metadata["display_source_timestamp"], 99.9)
          self.assertAlmostEqual(metadata["display_age_seconds"], paint_time - 99.9)

  def test_visible_model_source_timestamp_is_based_on_snapshot_capture(self):
    road = replace(self.snapshot(), camera_age_seconds=.1, model_age_seconds=.2, captured_at=100.)
    with patch("tools.android_auto.road_render.time.monotonic", return_value=100.05):
      _, metadata = self.paint(road)
    self.assertTrue(metadata["model_displayed"])
    self.assertAlmostEqual(metadata["display_source_timestamp"], 99.8)


if __name__ == "__main__":
  unittest.main()
