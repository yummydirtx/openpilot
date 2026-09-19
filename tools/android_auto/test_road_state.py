from dataclasses import FrozenInstanceError
import math
from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock, patch

from tools.android_auto.road_state import (
  ROAD_SERVICES, VIEW_FROM_DEVICE, RoadStateReader, copy_nv12, matrix_product, model_snapshot, nv12_to_rgb, rotation_from_euler,
)


class FakeBuffer:
  width, height, stride, uv_offset = 4, 2, 8, 24
  frame_id = 5

  def __init__(self):
    self.data = bytearray([16, 17, 18, 19, 250, 250, 250, 250, 20, 21, 22, 23] + [250] * 12 + [128, 129, 130, 131] + [250] * 4)


class FakeClient:
  frame_id, timestamp_sof, timestamp_eof, valid = 5, 99_980_000_000, 99_990_000_000, False

  def __init__(self, stream):
    self.stream = stream
    self.connect = Mock(return_value=True)
    self.is_connected = Mock(return_value=True)
    self.recv = Mock(return_value=FakeBuffer())


class RoadSubMaster:
  def __init__(self):
    self.services = ROAD_SERVICES
    self.seen, self.alive, self.valid, self.updated = (dict.fromkeys(ROAD_SERVICES, True) for _ in range(4))
    self.recv_time = dict.fromkeys(ROAD_SERVICES, 100.0)
    self.logMonoTime = dict.fromkeys(ROAD_SERVICES, 100_000_000_000)
    def line():
      return NS(x=[1.0, 10.0, 20.0], y=[0.0, 0.0, 0.0], z=[0.0, 0.0, 0.0])
    self.data = {
      "deviceState": NS(deviceType="mici", started=True),
      "narrowRoadCameraState": NS(sensor="os04c10", frameId=5, timestampEof=99_990_000_000),
      "wideRoadCameraState": NS(sensor="os04c10", frameId=5, timestampEof=99_990_000_000),
      "extrinsicsCalibration": NS(calStatus="calibrated", rpyCalib=[0, 0, 0], wideFromDeviceEuler=[0, 0, 0], height=[1.22]),
      "modelV2": NS(position=line(), laneLines=[line() for _ in range(4)], roadEdges=[line() for _ in range(2)],
                    laneLineProbs=[1, 1, 1, 1], roadEdgeStds=[0, 0], acceleration=NS(x=[0, 0, 0]),
                    frameId=5, frameIdExtra=5, timestampEof=99_990_000_000),
      "radarState": NS(leadOne=NS(present=True, dRel=20, yRel=0.2, vRel=-1), leadTwo=NS(present=False)),
      "selfdriveState": NS(experimentalMode=False, engageable=True),
      "carState": NS(vEgo=0), "carParams": NS(openpilotLongitudinalControl=True),
      "longitudinalPlan": NS(allowThrottle=True),
      "driverMonitoringState": NS(activePolicy="vision", isRHD=False),
      "driverStateV2": NS(leftDriverData=NS(faceOrientation=[0.1, 0.2, 0.3]), rightDriverData=NS(faceOrientation=[0.3, 0.2, 0.1])),
    }
    self.update = Mock()

  def __getitem__(self, key):
    return self.data[key]


class TestRoadState(unittest.TestCase):
  def setUp(self):
    self.sm = RoadSubMaster()
    camera = NS(width=4, height=2, intrinsics=((10, 0, 2), (0, 10, 1), (0, 0, 1)))
    self.configs = {("mici", "os04c10"): NS(narrow_road=camera, wide_road=camera)}
    self.params = Mock(spec=["get"])
    self.params.get.return_value = 0
    self.reader = RoadStateReader(sm=self.sm, params=self.params, client_factory=FakeClient, camera_configs=self.configs,
                                  converter=lambda data, width, height: bytes(data))

  def poll(self, now=100.0):
    with patch("tools.android_auto.road_state.time.monotonic", return_value=now):
      return self.reader.poll()

  def test_nv12_copy_strips_both_row_and_plane_padding_and_owns_pixels(self):
    buffer = FakeBuffer()
    data, width, height = copy_nv12(buffer, 5)
    self.assertEqual((width, height), (4, 2))
    self.assertEqual(data, bytes([16, 17, 18, 19, 20, 21, 22, 23, 128, 129, 130, 131]))
    buffer.data[0] = 200
    self.assertEqual(data[0], 16)

  def test_nv12_overwrite_before_or_during_copy_is_rejected(self):
    with self.assertRaisesRegex(ValueError, "already overwritten"):
      copy_nv12(FakeBuffer(), 6)
    class Overwritten(FakeBuffer):
      reads = 0

      @property
      def frame_id(self):
        self.reads += 1
        return 5 if self.reads == 1 else 6
    with self.assertRaisesRegex(ValueError, "during copy"):
      copy_nv12(Overwritten(), 5)

  def test_invalid_nv12_layout_does_not_get_converted(self):
    for attr, value in (("width", 3), ("height", 4096), ("stride", 2), ("uv_offset", 2)):
      with self.subTest(attr=attr), self.assertRaisesRegex(ValueError, "NV12"):
        buffer = FakeBuffer()
        setattr(buffer, attr, value)
        copy_nv12(buffer, 5)

  def test_optimized_nv12_conversion_matches_reference_for_aligned_and_padded_planes(self):
    import av
    for width, height in ((4, 2), (32, 4), (34, 6), (1344, 6)):
      with self.subTest(width=width, height=height):
        data = bytearray(16 + (index * 17) % 220 for index in range(width * height))
        data += bytearray(80 + (index * 13) % 96 for index in range(width * height // 2))
        frame = av.VideoFrame(width, height, "nv12")
        offset = 0
        for plane, rows in zip(frame.planes, (height, height // 2), strict=True):
          pixels = bytearray(plane.buffer_size)
          for row in range(rows):
            pixels[row * plane.line_size:row * plane.line_size + width] = data[offset:offset + width]
            offset += width
          plane.update(pixels)
        expected = frame.to_image()
        actual = nv12_to_rgb(data, width, height)
        self.assertEqual(actual.mode, "RGB")
        self.assertEqual(actual.size, (width, height))
        self.assertEqual(actual.tobytes(), expected.tobytes())
        before = actual.tobytes()
        data[:] = bytes(len(data))
        self.assertEqual(actual.tobytes(), before)

  def test_nv12_neutral_black_white_and_bad_input(self):
    for luma, expected in ((16, (0, 0, 0)), (235, (255, 255, 255))):
      image = nv12_to_rgb(bytes([luma] * 8 + [128] * 4), 4, 2)
      self.assertLessEqual(max(abs(actual - target) for actual, target in zip(image.getpixel((0, 0)), expected, strict=True)), 2)
    for data, width, height in ((b"", 4, 2), (bytes(12), 3, 2), (bytes(12), 4, 0)):
      with self.subTest(size=(width, height)), self.assertRaisesRegex(ValueError, "Invalid packed NV12"):
        nv12_to_rgb(data, width, height)

  def test_fresh_snapshot_does_not_treat_unused_vipc_valid_as_failure(self):
    state = self.poll()
    self.assertFalse(state.camera_stale)
    self.assertFalse(state.model_stale)
    self.assertTrue(state.geometry.calibrated)
    self.assertEqual(state.geometry.view_from_calib, VIEW_FROM_DEVICE)
    self.assertEqual(state.camera.stream, "narrow")
    self.assertEqual(len(state.leads), 1)
    self.assertTrue(state.driver_state.active)
    self.reader.client.recv.assert_called_once_with(0)
    self.reader.client.connect.assert_called_once_with(False)
    with self.assertRaises(FrozenInstanceError):
      state.camera.frame_id = 42

  def test_cached_frame_is_reused_without_repeated_conversion(self):
    convert = Mock(return_value=b"pixels")
    self.reader.converter = convert
    first = self.poll()
    self.reader.client.recv.return_value = None
    second = self.poll()
    self.assertIs(first.camera, second.camera)
    self.assertEqual(convert.call_count, 1)

  def test_camera_and_model_expiration_are_independent(self):
    self.sm.logMonoTime["modelV2"] = 99_600_000_000
    state = self.poll()
    self.assertIsNotNone(state.camera)
    self.assertIsNone(state.model)
    self.assertEqual(state.leads, ())
    self.assertIsNotNone(state.driver_state)
    self.sm.valid["narrowRoadCameraState"] = False
    self.assertIsNone(self.poll().camera)

  def test_model_acquisition_age_and_camera_alignment_are_required(self):
    for key, value in (("timestampEof", 99_600_000_000), ("timestampEof", 99_800_000_000), ("frameId", 1)):
      with self.subTest(key=key):
        self.setUp()
        setattr(self.sm["modelV2"], key, value)
        state = self.poll()
        self.assertIsNotNone(state.camera)
        self.assertIsNone(state.model)
        self.assertIn("model/camera not synchronized", state.reasons)

  def test_wide_camera_uses_extra_frame_id_and_actual_wide_calibration(self):
    self.sm["selfdriveState"].experimentalMode = True
    self.sm["modelV2"].frameId = 999
    self.sm["extrinsicsCalibration"].wideFromDeviceEuler = [0, 0, 0.1]
    state = self.poll()
    self.assertEqual(state.camera.stream, "wide")
    self.assertIsNotNone(state.model)
    expected = matrix_product(VIEW_FROM_DEVICE, rotation_from_euler([0, 0, 0.1]))
    self.assertEqual(state.geometry.view_from_calib, expected)
    self.sm["carState"].vEgo = 12
    self.assertEqual(self.poll().camera.stream, "wide")
    self.sm["carState"].vEgo = 16
    self.assertEqual(self.poll().camera.stream, "narrow")

  def test_uncalibrated_camera_retains_real_image_but_suppresses_overlays(self):
    self.sm["extrinsicsCalibration"].calStatus = "uncalibrated"
    state = self.poll()
    self.assertIsNotNone(state.camera)
    self.assertFalse(state.geometry.calibrated)
    self.assertIsNone(state.model)
    self.assertEqual(state.leads, ())

  def test_unknown_intrinsics_or_changed_dimensions_never_use_a_default(self):
    self.sm["narrowRoadCameraState"].sensor = "unknown-new-sensor"
    state = self.poll()
    self.assertIsNotNone(state.camera)
    self.assertIsNone(state.geometry)
    self.assertIsNone(state.model)
    self.setUp()
    self.configs[("mici", "os04c10")].narrow_road.width = 1280
    self.assertIn("camera dimensions mismatch", self.poll().reasons)

  def test_malformed_model_is_bounded_and_does_not_remove_camera(self):
    for values in ([1], [1] * 129, [1, math.nan, 3]):
      with self.subTest(values=len(values)):
        self.setUp()
        self.sm["modelV2"].position.x = values
        state = self.poll()
        self.assertIsNotNone(state.camera)
        self.assertIsNone(state.model)
    frozen = model_snapshot(RoadSubMaster()["modelV2"])
    self.sm["modelV2"].laneLines[0].x[0] = 999
    self.assertEqual(frozen.lane_lines[0][0][0], 1)

  def test_bad_radar_or_driver_pose_does_not_remove_camera_and_path(self):
    self.sm["radarState"].leadOne.dRel = math.nan
    self.sm["driverStateV2"].leftDriverData.faceOrientation = [1]
    state = self.poll()
    self.assertIsNotNone(state.model)
    self.assertEqual(state.leads, ())
    self.assertIsNone(state.driver_state)

  def test_settings_or_camera_transport_failure_has_separate_unavailable_state(self):
    self.params.get.side_effect = OSError("parameter read failed")
    state = self.poll()
    self.assertIsNotNone(state.camera)
    self.assertIsNone(state.model)
    self.assertIn("camera offset unavailable", state.reasons)
    self.reader.client.recv.side_effect = OSError("camera closed")
    state = self.poll()
    self.assertIsNone(state.camera)
    self.assertIsNotNone(state.driver_state)

  def test_display_age_metadata_tracks_only_available_rendered_sources(self):
    self.sm.logMonoTime["modelV2"] = 99_600_000_000
    self.sm.logMonoTime["driverStateV2"] = 99_800_000_000
    state = self.poll()
    self.assertAlmostEqual(state.display_age_seconds, 0.2)
    metadata = state.metadata(now=100.05)
    self.assertAlmostEqual(metadata["display_age_seconds"], 0.25)
    self.assertFalse(metadata["model_available"])
    self.assertNotIn("rgb", metadata)

  def test_decision_ages_are_independent_of_fresh_camera_model_data(self):
    self.sm.logMonoTime["selfdriveState"] = 99_700_000_000
    self.sm.logMonoTime["longitudinalPlan"] = 99_680_000_000
    state = self.poll()
    self.assertIsNotNone(state.model)
    self.assertAlmostEqual(state.selfdrive_age_seconds, 0.3)
    self.assertAlmostEqual(state.longitudinal_plan_age_seconds, 0.32)
    self.assertAlmostEqual(state.display_age_seconds, 0.32)
    metadata = state.metadata(now=100.05)
    self.assertAlmostEqual(metadata["selfdrive_age_seconds"], 0.35)
    self.assertAlmostEqual(metadata["longitudinal_plan_age_seconds"], 0.37)

  def test_expired_decisions_are_unavailable_without_discarding_live_geometry(self):
    self.sm["selfdriveState"].experimentalMode = True
    self.sm.logMonoTime["selfdriveState"] = 99_600_000_000
    self.sm.logMonoTime["longitudinalPlan"] = 99_600_000_000
    state = self.poll()
    self.assertIsNotNone(state.camera)
    self.assertIsNotNone(state.model)
    self.assertFalse(state.experimental_mode)
    self.assertFalse(state.engageable)
    self.assertFalse(state.allow_throttle)
    self.assertTrue(math.isinf(state.selfdrive_age_seconds))
    self.assertTrue(math.isinf(state.longitudinal_plan_age_seconds))
    self.assertIsNone(state.metadata(now=100)["selfdrive_age_seconds"])

  def test_unused_longitudinal_plan_does_not_shorten_camera_display_budget(self):
    self.sm["carParams"].openpilotLongitudinalControl = False
    self.sm.logMonoTime["longitudinalPlan"] = 99_680_000_000
    state = self.poll()
    self.assertAlmostEqual(state.longitudinal_plan_age_seconds, 0.32)
    self.assertLess(state.display_age_seconds, 0.1)

  def test_offroad_stops_camera_access(self):
    self.sm["deviceState"].started = False
    self.assertIsNone(self.poll().camera)
    self.assertIsNone(self.reader.client)


if __name__ == "__main__":
  unittest.main()
