from enum import IntEnum
from types import SimpleNamespace as NS, ModuleType
from unittest import TestCase
from unittest.mock import Mock, patch

from tools.android_auto.native_input import NativeInput, Target


class State(IntEnum):
  HOME = 0
  SETTINGS = 1
  ONROAD = 2


class TestNativeInput(TestCase):
  def setUp(self):
    module = ModuleType("openpilot.selfdrive.ui.layouts.main")
    module.MainState = State
    self.patch = patch.dict("sys.modules", {module.__name__: module})
    self.patch.start()
    self.addCleanup(self.patch.stop)
    self.input = NativeInput.__new__(NativeInput)
    self.input.app = NS(_nav_stack=[object()], pop_widget=Mock())
    self.input.main = NS(_current_mode=State.SETTINGS, open_settings=Mock(), _set_mode_for_state=Mock())
    self.input.command = Mock()
    self.input.previous = [Target((i, 0), NS(x=10, y=i * 100, width=80, height=80),
                                 NS(enabled=True, is_visible=True), (), Mock()) for i in range(5)]
    self.input.selected = (0, 0)
    self.input.visible = False

  def test_onroad_click_opens_full_settings_without_activating_a_setting(self):
    self.input.main._current_mode = State.ONROAD
    self.input.handle([("select", 1), ("select", 1)])
    self.input.main.open_settings.assert_called_once_with(99)
    for target in self.input.previous:
      target.callback.assert_not_called()

  def test_rotation_selects_original_callback_and_disabled_target_cannot_activate(self):
    self.input.handle([("rotate", 2), ("select", 1)])
    self.input.previous[2].callback.assert_called_once_with()
    self.input.previous[2].widget.enabled = False
    self.input.handle([("select", 1)])
    self.input.previous[2].callback.assert_called_once_with()

  def test_only_one_activation_per_render_to_prevent_click_through(self):
    self.input.handle([("select", 1), ("select", 1)])
    self.input.previous[0].callback.assert_called_once_with()

  def test_back_returns_to_road_then_oem(self):
    self.input.handle([("back", 1)])
    self.input.main._set_mode_for_state.assert_called_once_with()
    self.input.command.assert_not_called()
    self.input.main._current_mode = State.ONROAD
    self.input.handle([("back", 1)])
    self.input.command.assert_called_once_with("exit")

  def test_back_closes_dialog_before_leaving_settings(self):
    self.input.app._nav_stack.append(object())
    self.input.handle([("back", 1)])
    self.input.app.pop_widget.assert_called_once_with()
    self.input.main._set_mode_for_state.assert_not_called()

  def test_rotary_bounds_and_offscreen_target_scroll_into_view(self):
    panel = NS(rect=NS(y=100, height=400), scroll_panel=NS(offset=0, set_offset=Mock()))
    self.input.previous[-1].rect.y = 900
    self.input.previous[-1].scrollers = (panel,)
    self.input.handle([("rotate", 20)])
    self.assertEqual(self.input.selected, (4, 0))
    panel.scroll_panel.set_offset.assert_called_once_with(-488)
    self.input.handle([("rotate", -20)])
    self.assertEqual(self.input.selected, (0, 0))

  def test_media_shortcuts_release_to_oem(self):
    for name in ("music", "navigation"):
      self.input.command.reset_mock()
      self.input.handle([(name, 1)])
      self.input.command.assert_called_once_with("exit")
