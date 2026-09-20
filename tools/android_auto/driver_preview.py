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
from openpilot.system.ui.widgets.button import Button
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
    self._back = Button(tr("Back"), click_callback=self.dismiss, font_size=54)
    self._reset = Button(tr("Reset monitoring"), click_callback=self.reset_monitoring, font_size=48)
    self.driver_state_renderer.set_rect(rl.Rectangle(0, 0, 360, 360))
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

  def _render(self, rect):
    self.camera_displayed = self.monitoring_displayed = False
    self.display_age = 0.
    rl.draw_rectangle_rec(rect, rl.Color(10, 10, 10, 255))
    self._back.render(rl.Rectangle(rect.x + 40, rect.y + 25, 260, 100))
    gui_label(rl.Rectangle(rect.x + 340, rect.y + 25, rect.width - 700, 100), tr("Driver camera"),
              font_size=68, font_weight=FontWeight.BOLD)

    # Keep the C4 preview's aspect/crop and mirror. Scale its face-box geometry
    # uniformly; reserve a separate landscape column for monitoring feedback.
    area = rl.Rectangle(rect.x + 40, rect.y + 160, rect.width - 680, rect.height - 220)
    scale = min(area.width / 536, area.height / 240)
    camera_rect = rl.Rectangle(area.x + (area.width - 536 * scale) / 2, area.y + (area.height - 240 * scale) / 2,
                               536 * scale, 240 * scale)
    panel = rl.Rectangle(rect.x + rect.width - 580, rect.y + 160, 540, rect.height - 220)
    rl.draw_rectangle_rounded(panel, .06, 12, rl.Color(25, 25, 25, 255))
    offroad = preview_offroad(ui_state.sm, time.monotonic())
    self._reset.set_enabled(offroad)
    self._reset.set_text(tr("Reset monitoring") if offroad else tr("Live monitoring"))
    self._reset.render(rl.Rectangle(panel.x + 25, panel.y + panel.height - 100, panel.width - 50, 90))

    if not self.active or self._camera_view is None:
      return
    now = time.monotonic()
    rl.begin_scissor_mode(int(camera_rect.x), int(camera_rect.y), int(camera_rect.width), int(camera_rect.height))
    self._camera_view._render(camera_rect)
    camera = self._camera_view
    camera_age = max(0., now - camera.client.timestamp_eof / 1e9) if camera.frame is not None else float("inf")
    self.camera_displayed = camera_age <= SOURCE_MAX_AGE
    if not self.camera_displayed:
      rl.draw_rectangle_rec(camera_rect, rl.BLACK)
      text = tr("Camera starting…") if camera.frame is None else tr("Camera unavailable")
      gui_label(camera_rect, text, font_size=68, font_weight=FontWeight.MEDIUM, alignment=TextAlignment.CENTER)
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

    if not self.monitoring_displayed:
      gui_label(rl.Rectangle(panel.x + 20, panel.y + 60, panel.width - 40, 500), tr("Waiting for\ndriver monitoring"),
                font_size=48, alignment=TextAlignment.CENTER)
      return

    if driver_data is not None:
      rl.rl_push_matrix()
      rl.rl_translatef(panel.x + (panel.width - 171 * 1.7) / 2, panel.y + 35, 0)
      rl.rl_scalef(1.7, 1.7, 1.)
      super()._draw_eyes(rl.Rectangle(0, 0, 536, 240), driver_data)
      rl.rl_pop_matrix()
    else:
      gui_label(rl.Rectangle(panel.x, panel.y + 30, panel.width, 120), tr("Face not detected"),
                font_size=44, alignment=TextAlignment.CENTER)

    self.driver_state_renderer.set_position(panel.x + (panel.width - 360) / 2, panel.y + 165)
    self.driver_state_renderer.render()
    dm = ui_state.sm["driverMonitoringState"]
    vision = dm.activePolicy == log.DriverMonitoringState.MonitoringPolicy.vision
    awareness = dm.visionPolicyState.awarenessPercent if vision else dm.wheeltouchPolicyState.awarenessPercent
    gui_label(rl.Rectangle(panel.x, panel.y + 550, panel.width, 70), f"{tr('Awareness')}: {awareness:.0f}%",
              font_size=52, font_weight=FontWeight.MEDIUM, alignment=TextAlignment.CENTER)
    if dm.alertLevel != log.DriverMonitoringState.AlertLevel.none:
      alert = tr("Pay attention") if vision else tr("Touch wheel")
      gui_label(rl.Rectangle(panel.x, panel.y + 630, panel.width, 70), f"{alert} · {dm.alertLevel}",
                font_size=42, alignment=TextAlignment.CENTER, color=rl.Color(255, 185, 80, 255))
