"""Rotary focus for the native 3X widget tree, confined to the AA worker.

Targets come from widgets actually rendered in the active navigation stack.
Activation runs the target's original press/release handling; native enabled
and visibility conditions remain in force. No physical input is injected.
"""

from dataclasses import dataclass


@dataclass
class Target:
  key: tuple
  rect: object
  widget: object
  scrollers: tuple
  callback: object = None


class NativeInput:
  def __init__(self, app, main, command):
    import pyray as rl
    from openpilot.system.ui.widgets import Widget
    self.rl, self.app, self.main, self.command, self.Widget = rl, app, main, command, Widget
    self.targets, self.previous, self.parents = [], [], []
    self.selected = None
    self.visible = False
    render = Widget.render

    def observe(widget, rect=None):
      self.parents.append(widget)
      try:
        result = render(widget, rect)
        if widget.is_visible and all(p.enabled and p.is_visible for p in self.parents):
          self.register(widget)
        return result
      finally:
        self.parents.pop()

    Widget.render = observe
    self.original_render = render

  def begin(self):
    self.targets = []
    self.app._mouse_events = []

  def add(self, widget, rect, part=0, callback=None):
    if rect.width <= 0 or rect.height <= 0:
      return
    scrollers = tuple(p for p in self.parents if hasattr(p, "scroll_panel"))
    self.targets.append(Target((id(widget), part), self.rl.Rectangle(rect.x, rect.y, rect.width, rect.height), widget, scrollers, callback))

  def register(self, widget):
    from openpilot.system.ui.widgets.list_view import ListItem, MultipleButtonAction, BUTTON_HEIGHT, RIGHT_ITEM_PADDING
    from openpilot.selfdrive.ui.layouts.settings.settings import SettingsLayout
    from openpilot.selfdrive.ui.layouts.sidebar import Sidebar, SETTINGS_BTN, HOME_BTN
    from openpilot.selfdrive.ui.onroad.augmented_road_view import AugmentedRoadView
    r = widget.rect
    if isinstance(widget, SettingsLayout):
      self.add(widget, widget._close_btn_rect, "close", widget._close_callback)
    elif hasattr(widget, "panel_type") and hasattr(widget, "panel_info"):
      self.add(widget, r, callback=lambda w=widget: w.parent.set_current_panel(w.panel_type))
    elif isinstance(widget, Sidebar):
      self.add(widget, SETTINGS_BTN, "settings", widget._on_settings_click)
      self.add(widget, HOME_BTN, "bookmark", widget._on_flag_click)
    elif isinstance(widget, AugmentedRoadView):
      return
    elif isinstance(widget, ListItem):
      if widget.description:
        action = widget.get_right_item_rect(r) if widget.action_item else None
        width = action.x - r.x if action else r.width
        self.add(widget, self.rl.Rectangle(r.x, r.y, width, min(r.height, 170)), "description")
    elif isinstance(widget, MultipleButtonAction):
      for i in range(len(widget.buttons)):
        rect = self.rl.Rectangle(r.x + i * (widget.button_width + RIGHT_ITEM_PADDING),
                                 r.y + (r.height - BUTTON_HEIGHT) / 2, widget.button_width, BUTTON_HEIGHT)
        self.add(widget, rect, i)
    elif (widget._click_callback is not None or type(widget)._handle_mouse_release is not self.Widget._handle_mouse_release):
      self.add(widget, r)

  def finish(self):
    # Keep native layout order within columns; include offscreen scroller items
    # so rotating can reach every category/setting, not only the visible ones.
    self.previous = sorted(self.targets, key=lambda t: (int(t.rect.x >= 500), t.rect.y, t.rect.x))
    target = next((t for t in self.previous if t.key == self.selected), None)
    if target is None:
      self.selected = self.previous[0].key if self.previous else None
      target = self.previous[0] if self.previous else None
    if self.visible and target:
      r = target.rect
      for parent in target.scrollers:
        r = self.rl.get_collision_rec(r, parent.rect)
      if r.width > 0 and r.height > 0:
        self.rl.draw_rectangle_rounded_lines_ex(r, .08, 8, 5, self.rl.Color(75, 190, 255, 255))

  def reveal(self, target):
    for parent in reversed(target.scrollers):
      panel, bounds, r = parent.scroll_panel, parent.rect, target.rect
      delta = min(0, bounds.y + bounds.height - r.y - r.height - 8)
      if r.y < bounds.y + 8:
        delta = bounds.y + 8 - r.y
      if delta:
        panel.set_offset(panel.offset + delta)

  def activate(self, target):
    if not target.widget.enabled or not target.widget.is_visible:
      return
    if target.callback is not None:
      target.callback()
    else:
      import time
      from openpilot.system.ui.lib.application import MouseEvent, MousePos
      r = target.rect
      pos = MousePos(r.x + r.width / 2, r.y + r.height / 2)
      # Deliver to this widget only. Ancestor widgets must not interpret the
      # same knob click as a sidebar toggle or an unrelated list description.
      self.app._mouse_events = [MouseEvent(pos, 0, True, False, True, time.monotonic()),
                                MouseEvent(pos, 0, False, True, False, time.monotonic())]
      target.widget._process_mouse_events()
      self.app._mouse_events = []

  def handle(self, actions):
    from openpilot.selfdrive.ui.layouts.main import MainState
    for name, steps in actions:
      if name in ("music", "navigation"):
        self.command("exit")
        continue
      if name == "back":
        if len(self.app._nav_stack) > 1:
          self.app.pop_widget()
        elif self.main._current_mode == MainState.SETTINGS:
          self.main._set_mode_for_state()
        else:
          self.command("exit")
        self.selected = None
        continue
      if name == "home":
        while len(self.app._nav_stack) > 1:
          self.app.pop_widget()
        self.main._set_mode_for_state()
        self.visible = False
        continue
      if name not in ("menu", "select", "rotate", "up", "down", "left", "right"):
        continue
      if len(self.app._nav_stack) == 1 and self.main._current_mode == MainState.ONROAD:
        self.main.open_settings(99)
        self.selected = None
        self.visible = True
        # A click that opens settings must never activate its first item.
        break
      self.visible = True
      if not self.previous:
        continue
      current = next((i for i, t in enumerate(self.previous) if t.key == self.selected), 0)
      if name == "select":
        self.activate(self.previous[current])
        # Rendering refreshes the target set before any subsequent activation.
        break
      if name in ("left", "right"):
        origin = self.previous[current].rect
        candidates = [(i, t) for i, t in enumerate(self.previous)
                      if (t.rect.x - origin.x) * (1 if name == "right" else -1) > 100]
        if candidates:
          current = min(candidates, key=lambda pair: abs(pair[1].rect.y - origin.y) + .25 * abs(pair[1].rect.x - origin.x))[0]
      else:
        delta = steps if name == "rotate" else -1 if name == "up" else 1
        current = min(len(self.previous) - 1, max(0, current + delta))
      self.selected = self.previous[current].key
      self.reveal(self.previous[current])

  def close(self):
    self.Widget.render = self.original_render
