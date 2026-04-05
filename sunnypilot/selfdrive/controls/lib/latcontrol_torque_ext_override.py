"""
Copyright (c) 2021-, Haibin Wen, sunnypilot, and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.
"""

import numpy as np

from openpilot.common.params import Params


class LatControlTorqueExtOverride:
  def __init__(self, CP):
    self.CP = CP
    self.params = Params()
    self.enforce_torque_control_toggle = self.params.get_bool("EnforceTorqueControl")  # only during init
    self.torque_override_enabled = self.params.get_bool("TorqueParamsOverrideEnabled")
    self.frame = -1

    # Speed-dep state (set by LatControlTorqueExt subclass)
    self._speed_dep_active = False
    self._speed_dep_speed_bp = []
    self._speed_dep_lat_accel_factor_bp = []
    self._speed_dep_friction_bp = []
    self._speed_dep_car_cfg = None
    self._last_vego = 0.0

    # Speed-dependent STEER_MAX: read from CarControllerParams if available.
    # Cars like Mazda/Rivian have STEER_MAX that varies with speed — LAF is
    # normalized by STEER_MAX, so interpolation between bins on different
    # scales needs normalization. None = constant STEER_MAX, plain interp.
    self._steer_max_lookup = None
    try:
      from importlib import import_module
      ccp = import_module(f'opendbc.car.{CP.brand}.values').CarControllerParams(CP)
      if hasattr(ccp, 'STEER_MAX_LOOKUP'):
        self._steer_max_lookup = ccp.STEER_MAX_LOOKUP
    except Exception:
      pass

  def _interp_laf(self, vego, speed_bp, laf_bp):
    """Interpolate LAF with STEER_MAX normalization when applicable.
    Converts to physical space (LAF/STEER_MAX), interpolates, then converts
    back. Friction is unaffected — it's in lateral_acc space."""
    if self._steer_max_lookup is None:
      return float(np.interp(vego, speed_bp, laf_bp))
    sm_speeds, sm_values = self._steer_max_lookup
    physical_laf = [laf / float(np.interp(s, sm_speeds, sm_values))
                    for s, laf in zip(speed_bp, laf_bp)]
    current_sm = float(np.interp(vego, sm_speeds, sm_values))
    return float(np.interp(vego, speed_bp, physical_laf)) * current_sm

  def update_override_torque_params(self, torque_params) -> bool:
    changed = False

    # Speed-dep latAccelFactor and friction: interpolate by current speed each frame.
    # Must run here (before get_friction and torque_from_lateral_accel use
    # torque_params) because extension.update() runs after those calls.
    if self._speed_dep_active and self._speed_dep_speed_bp:
      new_lat_accel_factor = self._interp_laf(self._last_vego, self._speed_dep_speed_bp, self._speed_dep_lat_accel_factor_bp)
      new_fric = float(np.interp(self._last_vego, self._speed_dep_speed_bp, self._speed_dep_friction_bp))
      if new_lat_accel_factor != torque_params.latAccelFactor or new_fric != torque_params.friction:
        torque_params.latAccelFactor = new_lat_accel_factor
        torque_params.friction = new_fric
        changed = True

    if not self.enforce_torque_control_toggle:
      return changed

    self.frame += 1
    if self.frame % 300 == 0:
      self.torque_override_enabled = self.params.get_bool("TorqueParamsOverrideEnabled")

      if not self.torque_override_enabled:
        return changed

      torque_params.latAccelFactor = float(self.params.get("TorqueParamsOverrideLatAccelFactor", return_default=True))
      torque_params.friction = float(self.params.get("TorqueParamsOverrideFriction", return_default=True))
      return True

    return changed
