from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from tools.android_auto.native_identity import native_identity


class TestNativeIdentity(unittest.TestCase):
  def test_unprivileged_ui_is_left_alone(self):
    with patch("os.geteuid", return_value=1000), patch("os.setuid") as setuid:
      native_identity()
      setuid.assert_not_called()

  def test_groups_and_uid_are_dropped_before_frontend_can_write_params(self):
    calls = Mock()
    user = SimpleNamespace(pw_name="comma", pw_uid=1000, pw_gid=1000, pw_dir="/home/comma")
    with patch("os.geteuid", return_value=0), patch("pwd.getpwnam", return_value=user), patch.dict("os.environ"), \
         patch("os.initgroups", side_effect=calls.groups), patch("os.setgid", side_effect=calls.gid), \
         patch("os.setuid", side_effect=calls.uid), patch("os.umask", side_effect=calls.mask):
      native_identity()
    self.assertEqual([c[0] for c in calls.mock_calls], ["groups", "gid", "uid", "mask"])
    calls.uid.assert_called_once_with(1000)
