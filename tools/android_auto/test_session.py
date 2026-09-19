import io
import json
import struct
from types import SimpleNamespace
import unittest

from tools.android_auto.session import AuthenticationRejected, MAX_MESSAGE, Session, field, one, parse_fields, varint
from tools.android_auto.test_probe import FragmentedPeer


def frame(channel, flags, body, total=None):
  header = struct.pack(">BBH", channel, flags, len(body))
  return header + (struct.pack(">I", total) if total is not None else b"") + body


def receiver(wire):
  # Exercise plaintext frame assembly without creating TLS credentials.
  session = object.__new__(Session)
  session.peer = FragmentedPeer(wire)
  session.log = io.StringIO()
  session.fragments = {}
  session.peer_verification_enabled = False
  return session


class TestWireFormat(unittest.TestCase):
  def test_protobuf_repeated_and_nested_fields(self):
    encoded = field(1, field(1, 9)) + field(1, field(1, 12)) + field(4, "test") + field(5, 300)
    fields = parse_fields(encoded)
    self.assertEqual([one(parse_fields(c), 1) for c in fields[1]], [9, 12])
    self.assertEqual(one(fields, 4), b"test")
    self.assertEqual(one(fields, 5), 300)

  def test_reject_invalid_protobuf(self):
    for wire in (b"\x08\x80", b"\x0a\x05hi", b"\x00\x00", b"\x0b", b"\x08" + b"\xff" * 10):
      with self.subTest(wire=wire), self.assertRaises(ValueError):
        parse_fields(wire)
    for value in (-1, 1 << 64):
      with self.subTest(value=value), self.assertRaises(ValueError):
        varint(value)

  def test_fixed_width_unknown_fields(self):
    self.assertEqual(parse_fields(b"\x0d1234\x1112345678"), {1: [b"1234"], 2: [b"12345678"]})

  def test_fragmented_interleaved_channels(self):
    wire = frame(1, 1, b"\x80\x03ab", 8) + frame(2, 3, b"\x00\x0bping") + frame(1, 0, b"cd") + frame(1, 2, b"ef")
    session = receiver(wire)
    self.assertEqual(session.receive(), (2, 11, b"ping"))
    self.assertEqual(session.receive(), (1, 0x8003, b"abcdef"))
    self.assertEqual(session.fragments, {})

  def test_reject_broken_fragment_sequences(self):
    bad_sequences = [
      frame(1, 2, b"ab"),
      frame(1, 1, b"ab", MAX_MESSAGE + 1),
      frame(1, 1, b"ab", 2),
      frame(1, 1, b"ab", 4) + frame(1, 3, b"cd"),
      frame(1, 1, b"ab", 4) + frame(1, 2, b"c"),
      frame(1, 1, b"ab", 4) + frame(1, 2, b"cde"),
      frame(1, 1, b"ab", 4) + frame(1, 6, b"cd"),
    ]
    for wire in bad_sequences:
      with self.subTest(wire=wire), self.assertRaises(ValueError):
        receiver(wire).receive()

  def test_ping_during_wait(self):
    session = receiver(frame(0, 3, b"\x00\x0b" + field(1, 1234)) + frame(1, 3, b"\x00\x08" + field(1, 0)))
    replies = []
    session.send = lambda *args: replies.append(args)
    self.assertEqual(session.wait_for(1, 8), field(1, 0))
    self.assertEqual(replies, [(0, 12, field(1, 1234))])

  def test_outbound_fragmentation_and_reassembly(self):
    sender = receiver(b"")
    payload = bytes(range(256)) * 200
    sender.send(5, 0x8001, payload, encrypted=False, control=True)
    self.assertEqual(receiver(sender.peer.sent).receive(), (5, 0x8001, payload))

  def test_tls_success_does_not_hide_authentication_failure(self):
    wire = frame(0, 3, b"\x00\x01\x00\x01\x00\x07") + frame(0, 3, b"\x00\x03hello")
    wire += frame(0, 3, b"\x00\x04" + field(1, (1 << 64) - 3))
    session = receiver(wire)
    session.incoming = io.BytesIO()
    session.outgoing = io.BytesIO()
    session.tls = SimpleNamespace(do_handshake=lambda: None, version=lambda: "TLSv1.2", cipher=lambda: ("test",))
    with self.assertRaisesRegex(AuthenticationRejected, "status -3"):
      session.authenticate()
    self.assertIn('"event": "tls_established"', session.log.getvalue())
    self.assertNotIn('"event": "authenticated"', session.log.getvalue())

  def test_discovery_preserves_unknown_video_bytes_in_json(self):
    config = field(1, 1) + field(2, 1) + field(11, b"\x08\x01")
    descriptor = field(1, 2) + field(3, field(1, 3) + field(4, config))
    session = receiver(b"")
    session.send = lambda *args: None
    session.wait_for = lambda *args: field(1, descriptor)
    channels = session.discover()
    self.assertEqual(channels[0]["video_configs"][0][11], [{"hex": "0801"}])
    self.assertEqual(json.loads(session.log.getvalue())["event"], "discovered")
    json.dumps(channels)


if __name__ == "__main__":
  unittest.main()
