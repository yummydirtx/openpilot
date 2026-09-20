"""Render the complete installed 3X frontend using its original GPU widgets.

This module is imported only after BIG=1 in the isolated worker. It does not
open a physical window, poll touch hardware, publish uiDebug, or manage display
power. Original widget callbacks retain their normal offroad/engagement gates.
"""

import math
import os

from tools.android_auto.native_egl import HeadlessContext


class NativeFrame:
  def __init__(self, rl, image):
    self.rl, self.image = rl, image
    self.buffer = rl.ffi.buffer(image.data, image.width * image.height * 4)

  def pil(self):
    from PIL import Image
    return Image.frombytes("RGBA", (self.image.width, self.image.height), bytes(self.buffer)).convert("RGB")

  def close(self):
    self.rl.unload_image(self.image)


class NativeRenderer:
  def __init__(self, viewport):
    if os.geteuid() == 0:
      raise RuntimeError("Native frontend must run as comma, not root")
    self.viewport = viewport
    self.context = HeadlessContext(viewport.width, viewport.height)
    import pyray as rl
    from openpilot.system.ui.lib.application import gui_app
    from openpilot.selfdrive.ui.ui_state import ui_state
    from openpilot.selfdrive.ui.layouts.main import MainLayout, MainState
    self.rl, self.app, self.state = rl, gui_app, ui_state
    self.command = None
    self.app._width, self.app._height = round(viewport.logical_width), 1080
    self.app._scale = viewport.scale
    self.app._target_fps = 30
    self.app._load_fonts()
    self.app._patch_text_functions()
    self.app._patch_scissor_mode()
    self.state.update_params()
    self.main = MainLayout(bookmark_callback=lambda: self.request_command("bookmark"))
    self._add_display_panel()
    self.road = self.main._layouts[MainState.ONROAD]
    self.texture = rl.load_render_texture(viewport.width - viewport.margin_width, viewport.height - viewport.margin_height)
    self.output = rl.load_render_texture(viewport.width, viewport.height)
    from tools.android_auto.native_input import NativeInput
    self.input = NativeInput(self.app, self.main, self.request_command)
    # These two requested presentation changes are scoped to projection.
    draw_path = self.road.model_renderer._draw_path

    def engaged_path(sm):
      if self.state.engaged and sm.alive["selfdriveState"] and sm.valid["selfdriveState"]:
        draw_path(sm)

    self.road.model_renderer._draw_path = engaged_path
    wheel = self.road._hud_renderer._exp_button
    original_wheel = wheel._render

    def rotating_wheel(rect):
      if wheel._held_or_actual_mode():
        original_wheel(rect)
        return
      cs = self.state.sm["carState"]
      angle = float(cs.steeringAngleDeg)
      if not math.isfinite(angle) or not self.state.sm.valid["carState"]:
        angle = 0
      cx, cy = rect.x + rect.width / 2, rect.y + rect.height / 2
      texture = wheel._txt_wheel
      wheel._white_color.a = 180 if wheel.is_pressed or not wheel._engageable else 255
      rl.draw_circle(int(cx), int(cy), rect.width / 2, wheel._black_bg)
      rl.draw_texture_pro(texture, rl.Rectangle(0, 0, texture.width, texture.height),
                          rl.Rectangle(cx, cy, texture.width, texture.height),
                          rl.Vector2(texture.width / 2, texture.height / 2), -angle, wheel._white_color)

    wheel._render = rotating_wheel

  def _add_display_panel(self):
    from openpilot.selfdrive.ui.layouts.main import MainState
    from openpilot.selfdrive.ui.layouts.settings.settings import PanelInfo
    from openpilot.system.ui.widgets import Widget
    from openpilot.system.ui.widgets.list_view import button_item
    from openpilot.system.ui.widgets.scroller_tici import Scroller
    if self.app.sunnypilot_ui():
      from openpilot.selfdrive.ui.sunnypilot.layouts.settings.settings import PanelInfo
    owner = self

    class DisplayPanel(Widget):
      def __init__(self):
        super().__init__()
        self.scroller = Scroller([
          button_item("Road view", "OPEN", callback=owner.main._set_mode_for_state),
          button_item("Mazda Connect", "OPEN", callback=lambda: owner.request_command("exit")),
          button_item("Use comma display", "SWITCH", callback=lambda: owner.request_command("local")),
        ])

      def _render(self, rect):
        self.scroller.render(rect)

    settings = self.main._layouts[MainState.SETTINGS]
    settings._panels = {99: PanelInfo("Android Auto", DisplayPanel()), **settings._panels}

  def request_command(self, command):
    self.command = command

  def render(self, actions=(), *, raw=False):
    rl = self.rl
    self.state.update(update_display=False)
    self.input.handle(actions)
    self.input.begin()
    rl.begin_texture_mode(self.texture)
    rl.clear_background(rl.BLACK)
    rl.rl_push_matrix()
    rl.rl_scalef(self.viewport.scale, self.viewport.scale, 1.)
    # Same stack and native widgets as ui.py; no hand-drawn replacement menu.
    for tick in self.app._nav_stack_ticks:
      tick()
    self.app._nav_stack[-1].render(rl.Rectangle(0, 0, self.app.width, self.app.height))
    self.input.finish()
    rl.rl_pop_matrix()
    rl.end_texture_mode()
    rl.begin_texture_mode(self.output)
    rl.clear_background(rl.BLACK)
    ox, oy = self.viewport.offset
    w, h = self.texture.texture.width, self.texture.texture.height
    # Flip on the GPU so glReadPixels produces a top-down encoder buffer.
    rl.draw_texture_pro(self.texture.texture, rl.Rectangle(0, 0, w, h), rl.Rectangle(ox, oy, w, h),
                        rl.Vector2(0, 0), 0, rl.WHITE)
    rl.end_texture_mode()
    frame = NativeFrame(rl, rl.load_image_from_texture(self.output.texture))
    self.app._frame += 1
    if raw:
      return frame
    try:
      return frame.pil()
    finally:
      frame.close()

  def metadata(self, captured_at):
    sm = self.state.sm
    services = ["carState", "selfdriveState", "selfdriveStateSP"] if self.state.started else []
    from openpilot.selfdrive.ui.layouts.main import MainState
    if self.state.started and self.main._current_mode == MainState.ONROAD:
      from openpilot.cereal.visionipc import VisionStreamType
      narrow = self.road.stream_type == VisionStreamType.VISION_STREAM_NARROW_ROAD
      services += ["modelV2", "narrowRoadCameraState" if narrow else "wideRoadCameraState"]
    ages = [max(0., captured_at - sm.recv_time[s], captured_at - sm.logMonoTime[s] / 1e9) for s in services]
    missing = [s for s in services if not sm.valid[s] or not sm.alive[s]]
    if not sm.valid["deviceState"] or not sm.alive["deviceState"] or captured_at - sm.recv_time["deviceState"] > 1.5:
      missing.append("deviceState")
    if self.state.started and self.main._current_mode == MainState.ONROAD:
      if self.road.frame is None:
        missing.append("camera")
      else:
        ages.append(max(0., captured_at - self.road.client.timestamp_eof / 1e9))
    age = max(ages, default=0.)
    return {"captured_at": captured_at, "stale": bool(missing) or age > .35,
            "status": self.state.status.value, "source_age_seconds": age, "display_source_age_seconds": age,
            "missing_services": missing, "native_ui": True, "ui_command": self.command}

  def close(self):
    self.input.close()
    self.road.close()
    self.rl.unload_render_texture(self.texture)
    self.rl.unload_render_texture(self.output)
    self.context.close()
