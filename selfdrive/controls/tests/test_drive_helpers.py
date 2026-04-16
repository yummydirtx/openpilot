from openpilot.selfdrive.controls.lib.drive_helpers import should_stop_with_hysteresis


def test_should_stop_with_hysteresis_default_threshold():
  assert should_stop_with_hysteresis(0.24, 0.24, 0.25)
  assert not should_stop_with_hysteresis(0.24, 0.26, 0.25)


def test_should_stop_with_hysteresis_enter_exit_margins():
  # With Mazda's hysteresis, hovering near the stop threshold should not enter
  # stop mode from a moving/restarting state.
  assert not should_stop_with_hysteresis(0.24, 0.24, 0.25, prev_should_stop=False,
                                         enter_margin=0.05, exit_margin=0.05)
  # Once we are in stop mode, stay there until the target speeds rise
  # meaningfully above the stop threshold.
  assert should_stop_with_hysteresis(0.24, 0.24, 0.25, prev_should_stop=True,
                                     enter_margin=0.05, exit_margin=0.05)
  assert not should_stop_with_hysteresis(0.31, 0.24, 0.25, prev_should_stop=True,
                                         enter_margin=0.05, exit_margin=0.05)
