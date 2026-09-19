import struct
import unittest

from tools.android_auto.probe import probe, read_control


class FragmentedPeer:
  def __init__(self, wire):
    self.wire = wire
    self.sent = b""

  def recv(self, size):
    # A real TCP read can return fewer bytes than requested, even for a header.
    part, self.wire = self.wire[:1], self.wire[1:]
    return part

  def sendall(self, data):
    self.sent += data


class TestProbe(unittest.TestCase):
  def test_partial_reads_and_coalesced_messages(self):
    request = bytes.fromhex("00 03 00 06 00 01 00 01 00 07")
    hello = bytes.fromhex("00 03 00 0b 00 03 16 03 03 00 04 01 00 00 00")
    peer = FragmentedPeer(request + hello)
    result = probe(peer)
    self.assertEqual(peer.sent, bytes.fromhex("00 03 00 08 00 02 00 01 00 05 00 00"))
    self.assertFalse(result["authentication_complete"])
    self.assertFalse(result["video_tested"])
    self.assertEqual(result["head_unit_version"], [1, 7])

  def test_truncated_header_and_payload(self):
    for wire in (b"", b"\x00\x03", bytes.fromhex("00 03 00 06 00 01")):
      with self.subTest(wire=wire), self.assertRaises(EOFError):
        read_control(FragmentedPeer(wire))

  def test_reject_unsupported_frames(self):
    for channel, flags, size in ((1, 3, 2), (0, 11, 2), (0, 1, 2), (0, 3, 1)):
      with self.subTest(channel=channel, flags=flags, size=size), self.assertRaises(ValueError):
        read_control(FragmentedPeer(struct.pack(">BBH", channel, flags, size)))

  def test_reject_wrong_message_and_version(self):
    for wire in ("00 03 00 02 00 0b", "00 03 00 06 00 01 00 02 00 00"):
      with self.subTest(wire=wire), self.assertRaises(ValueError):
        probe(FragmentedPeer(bytes.fromhex(wire)))


if __name__ == "__main__":
  unittest.main()
