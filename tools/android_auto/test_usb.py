import tempfile
from pathlib import Path
import unittest
from unittest.mock import call, patch

from tools.android_auto.usb import Gadget, write_all


class TestAccessoryTransport(unittest.TestCase):
  def test_partial_writes_preserve_every_byte(self):
    received = bytearray()

    def partial_write(fd, data):
      self.assertEqual(fd, 42)
      size = min(3, len(data))
      received.extend(data[:size])
      return size

    with patch("tools.android_auto.usb.os.write", side_effect=partial_write):
      write_all(42, b"an Android Auto frame")
    self.assertEqual(received, b"an Android Auto frame")

  def test_zero_write_fails_instead_of_spinning(self):
    with patch("tools.android_auto.usb.os.write", return_value=0), self.assertRaises(OSError):
      write_all(42, b"frame")

  def test_existing_gadget_is_never_modified(self):
    with tempfile.TemporaryDirectory() as directory:
      root = Path(directory)
      (root / "user-gadget").mkdir()
      gadget = Gadget(root)
      with self.assertRaisesRegex(RuntimeError, "existing gadget"):
        gadget.setup()
      gadget.cleanup()
      self.assertEqual(list(root.iterdir()), [root / "user-gadget"])

  def test_partial_creation_cleanup_is_reverse_order(self):
    with tempfile.TemporaryDirectory() as directory:
      root = Path(directory)
      gadget = Gadget(root)
      gadget.mkdir("")
      gadget.mkdir("partially-created-function")
      gadget.cleanup()
      self.assertEqual(list(root.iterdir()), [])
      gadget.cleanup()

  def test_unbind_before_removing_functions(self):
    gadget = Gadget()
    gadget.bound = gadget.linked = True
    gadget.created = [gadget.path, gadget.path / "functions/accessory.0"]
    operations = []
    with patch.object(gadget, "write", side_effect=lambda *args: operations.append(("write", args))), \
         patch.object(Path, "unlink", side_effect=lambda: operations.append(("unlink",))), \
         patch.object(Path, "rmdir", autospec=True) as remove:
      gadget.cleanup()
    self.assertEqual(operations, [("write", ("UDC", "")), ("unlink",)])
    self.assertEqual(remove.call_args_list, [call(gadget.path / "functions/accessory.0"), call(gadget.path)])

  def test_failed_unbind_does_not_remove_a_live_function(self):
    gadget = Gadget()
    gadget.bound = gadget.linked = True
    gadget.created = [gadget.path]
    with patch.object(gadget, "write", side_effect=OSError("still busy")), \
         patch.object(Path, "unlink") as unlink, patch.object(Path, "rmdir") as remove, self.assertRaises(OSError):
      gadget.cleanup()
    unlink.assert_not_called()
    remove.assert_not_called()
    self.assertTrue(gadget.bound)


if __name__ == "__main__":
  unittest.main()
