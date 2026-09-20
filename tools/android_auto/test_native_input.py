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
    self.now = 100.
    clock = patch("tools.android_auto.native_input.time.monotonic", side_effect=lambda: self.now)
    clock.start()
    self.addCleanup(clock.stop)
    self.input.previous = [Target((i, 0), NS(x=10, y=i * 100, width=80, height=80),
                                 NS(enabled=True, is_visible=True), (), Mock()) for i in range(5)]
    self.input.selected = (0, 0)
    self.input.visible = True
    self.input.last_input_at = self.now

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

  def test_focus_fades_after_three_seconds_then_hides_without_losing_selection(self):
    self.now += 2.99
    self.assertEqual(self.input.focus_alpha(), 255)
    self.now += .135
    self.assertEqual(self.input.focus_alpha(), 128)
    self.now += .125
    self.assertEqual(self.input.focus_alpha(), 0)
    self.assertEqual(self.input.selected, (0, 0))

  def test_first_click_after_idle_reveals_and_next_click_activates(self):
    self.now += 4
    self.input.handle([("select", 1), ("select", 1)])
    self.input.previous[0].callback.assert_not_called()
    self.assertEqual(self.input.focus_alpha(), 255)
    self.input.handle([("select", 1)])
    self.input.previous[0].callback.assert_called_once_with()

  def test_rotation_after_idle_moves_selection_and_restarts_timer(self):
    self.now += 4
    self.input.handle([("rotate", 2)])
    self.assertEqual(self.input.selected, (2, 0))
    self.assertEqual(self.input.focus_alpha(), 255)
    self.now += 2
    self.input.handle([("unknown", 1)])
    self.now += 2
    self.assertEqual(self.input.focus_alpha(), 0)

  def test_home_hides_focus_immediately(self):
    self.input.handle([("home", 1)])
    self.assertEqual(self.input.focus_alpha(), 0)

  def test_clipping_does_not_shrink_or_recenter_focus_outline(self):
    def rect(x, y, width, height):
      return NS(x=x, y=y, width=width, height=height)

    def intersect(a, b):
      x, y = max(a.x, b.x), max(a.y, b.y)
      return rect(x, y, max(0, min(a.x + a.width, b.x + b.width) - x), max(0, min(a.y + a.height, b.y + b.height) - y))

    self.input.rl = NS(get_collision_rec=intersect, begin_scissor_mode=Mock(), end_scissor_mode=Mock(),
                       draw_rectangle_rounded_lines_ex=Mock(), Color=lambda *v: v)
    target = self.input.previous[0]
    target.focus_rect = rect(40, 80, 450, 110)
    target.scrollers = (NS(rect=rect(0, 100, 500, 400)),)
    self.input.targets = [target]
    self.input.finish()
    self.input.rl.begin_scissor_mode.assert_called_once_with(0, 100, 500, 400)
    self.assertEqual(self.input.rl.draw_rectangle_rounded_lines_ex.call_args.args[0], target.focus_rect)
    self.input.rl.end_scissor_mode.assert_called_once_with()


class TestSunnypilotFocusGeometry(TestCase):
  """No raylib/device imports; match the native layouts' distinct conventions."""

  def setUp(self):
    modules = {}

    def module(name, **members):
      value = ModuleType(name)
      value.__dict__.update(members)
      modules[name] = value

    self.classes = {name: type(name, (), {}) for name in ("NavButton", "ToggleSP", "MultipleButtonActionSP", "ListItemSP",
                                                       "SimpleButtonActionSP", "ToggleAction")}
    self.style = NS(TOGGLE_WIDTH=210, TOGGLE_BG_HEIGHT=100, BUTTON_HEIGHT=120, ITEM_PADDING=20, ITEM_BASE_HEIGHT=170)
    module("openpilot.selfdrive.ui.sunnypilot.layouts.settings.settings", NavButton=self.classes["NavButton"])
    module("openpilot.selfdrive.ui.layouts.settings.settings", SIDEBAR_WIDTH=500, NAV_BTN_HEIGHT=110)
    module("openpilot.system.ui.sunnypilot.widgets.toggle", ToggleSP=self.classes["ToggleSP"])
    module("openpilot.system.ui.sunnypilot.widgets.list_view", **{name: self.classes[name] for name in
                                                               ("MultipleButtonActionSP", "ListItemSP", "SimpleButtonActionSP")})
    module("openpilot.system.ui.widgets.list_view", ToggleAction=self.classes["ToggleAction"])
    module("openpilot.system.ui.sunnypilot.lib.styles", style=self.style)
    patches = patch.dict("sys.modules", modules)
    patches.start()
    self.addCleanup(patches.stop)
    self.input = NativeInput.__new__(NativeInput)
    self.input.rl = NS(Rectangle=self.rect)
    self.input.parents = []
    self.input.targets = []

  @staticmethod
  def rect(x, y, width, height):
    return NS(x=x, y=y, width=width, height=height)

  def widget(self, name, rect, **attrs):
    widget = self.classes[name]()
    widget.rect = rect
    widget.__dict__.update(attrs)
    return widget

  def test_category_outline_matches_painted_background_and_moves_with_scroll(self):
    widget = self.widget("NavButton", self.rect(0, 200, 400, 110), parent=NS(set_current_panel=Mock()), panel_type=99,
                         container_rect=self.rect(40, 999, 450, 110))
    self.input.register_sunnypilot(widget)
    target = self.input.targets[0]
    self.assertEqual(target.bounds, self.rect(40, 200, 450, 110))
    self.assertEqual(target.rect, widget.rect)
    target.callback()
    widget.parent.set_current_panel.assert_called_once_with(99)

  def test_toggle_outline_uses_track_height_not_taller_touch_rect(self):
    widget = self.widget("ToggleSP", self.rect(550, 220, 210, 120))
    self.input.register_sunnypilot(widget)
    target = self.input.targets[0]
    self.assertEqual(target.bounds, self.rect(550, 220, 210, 100))
    self.assertEqual(target.rect, widget.rect)

  def test_segments_have_no_stock_spacing_and_skip_disabled_choices(self):
    widget = self.widget("MultipleButtonActionSP", self.rect(550, 200, 940, 170), buttons=["A", "B", "C"],
                         button_width=300, enabled_buttons={0, 2})
    self.input.register_sunnypilot(widget)
    self.assertEqual([t.rect for t in self.input.targets], [self.rect(550, 225, 300, 120), self.rect(1150, 225, 300, 120)])
    self.assertEqual([t.key[1] for t in self.input.targets], [0, 2])

  def test_description_focus_excludes_left_toggle_and_its_padding(self):
    toggle = self.widget("ToggleAction", self.rect(550, 200, 210, 120))
    widget = self.widget("ListItemSP", self.rect(530, 180, 1000, 400), action_item=toggle, description="Details", inline=True)
    self.input.register_sunnypilot(widget)
    self.assertEqual(self.input.targets[0].rect, self.rect(790, 180, 720, 170))
