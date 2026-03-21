#!/usr/bin/env python3
"""Tests for speed-dependent torque mechanism (vehicle-agnostic).

Uses get_speed_dependent_torque_params() to discover configured cars from
speed_dependent.toml, then tests the CarInterfaceExt callbacks generically.
"""
import numpy as np
import pytest

from unittest.mock import MagicMock  # noqa: TID251
from opendbc.car.interfaces import get_speed_dependent_torque_params

# Discover all cars with speed-dependent torque config
SPEED_DEP_CARS = get_speed_dependent_torque_params()


def get_car_interface_ext(fingerprint):
  """Dynamically import and instantiate CarInterfaceExt for a given fingerprint."""
  from opendbc.car.values import PLATFORMS
  platform = PLATFORMS.get(fingerprint)
  if platform is None:
    return None
  brand = platform.__class__.__module__.split('.')[-2]
  mod = __import__(f'opendbc.sunnypilot.car.{brand}.interface_ext', fromlist=['CarInterfaceExt'])
  CP = MagicMock()
  CP.carFingerprint = fingerprint
  CI_Base = MagicMock()
  return mod.CarInterfaceExt(CP, CI_Base)


def _get_unconfigured_ext():
  """Build an ext for a fake unconfigured car using a configured car's brand."""
  CP = MagicMock()
  CP.carFingerprint = 'DEFINITELY_NOT_A_REAL_CAR'
  CI_Base = MagicMock()
  brand_fingerprint = next(iter(SPEED_DEP_CARS))
  from opendbc.car.values import PLATFORMS
  platform = PLATFORMS.get(brand_fingerprint)
  brand = platform.__class__.__module__.split('.')[-2]
  mod = __import__(f'opendbc.sunnypilot.car.{brand}.interface_ext', fromlist=['CarInterfaceExt'])
  return mod.CarInterfaceExt(CP, CI_Base), CI_Base


@pytest.mark.skipif(len(SPEED_DEP_CARS) == 0, reason="No cars configured in speed_dependent.toml")
class TestSpeedDepTorqueCallbacks:
  """Test speed-dependent torque callbacks for every configured car."""

  def test_config_has_required_keys(self):
    for fingerprint, cfg in SPEED_DEP_CARS.items():
      assert 'speed_bp' in cfg, f"{fingerprint} missing speed_bp"
      assert 'laf_bp' in cfg, f"{fingerprint} missing laf_bp"
      assert len(cfg['speed_bp']) == len(cfg['laf_bp']), f"{fingerprint} speed_bp/laf_bp length mismatch"
      assert len(cfg['speed_bp']) > 1, f"{fingerprint} needs at least 2 breakpoints"
      for i in range(len(cfg['speed_bp']) - 1):
        assert cfg['speed_bp'][i] < cfg['speed_bp'][i + 1], f"{fingerprint} speed_bp not sorted at index {i}"
      if 'friction_bp' in cfg:
        assert len(cfg['friction_bp']) == len(cfg['speed_bp']), f"{fingerprint} friction_bp/speed_bp length mismatch"

  def test_laf_values_positive(self):
    for fingerprint, cfg in SPEED_DEP_CARS.items():
      for laf in cfg['laf_bp']:
        assert laf > 0, f"{fingerprint}: LAF must be positive"

  def test_closure_uses_table_values(self):
    for fingerprint, cfg in SPEED_DEP_CARS.items():
      ext = get_car_interface_ext(fingerprint)
      if ext is None or not hasattr(ext, 'torque_from_lateral_accel_speed_dep_closure'):
        continue
      tp = MagicMock()
      ext.v_ego = 15.0
      laf = float(np.interp(15.0, cfg['speed_bp'], cfg['laf_bp']))
      torque = ext.torque_from_lateral_accel_speed_dep_closure(1.0, tp)
      assert torque == pytest.approx(1.0 / laf, abs=1e-6), f"{fingerprint}: closure should use table LAF"

  def test_closure_tracks_updated_table(self):
    for fingerprint, cfg in SPEED_DEP_CARS.items():
      ext = get_car_interface_ext(fingerprint)
      if ext is None or not hasattr(ext, 'torque_from_lateral_accel_speed_dep_closure'):
        continue
      tp = MagicMock()
      ext.v_ego = cfg['speed_bp'][0]
      torque_before = ext.torque_from_lateral_accel_speed_dep_closure(1.0, tp)
      nudged = [v * 1.1 for v in ext.speed_dep_laf_v]
      friction = cfg.get('friction_bp', [0.1] * len(cfg['speed_bp']))
      ext.update_speed_dep_laf(cfg['speed_bp'], nudged, friction, [True] * len(cfg['speed_bp']))
      torque_after = ext.torque_from_lateral_accel_speed_dep_closure(1.0, tp)
      assert torque_before != pytest.approx(torque_after, abs=1e-3), \
        f"{fingerprint}: closure should reflect updated LAF"

  def test_inverse_consistency(self):
    for fingerprint in SPEED_DEP_CARS:
      ext = get_car_interface_ext(fingerprint)
      if ext is None or not hasattr(ext, 'torque_from_lateral_accel_speed_dep_closure'):
        continue
      tp = MagicMock()
      for speed in [5.0, 15.0, 25.0]:
        ext.v_ego = speed
        lat_accel = 0.8
        torque = ext.torque_from_lateral_accel_speed_dep_closure(lat_accel, tp)
        recovered = ext.lateral_accel_from_torque_speed_dep_closure(torque, tp)
        assert lat_accel == pytest.approx(recovered, abs=1e-6), f"{fingerprint} @ {speed} m/s: inverse failed"

  def test_torque_space_callback_returns_speed_dep(self):
    for fingerprint in SPEED_DEP_CARS:
      ext = get_car_interface_ext(fingerprint)
      if ext is None or not hasattr(ext, 'torque_from_lateral_accel_in_torque_space'):
        continue
      cb = ext.torque_from_lateral_accel_in_torque_space()
      assert cb.__name__ != 'torque_from_lateral_accel_linear_in_torque_space', \
        f"{fingerprint} should not use linear callback"

  def test_update_speed_dep_laf_within_bounds(self):
    for fingerprint, cfg in SPEED_DEP_CARS.items():
      ext = get_car_interface_ext(fingerprint)
      if ext is None or not hasattr(ext, 'update_speed_dep_laf'):
        continue
      original = list(ext.speed_dep_laf_v)
      nudged_laf = [v * 1.05 for v in original]
      friction = cfg.get('friction_bp', [0.1] * len(cfg['speed_bp']))
      ext.update_speed_dep_laf(cfg['speed_bp'], nudged_laf, friction, [True] * len(cfg['speed_bp']))
      for i in range(len(original)):
        assert ext.speed_dep_laf_v[i] == pytest.approx(nudged_laf[i], abs=1e-4), \
          f"{fingerprint} bin {i}: should accept +5% nudge"

  def test_update_speed_dep_laf_out_of_bounds_rejected(self):
    for fingerprint, cfg in SPEED_DEP_CARS.items():
      ext = get_car_interface_ext(fingerprint)
      if ext is None or not hasattr(ext, 'update_speed_dep_laf'):
        continue
      original = list(ext.speed_dep_laf_v)
      bad_laf = [v * 5.0 for v in original]
      friction = cfg.get('friction_bp', [0.1] * len(cfg['speed_bp']))
      ext.update_speed_dep_laf(cfg['speed_bp'], bad_laf, friction, [True] * len(cfg['speed_bp']))
      assert ext.speed_dep_laf_v == original, f"{fingerprint}: out-of-bounds LAF should be rejected"

  def test_update_speed_dep_laf_invalid_bins_skipped(self):
    for fingerprint, cfg in SPEED_DEP_CARS.items():
      ext = get_car_interface_ext(fingerprint)
      if ext is None or not hasattr(ext, 'update_speed_dep_laf'):
        continue
      n = len(cfg['speed_bp'])
      original = list(ext.speed_dep_laf_v)
      nudged_laf = [v * 1.05 for v in original]
      friction = cfg.get('friction_bp', [0.1] * n)
      valid = [(i % 2 == 0) for i in range(n)]
      ext.update_speed_dep_laf(cfg['speed_bp'], nudged_laf, friction, valid)
      for i in range(n):
        if valid[i]:
          assert ext.speed_dep_laf_v[i] == pytest.approx(nudged_laf[i], abs=1e-4)
        else:
          assert ext.speed_dep_laf_v[i] == pytest.approx(original[i], abs=1e-4)

  def test_update_speed_dep_laf_mismatched_length_rejected(self):
    for fingerprint, cfg in SPEED_DEP_CARS.items():
      ext = get_car_interface_ext(fingerprint)
      if ext is None or not hasattr(ext, 'update_speed_dep_laf'):
        continue
      original = list(ext.speed_dep_laf_v)
      short_laf = [v * 1.05 for v in original[:-1]]
      short_valid = [True] * (len(original) - 1)
      friction = cfg.get('friction_bp', [0.1] * len(cfg['speed_bp']))
      ext.update_speed_dep_laf(cfg['speed_bp'], short_laf, friction, short_valid)
      assert ext.speed_dep_laf_v == original, f"{fingerprint}: mismatched-length update should be rejected"


@pytest.mark.skipif(len(SPEED_DEP_CARS) == 0, reason="No cars configured in speed_dependent.toml")
class TestNonConfiguredCarsUnaffected:
  """Cars NOT in speed_dependent.toml should use linear model."""

  def test_unconfigured_car_uses_linear(self):
    ext, CI_Base = _get_unconfigured_ext()
    cb = ext.torque_from_lateral_accel_in_torque_space()
    assert cb == CI_Base.torque_from_lateral_accel_linear_in_torque_space

  def test_unconfigured_car_update_is_noop(self):
    ext, _ = _get_unconfigured_ext()
    ext.update_speed_dep_laf([5.0, 15.0], [2.0, 2.0], [0.1, 0.1], [True, True])
