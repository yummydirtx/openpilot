"""Native four UI adapter. The supervisor must be installed for controls to appear."""

import time
import subprocess
from pathlib import Path

from openpilot.system.ui.lib.display_handoff import HandoffClient


class NativeDisplayHandoff:
  def __init__(self, app, ui_state):
    self.app, self.ui_state = app, ui_state
    self._starter = None
    enabled = Path("/data/automaxxing/native-ui-enabled").is_file()
    self.client = HandoffClient(starter=self.start_supervisor if enabled else None)
    app.display_handoff = self.client
    app.input_filter = self.before_frame

  def start_supervisor(self):
    if self._starter is None or self._starter.poll() is not None:
      self._starter = subprocess.Popen(["sudo", "-n", "/usr/local/venv/bin/python",
                                        "/data/automaxxing/tools/android_auto/install_ui.py", "start-control"],
                                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

  def before_frame(self, events):
    if self._starter is not None and self._starter.poll() is not None:
      if self._starter.returncode:
        self.client.select("local")
      self._starter = None
    sm = self.ui_state.sm
    critical = (sm.valid["selfdriveState"] and sm["selfdriveState"].alertStatus.raw == 2)
    # A stalled subscription must not hide the only locally monitored display.
    if self.ui_state.started:
      critical |= (not sm.alive["selfdriveState"] or not sm.valid["selfdriveState"]
                   or time.monotonic() - sm.recv_time["selfdriveState"] > 0.5)
    was_suppressed = self.client.suppressed
    events = self.client.tick(events, critical=critical)
    self.app.projection_suppressed = self.client.suppressed
    if was_suppressed and not self.client.suppressed:
      from openpilot.selfdrive.ui.ui_state import device
      device.wake_for_projection_return()
    return events
