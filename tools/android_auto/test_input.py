import tempfile
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

from tools.android_auto.input import InputAction, InputDecoder, repeated_integers
from tools.android_auto.menu import ProjectionMenu
from tools.android_auto.session import field, varint
from tools.android_auto.test_live_session import acknowledge, grant_focus, session, KEYFRAME


def button(code, pressed=True, long=False):
  return field(4, field(1, field(1, code) + field(2, int(pressed)) + field(4, int(long))))


def rotate(delta):
  return field(6, field(1, field(1, 65536) + field(2, delta % (1 << 64))))


class TestInput(unittest.TestCase):
  def test_packed_capabilities_and_malformed_varint(self):
    self.assertEqual(repeated_integers([3, varint(65536) + varint(23)]), [3, 65536, 23])
    with self.assertRaises(ValueError):
      repeated_integers([b"\x80"])

  def test_signed_rotation_is_bounded(self):
    d = InputDecoder()
    for amount, expected in ((-1, -1), (5, 5), (-400, -20), (400, 20)):
      self.assertEqual(d.decode(rotate(amount)), [InputAction("rotate", expected)])

  def test_click_repeat_release_and_long_press(self):
    d = InputDecoder()
    self.assertEqual(d.decode(button(23)), [InputAction("select")])
    self.assertEqual(d.decode(button(23)), [])
    self.assertEqual(d.decode(button(23, False)), [])
    self.assertEqual(d.decode(button(23)), [InputAction("select")])
    self.assertEqual(d.decode(button(3, long=True)), [])

  def test_unknown_and_oversized_events(self):
    d = InputDecoder()
    self.assertEqual(d.decode(button(999)), [])
    with self.assertRaises(ValueError):
      d.decode(button(23) * 65)
    with self.assertRaises(ValueError):
      d.decode(b"\0" * 16385)

  def test_open_and_bind_while_video_messages_arrive(self):
    s = session()
    s.channels = [{"id": 2, "input_keycodes": [3, 4, 23, 65536, 999]}]
    s.receive = Mock(side_effect=[(0, 11, field(1, 55)), (2, 8, field(1, 0)),
                                 (9, 0x8008, field(1, 1)), (2, 0x8003, field(1, 0))])
    with patch("tools.android_auto.live_session.select.select", return_value=([s.peer], [], [])):
      s.open_input()
    s.send.assert_any_call(0, 12, field(1, 55))
    s.send.assert_any_call(2, 0x8002, b"".join(field(1, c) for c in (3, 4, 23, 65536)))

  def test_input_focus_and_release(self):
    s = session()
    s.input_channel = 2
    grant_focus(s)
    s.handle(2, 0x8001, button(23))
    self.assertEqual(list(s.input_actions), [InputAction("select")])
    grant_focus(s, 2)
    self.assertFalse(s.input_actions)
    s.handle(2, 0x8001, rotate(1))
    self.assertFalse(s.input_actions)
    grant_focus(s)
    s.handle(2, 0x8001, button(23))
    self.assertEqual(list(s.input_actions), [InputAction("select")])

  def test_local_choice_rejects_late_focus_until_explicit_request(self):
    s = session()
    grant_focus(s)
    s.send_frame(KEYFRAME, 0)
    s.request_native(resume_from_head_unit=False)
    s.send.assert_called_with(9, 0x8007, field(2, 2) + field(3, 4))
    for mode in (1, 2, 1):
      s.handle(9, 0x8008, field(1, mode) + field(2, 1))
      self.assertFalse(s.focused)
    acknowledge(s)
    s.request_projection()
    grant_focus(s)
    self.assertTrue(s.focused)
    self.assertTrue(s.needs_keyframe)
    self.assertEqual(s.epoch_acked, 0)

  def test_oem_exit_requires_unfocus_then_unrequested_grant_to_resume(self):
    s = session()
    grant_focus(s)
    s.request_native()
    s.handle(9, 0x8008, field(1, 1) + field(2, 1))
    self.assertFalse(s.focused)
    grant_focus(s, 2)
    grant_focus(s)
    self.assertFalse(s.focused)
    s.handle(9, 0x8008, field(1, 1) + field(2, 1))
    self.assertTrue(s.focused)


class TestMenu(unittest.TestCase):
  def test_selection_and_back_do_not_execute_on_first_click(self):
    m = ProjectionMenu()
    self.assertIsNone(m.handle(InputAction("select")))
    self.assertTrue(m.opened)
    m.handle(InputAction("rotate", 1))
    m.handle(InputAction("select"))
    self.assertEqual(m.view, "hud")
    self.assertFalse(m.opened)
    m.handle(InputAction("back"))
    self.assertEqual(m.selected, 3)
    self.assertEqual(m.handle(InputAction("select")), "exit")

  def test_four_directions_bounds_home_and_shortcuts(self):
    m = ProjectionMenu(opened=True)
    for name in ("down", "right"):
      m.handle(InputAction(name))
    self.assertEqual(m.selected, 2)
    for name in ("up", "left"):
      m.handle(InputAction(name))
    self.assertEqual(m.selected, 0)
    m.handle(InputAction("rotate", -20))
    self.assertEqual(m.selected, 0)
    m.handle(InputAction("rotate", 20))
    self.assertEqual(m.selected, 3)
    m.handle(InputAction("home"))
    self.assertFalse(m.opened)
    self.assertEqual(m.handle(InputAction("music")), "exit")
    self.assertEqual(m.handle(InputAction("navigation")), "exit")

  def test_overlay_keeps_cached_frame_and_margins_unchanged(self):
    from PIL import Image
    from tools.android_auto.menu import draw_menu
    from tools.android_auto.viewport import Viewport
    assets = Path(__file__).resolve().parents[2] / ".cache/automaxxing/ui-assets"
    if not (assets / "fonts/Inter-Medium.ttf").exists():
      self.skipTest("Native assets not prepared")
    original = Image.new("RGB", (1280, 720))
    image = draw_menu(original, Viewport(1280, 720, 0, 240), ProjectionMenu(opened=True, selected=3).snapshot(), assets)
    self.assertIsNone(original.getbbox())
    self.assertIsNone(image.crop((0, 0, 1280, 120)).getbbox())
    self.assertIsNone(image.crop((0, 600, 1280, 720)).getbbox())
    with tempfile.TemporaryDirectory() as tmp:
      image.save(Path(tmp) / "menu.png")
