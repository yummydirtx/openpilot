import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from tools.android_auto import install_ui


class TestNativeInstall(unittest.TestCase):
  def setUp(self):
    self.temp = TemporaryDirectory()
    self.addCleanup(self.temp.cleanup)
    self.root = Path(self.temp.name)
    self.checkout = self.root / "checkout"
    self.source = self.root / "source"
    self.checkout.mkdir()
    self.source.mkdir()
    self.manifest = {}
    for i, relative in enumerate(install_ui.FILES):
      source = self.source / relative
      source.parent.mkdir(parents=True, exist_ok=True)
      source.write_text("changed = True\n")
      original = self.checkout / relative
      original.parent.mkdir(parents=True, exist_ok=True)
      if i % 2:
        original.write_text("changed = False\n")
      self.manifest[relative] = {"before": install_ui.digest(original), "after": install_ui.digest(source)}
    (self.source / "manifest.json").write_text(json.dumps(self.manifest))
    patches = patch.multiple(install_ui, CHECKOUT=self.checkout, BACKUP=self.root / "backup",
                             SERVICE=self.root / "service", ENABLED=self.root / "enabled")
    patches.start()
    self.addCleanup(patches.stop)
    for name in ("verify_parked", "start_control"):
      p = patch.object(install_ui, name)
      p.start()
      self.addCleanup(p.stop)
    p = patch.object(install_ui.subprocess, "run")
    p.start()
    self.addCleanup(p.stop)

  def test_round_trip_restores_exact_originals_and_removes_added_files(self):
    install_ui.install(self.source)
    for relative, hashes in self.manifest.items():
      self.assertEqual(install_ui.digest(self.checkout / relative), hashes["after"])
    install_ui.rollback()
    for relative, hashes in self.manifest.items():
      self.assertEqual(install_ui.digest(self.checkout / relative), hashes["before"])
    self.assertFalse(install_ui.BACKUP.exists())

  def test_mismatched_checkout_is_rejected_before_any_write(self):
    target = self.checkout / install_ui.FILES[1]
    target.write_text("user_edit = True\n")
    with self.assertRaisesRegex(RuntimeError, "changed"):
      install_ui.install(self.source)
    self.assertEqual(target.read_text(), "user_edit = True\n")
    self.assertFalse(install_ui.BACKUP.exists())

  def test_failed_service_install_rolls_back_native_files(self):
    install_ui.start_control.side_effect = OSError("read-only service location")
    with self.assertRaises(OSError):
      install_ui.install(self.source)
    for relative, hashes in self.manifest.items():
      self.assertEqual(install_ui.digest(self.checkout / relative), hashes["before"])

  def test_rollback_preserves_subsequent_user_edit(self):
    install_ui.install(self.source)
    target = self.checkout / install_ui.FILES[0]
    target.write_text("user_edit = True\n")
    with self.assertRaisesRegex(RuntimeError, "later native edit"):
      install_ui.rollback()
    self.assertEqual(target.read_text(), "user_edit = True\n")

  def test_unknown_paths_and_uncompilable_bundle_are_rejected(self):
    self.manifest["../outside.py"] = {}
    (self.source / "manifest.json").write_text(json.dumps(self.manifest))
    with self.assertRaises(ValueError):
      install_ui.install(self.source)
    del self.manifest["../outside.py"]
    bad = "syntax error !\n"
    (self.source / install_ui.FILES[0]).write_text(bad)
    self.manifest[install_ui.FILES[0]]["after"] = hashlib.sha256(bad.encode()).hexdigest()
    (self.source / "manifest.json").write_text(json.dumps(self.manifest))
    with self.assertRaises(SyntaxError):
      install_ui.install(self.source)
    self.assertFalse(install_ui.BACKUP.exists())
