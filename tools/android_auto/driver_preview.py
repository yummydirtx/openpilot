"""C4 driver-monitoring preview adapted to the projected landscape frontend."""

import time
import uuid

import pyray as rl

from openpilot.cereal import log
from openpilot.selfdrive.ui.mici.onroad.cabin_camera_dialog import BaseCabinCameraDialog
from openpilot.selfdrive.ui.ui_state import ui_state
from openpilot.system.ui.lib.application import FontWeight, TextAlignment
from openpilot.system.ui.lib.driver_preview import preview_allowed, preview_offroad, monitoring_fresh
from openpilot.system.ui.lib.multilang import tr
from openpilot.system.ui.widgets import Widget
from openpilot.system.ui.widgets.button import Button, ButtonStyle
from openpilot.system.ui.widgets.label import gui_label

PREVIEW_IDLE_SECONDS = 300
SOURCE_MAX_AGE = .35


class DriverPreview(BaseCabinCameraDialog):
  def __init__(self, request):
    super().__init__()
    self.request = request
    self.active = False
    self.last_input_at = time.monotonic()
    self.reset_id = uuid.uuid4().hex
    self.camera_displayed = self.monitoring_displayed = False
    self.display_age = 0.
    self._back = Button(tr("Back"), click_callback=self.dismiss, font_size=44, button_style=ButtonStyle.TRANSPARENT_WHITE_TEXT)
    self._reset = Button(tr("Reset monitoring"), click_callback=self.reset_monitoring, font_size=40,
                         button_style=ButtonStyle.TRANSPARENT_WHITE_TEXT)
    self.driver_state_renderer.set_rect(rl.Rectangle(0, 0, 200, 200))
    self.driver_state_renderer.load_icons()

  def show_event(self):
    # The physical UI owns the camera parameter, timeout-independent lease,
    # distraction reset and sound publisher. Never invoke the C4 show hook here.
    Widget.show_event(self)
    self.active = True
    self.last_input_at = time.monotonic()

  def hide_event(self):
    Widget.hide_event(self)
    self.close()

  def close(self):
    if getattr(self, "active", False):
      self.request.update(False, self.reset_id)
      self.active = False
    camera = getattr(self, "_camera_view", None)
    if camera is not None:
      # CameraView registers a bound callback; remove it before releasing GL.
      callbacks = ui_state._offroad_transition_callbacks
      if camera._offroad_transition in callbacks:
        callbacks.remove(camera._offroad_transition)
      camera.close()
      self._camera_view = None

  def _handle_mouse_release(self, _):
    # Like the C4 preview, clicking the image clears the distraction lockout.
    self.reset_monitoring()

  def reset_monitoring(self):
    if preview_offroad(ui_state.sm, time.monotonic()):
      self.reset_id = uuid.uuid4().hex
    self.last_input_at = time.monotonic()

  def tick(self, actions=()):
    now = time.monotonic()
    if actions:
      self.last_input_at = now
    if not preview_allowed(ui_state.sm, now) or now - self.last_input_at >= PREVIEW_IDLE_SECONDS:
      self.dismiss()
      return
    self.request.update(True, self.reset_id)

  def _publish_alert_sound(self, _):
    # Audio is emitted on the C4 through DriverPreviewHost, never a second
    # selfdriveState publisher in the projection worker.
    pass

  @staticmethod
  def _overlay(rect):
    # Alpha blending only: keep the native camera visible without a blur pass.
    rl.draw_rectangle_rounded(rect, .18, 12, rl.Color(10, 14, 18, 145))

  def _render(self, rect):
    self.camera_displayed = self.monitoring_displayed = False
    self.display_age = 0.
    offroad = preview_offroad(ui_state.sm, time.monotonic())
    rl.draw_rectangle_rec(rect, rl.BLACK)
    driver_data = self._render_camera(rect, offroad)

    header = rl.Rectangle(rect.x + 28, rect.y + 28, 600, 82)
    self._overlay(header)
    self._back.render(rl.Rectangle(header.x, header.y, 190, header.height))
    gui_label(rl.Rectangle(header.x + 205, header.y, 375, header.height), tr("Driver camera"),
              font_size=42, font_weight=FontWeight.MEDIUM)
    reset_rect = rl.Rectangle(rect.x + rect.width - 448, rect.y + 28, 420, 82)
    self._overlay(reset_rect)
    self._reset.set_enabled(offroad)
    self._reset.set_text(tr("Reset monitoring") if offroad else tr("Live monitoring"))
    self._reset.render(reset_rect)

    panel = rl.Rectangle(rect.x + rect.width - 678, rect.y + rect.height - 278, 650, 250)
    self._overlay(panel)
    self._render_monitoring(panel, driver_data)

  def _render_camera(self, rect, offroad):
    # Cover the usable display with the C4 camera composition. Camera and face
    # box share one uniform transform; crop the overflow instead of stretching.
    scale = max(rect.width / 536, rect.height / 240)
    camera_rect = rl.Rectangle(rect.x + (rect.width - 536 * scale) / 2, rect.y + (rect.height - 240 * scale) / 2,
                               536 * scale, 240 * scale)
    if not self.active or self._camera_view is None:
      return
    now = time.monotonic()
    rl.begin_scissor_mode(int(rect.x), int(rect.y), int(rect.width), int(rect.height))
    self._camera_view._render(camera_rect)
    camera = self._camera_view
    camera_age = max(0., now - camera.client.timestamp_eof / 1e9) if camera.frame is not None else float("inf")
    self.camera_displayed = camera_age <= SOURCE_MAX_AGE
    if not self.camera_displayed:
      rl.draw_rectangle_rec(camera_rect, rl.BLACK)
      text = tr("Camera starting…") if camera.frame is None else tr("Camera unavailable")
      gui_label(rect, text, font_size=68, font_weight=FontWeight.MEDIUM, alignment=TextAlignment.CENTER)
    else:
      self.display_age = camera_age

    demo = offroad and ui_state.params.get_bool("IsDriverViewEnabled")
    self.monitoring_displayed = self.camera_displayed and monitoring_fresh(ui_state.sm, now, demo=demo)
    driver_data = None
    if self.monitoring_displayed:
      dm_age = max(max(0., now - ui_state.sm.recv_time[name], now - ui_state.sm.logMonoTime[name] / 1e9)
                   for name in ("driverMonitoringState", "driverStateV2"))
      self.display_age = max(camera_age, dm_age)
      rl.rl_push_matrix()
      rl.rl_translatef(camera_rect.x, camera_rect.y, 0)
      rl.rl_scalef(scale, scale, 1.)
      driver_data = super()._draw_face_detection(rl.Rectangle(0, 0, 536, 240))
      rl.rl_pop_matrix()
    rl.end_scissor_mode()

    return driver_data

  def _render_monitoring(self, panel, driver_data):
    if not self.monitoring_displayed:
      gui_label(panel, tr("Waiting for driver monitoring"), font_size=40, alignment=TextAlignment.CENTER)
      return

    self.driver_state_renderer.set_position(panel.x + 20, panel.y + 25)
    self.driver_state_renderer.render()
    detail = rl.Rectangle(panel.x + 240, panel.y + 15, 385, 100)
    if driver_data is not None:
      rl.rl_push_matrix()
      rl.rl_translatef(detail.x + (detail.width - 171 * .55) / 2, detail.y, 0)
      rl.rl_scalef(.55, .55, 1.)
      super()._draw_eyes(rl.Rectangle(0, 0, 536, 240), driver_data)
      rl.rl_pop_matrix()
    else:
      gui_label(detail, tr("Face not detected"), font_size=34, alignment=TextAlignment.CENTER)

    dm = ui_state.sm["driverMonitoringState"]
    vision = dm.activePolicy == log.DriverMonitoringState.MonitoringPolicy.vision
    awareness = dm.visionPolicyState.awarenessPercent if vision else dm.wheeltouchPolicyState.awarenessPercent
    gui_label(rl.Rectangle(detail.x, panel.y + 123, detail.width, 58), f"{tr('Awareness')}: {awareness:.0f}%",
              font_size=40, font_weight=FontWeight.MEDIUM, alignment=TextAlignment.CENTER)
    if dm.alertLevel != log.DriverMonitoringState.AlertLevel.none:
      alert = tr("Pay attention") if vision else tr("Touch wheel")
      gui_label(rl.Rectangle(detail.x, panel.y + 185, detail.width, 50), f"{alert} - {dm.alertLevel.raw}",
                font_size=34, alignment=TextAlignment.CENTER, color=rl.Color(255, 185, 80, 255))
