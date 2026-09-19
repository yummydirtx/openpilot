import io
import json
import socket
import ssl
import struct
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

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

  def test_aggregate_fragment_reservations_are_bounded(self):
    wire = b"".join(frame(channel, 1, b"\x80\x03", MAX_MESSAGE) for channel in (1, 2, 3))
    session = receiver(wire)
    with self.assertRaisesRegex(ValueError, "Aggregate fragmented"):
      session.receive()
    self.assertEqual(set(session.fragments), {1, 2})

  def test_completed_fragment_releases_aggregate_capacity(self):
    wire = frame(1, 1, b"\x80\x03", 4) + frame(2, 1, b"\x80\x04", 4) + frame(1, 2, b"ab")
    wire += frame(3, 1, b"\x80\x05", 4) + frame(2, 2, b"cd") + frame(3, 2, b"ef")
    session = receiver(wire)
    with patch("tools.android_auto.session.MAX_FRAGMENT_BYTES", 8):
      self.assertEqual(session.receive(), (1, 0x8003, b"ab"))
      self.assertEqual(session.receive(), (2, 0x8004, b"cd"))
      self.assertEqual(session.receive(), (3, 0x8005, b"ef"))
    self.assertEqual(session.fragments, {})

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
    self.assertFalse(session.authenticated)
    self.assertIn('"event": "tls_established"', session.log.getvalue())
    self.assertNotIn('"event": "authenticated"', session.log.getvalue())

  def test_auth_status_zero_enforces_encryption_on_following_messages(self):
    wire = frame(0, 3, b"\x00\x01\x00\x01\x00\x07") + frame(0, 3, b"\x00\x03hello")
    wire += frame(0, 3, b"\x00\x04" + field(1, 0)) + frame(0, 3, b"\x00\x0b" + field(1, 123))
    session = receiver(wire)
    session.incoming = io.BytesIO()
    session.outgoing = io.BytesIO()
    session.tls = SimpleNamespace(do_handshake=lambda: None, version=lambda: "TLSv1.2", cipher=lambda: ("test",))
    session.authenticate()
    self.assertTrue(session.authenticated)
    with self.assertRaisesRegex(ValueError, "Plaintext"):
      session.receive()
    with self.assertRaisesRegex(ValueError, "Plaintext"):
      session.send(0, 11, field(1, 123), encrypted=False)

  def test_authenticated_encrypted_message_is_decrypted(self):
    session = receiver(frame(0, 11, b"encrypted"))
    session.authenticated = True
    session.incoming = io.BytesIO()
    session.tls = SimpleNamespace(read=Mock(side_effect=[b"\x00\x0b" + field(1, 123), ssl.SSLWantReadError()]))
    self.assertEqual(session.receive(), (0, 11, field(1, 123)))

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


class TestReceiveDeadline(unittest.TestCase):
  def test_blocked_partial_header_restores_socket_timeout(self):
    for original_timeout in (None, 7.0):
      left, right = socket.socketpair()
      with self.subTest(original_timeout=original_timeout), left, right:
        left.settimeout(original_timeout)
        right.sendall(b"\x00\x0b")
        session = receiver(b"")
        session.peer = left
        session.authenticated = True
        session.receive_timeout = 0.02
        with self.assertRaises(TimeoutError):
          session.receive()
        self.assertEqual(left.gettimeout(), original_timeout)

  def test_socket_timeout_restored_after_success_and_protocol_error(self):
    for wire, valid in ((frame(0, 3, b"\x00\x0b"), True), (frame(0, 16, b"\x00\x0b"), False)):
      left, right = socket.socketpair()
      with self.subTest(valid=valid), left, right:
        left.settimeout(7)
        right.sendall(wire)
        session = receiver(b"")
        session.peer = left
        if valid:
          self.assertEqual(session.receive(), (0, 11, b""))
        else:
          with self.assertRaises(ValueError):
            session.receive()
        self.assertEqual(left.gettimeout(), 7)

  def test_partial_reads_and_fragments_share_one_deadline(self):
    session = receiver(frame(1, 1, b"\x80\x03", 4) + frame(1, 2, b"ab"))
    session.handshake_timeout = 0.6
    clock = [0.0]
    timeouts = []
    original_recv = session.peer.recv

    def receive_slow_byte(size):
      clock[0] += 0.05
      return original_recv(size)

    session.peer.recv = receive_slow_byte
    session.peer.gettimeout = lambda: 7.0
    session.peer.settimeout = timeouts.append
    with patch("tools.android_auto.session.time.monotonic", side_effect=lambda: clock[0]):
      with self.assertRaisesRegex(TimeoutError, "deadline"):
        session.receive()
    self.assertEqual(timeouts[-1], 7.0)
    self.assertTrue(all(a > b for a, b in zip(timeouts[:-2], timeouts[1:-1], strict=True)))
    self.assertAlmostEqual(clock[0], 0.6)

  def test_authentication_switches_from_handshake_to_runtime_deadline(self):
    for authenticated, expected in ((False, 15.0), (True, 2.0)):
      with self.subTest(authenticated=authenticated):
        session = receiver(b"\x00")
        session.authenticated = authenticated
        session.peer.gettimeout = lambda: None
        session.peer.settimeout = Mock()
        with patch("tools.android_auto.session.time.monotonic", return_value=0), self.assertRaises(EOFError):
          session.receive()
        self.assertEqual(session.peer.settimeout.call_args_list[0].args, (expected,))
        self.assertEqual(session.peer.settimeout.call_args_list[-1].args, (None,))

  def test_invalid_timeouts_are_rejected(self):
    session = receiver(b"")
    for name in ("receive_timeout", "send_timeout", "handshake_timeout"):
      for value in (0, -1, float("nan"), float("inf")):
        with self.subTest(name=name, value=value), self.assertRaises(ValueError):
          setattr(session, name, value)


class TestSendDeadline(unittest.TestCase):
  def test_slow_fragment_drain_cannot_restart_whole_message_deadline(self):
    session = receiver(b"")
    session.send_timeout = 0.1
    clock, timeout = [0.0], [7.0]
    timeouts, accepted_fragments = [], []

    def set_timeout(value):
      timeout[0] = value
      timeouts.append(value)

    def slow_send(data):
      # Each fragment would succeed under a fresh 100ms socket timeout. The
      # second must fail with the remaining 40ms of the message's total budget.
      delay = 0.06
      if timeout[0] < delay:
        clock[0] += timeout[0]
        raise TimeoutError("slow reader")
      clock[0] += delay
      accepted_fragments.append(data)

    session.peer.gettimeout = lambda: timeout[0]
    session.peer.settimeout = set_timeout
    session.peer.sendall = slow_send
    with patch("tools.android_auto.session.time.monotonic", side_effect=lambda: clock[0]):
      with self.assertRaises(TimeoutError):
        session.send(5, 0, b"x" * 50_000, encrypted=False)
    self.assertEqual(len(accepted_fragments), 1)
    self.assertAlmostEqual(clock[0], 0.1)
    self.assertAlmostEqual(timeouts[0], 0.1)
    self.assertAlmostEqual(timeouts[1], 0.04)
    self.assertEqual(timeouts[-1], 7.0)

  def test_blocked_socket_send_restores_original_timeout(self):
    for original_timeout in (None, 7.0):
      left, right = socket.socketpair()
      with self.subTest(original_timeout=original_timeout), left, right:
        left.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4096)
        left.settimeout(original_timeout)
        session = receiver(b"")
        session.peer = left
        session.send_timeout = 0.02
        with self.assertRaises(TimeoutError):
          session.send(5, 0, b"x" * 50_000, encrypted=False)
        self.assertEqual(left.gettimeout(), original_timeout)

  def test_successful_send_restores_timeout_and_preserves_wire(self):
    left, right = socket.socketpair()
    with left, right:
      left.settimeout(7)
      right.settimeout(1)
      session = receiver(b"")
      session.peer = left
      session.send_timeout = 0.1
      session.send(0, 11, field(1, 123), encrypted=False)
      self.assertEqual(left.gettimeout(), 7)
      self.assertEqual(receiver(right.recv(1024)).receive(), (0, 11, field(1, 123)))

  def test_encryption_time_also_consumes_send_budget(self):
    session = receiver(b"")
    session.authenticated = True
    session.send_timeout = 0.1
    session.peer.gettimeout = lambda: 7.0
    session.peer.settimeout = Mock()
    session.peer.sendall = Mock()
    clock = [0.0]

    def encrypt(data):
      clock[0] += 0.11
      return len(data)

    session.tls = SimpleNamespace(write=encrypt)
    session.outgoing = SimpleNamespace(read=lambda: b"encrypted")
    with patch("tools.android_auto.session.time.monotonic", side_effect=lambda: clock[0]):
      with self.assertRaisesRegex(TimeoutError, "send deadline"):
        session.send(0, 11, field(1, 123))
    session.peer.sendall.assert_not_called()
    self.assertEqual(session.peer.settimeout.call_args_list[-1].args, (7.0,))


if __name__ == "__main__":
  unittest.main()
