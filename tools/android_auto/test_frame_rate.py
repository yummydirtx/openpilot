import unittest

from tools.android_auto.frame_rate import FrameRate


class TestFrameRate(unittest.TestCase):
  def test_load_reduces_cadence_before_runtime_cpu_guard_expires(self):
    rate = FrameRate(30)
    self.assertTrue(rate.sample(1.1))
    self.assertEqual(rate.current, 20)
    self.assertFalse(rate.sample(.72))

  def test_recovery_needs_sustained_headroom_and_respects_maximum(self):
    rate = FrameRate(25)
    rate.sample(1.3)
    self.assertEqual(rate.current, 15)
    for _ in range(9):
      self.assertFalse(rate.sample(.5))
    self.assertTrue(rate.sample(.5))
    self.assertEqual(rate.current, 20)
    for _ in range(100):
      rate.sample(.3)
    self.assertEqual(rate.current, 25)

  def test_lowest_rate_does_not_hide_sustained_overload(self):
    rate = FrameRate(8)
    for _ in range(5):
      self.assertFalse(rate.sample(1.))
    self.assertEqual(rate.current, 8)
