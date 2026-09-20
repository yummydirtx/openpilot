import unittest
from unittest.mock import Mock, patch

from tools.android_auto.frame_worker import MAX_FRAME_BYTES
from tools.android_auto.hardware_encode import HardwareH264Encoder, normalize_access_unit
from tools.android_auto.live_encode import create_native_encoder
from tools.android_auto.video import access_units, nal_units


def nal(kind):
  return b"\x00\x00\x00\x01" + bytes([kind]) + b"payload"


class TestHardwareFraming(unittest.TestCase):
  def test_header_and_idr_become_one_independent_access_unit(self):
    for data in (nal(7) + nal(8) + nal(5), nal(7) + nal(8) + nal(9) + nal(5)):
      result = normalize_access_unit(data, keyframe=True)
      self.assertEqual(access_units(result), [result])
      self.assertEqual([kind for kind, _ in nal_units(result)], [9, 7, 8, 5])

  def test_predicted_frame_does_not_inherit_obsolete_headers(self):
    result = normalize_access_unit(nal(1), keyframe=False)
    self.assertEqual([kind for kind, _ in nal_units(result)], [9, 1])
    with self.assertRaises(RuntimeError):
      normalize_access_unit(nal(1), keyframe=True)

  def test_rejects_missing_headers_corrupt_unbounded_or_multiple_frames(self):
    for data in (b"", b"bad" + nal(5), b"\x00\x00\x01", nal(7) + nal(8), nal(5),
                 nal(7) + nal(8) + nal(5) + nal(1), nal(9) + nal(9) + nal(5),
                 nal(7) + nal(8) + nal(5) + b"x" * MAX_FRAME_BYTES):
      with self.subTest(prefix=data[:20]), self.assertRaises(RuntimeError):
        normalize_access_unit(data, keyframe=True)

  def test_invalid_configuration_never_loads_a_driver(self):
    for width, height, fps in ((0, 720, 30), (1281, 720, 30), (1280, 721, 30), (1280, 720, 60)):
      with self.assertRaises(ValueError):
        HardwareH264Encoder(width, height, fps=fps, library="/must/not/load")
    for margin in (-4, 2, 720):
      with self.assertRaises(ValueError):
        HardwareH264Encoder(1280, 720, margin_height=margin, library="/must/not/load")


class TestEncoderSelection(unittest.TestCase):
  def test_auto_uses_validated_hardware(self):
    encoder = Mock()
    with patch("tools.android_auto.hardware_encode.HardwareH264Encoder", return_value=encoder):
      result, reason = create_native_encoder(800, 480)
    self.assertIs(result, encoder)
    self.assertIsNone(reason)
    encoder.encode_rgba.assert_called_once_with(bytes(800 * 480 * 4), force_keyframe=True)

  def test_black_viewport_margins_reach_hardware_only(self):
    with patch("tools.android_auto.hardware_encode.HardwareH264Encoder") as hardware:
      create_native_encoder(1280, 720, margin_height=240)
    hardware.assert_called_once_with(1280, 720, margin_height=240)

  def test_auto_cleans_up_failed_hardware_before_software_fallback(self):
    encoder = Mock()
    encoder.encode_rgba.side_effect = RuntimeError("No independently decodable keyframe")
    with patch("tools.android_auto.hardware_encode.HardwareH264Encoder", return_value=encoder), \
         patch("tools.android_auto.live_encode.H264Encoder") as software:
      result, reason = create_native_encoder(800, 480)
    encoder.close.assert_called_once()
    self.assertIs(result, software.return_value)
    self.assertIn("keyframe", reason)

  def test_auto_handles_missing_library_but_explicit_hardware_reports_failure(self):
    with patch("tools.android_auto.hardware_encode.HardwareH264Encoder", side_effect=OSError("library missing")), \
         patch("tools.android_auto.live_encode.H264Encoder") as software:
      result, reason = create_native_encoder(800, 480)
      self.assertIs(result, software.return_value)
      self.assertIn("library missing", reason)
      with self.assertRaises(OSError):
        create_native_encoder(800, 480, "hardware")
    software.assert_called_once()

  def test_explicit_software_never_opens_hardware(self):
    with patch("tools.android_auto.hardware_encode.HardwareH264Encoder") as hardware, \
         patch("tools.android_auto.live_encode.H264Encoder") as software:
      result, reason = create_native_encoder(800, 480, "software")
    hardware.assert_not_called()
    self.assertIs(result, software.return_value)
    self.assertIsNone(reason)
