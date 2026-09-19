"""Experimental phone-side AA session over a socket-compatible byte transport.

Protocol facts referenced from AACS (https://github.com/tomasz-grobelny/AACS).
Only the services needed by the DHU and parked Mazda video experiments are implemented.
"""

import json
import math
import ssl
import struct
import time

MAX_MESSAGE = 2 * 1024 * 1024
MAX_FRAGMENT_BYTES = 2 * MAX_MESSAGE


class AuthenticationRejected(ValueError):
  pass


def varint(value):
  if not 0 <= value < 1 << 64:
    raise ValueError("Expected an unsigned 64-bit protobuf integer")
  out = bytearray()
  while value > 127:
    out.append((value & 127) | 128)
    value >>= 7
  out.append(value)
  return bytes(out)


def field(number, value):
  if isinstance(value, str):
    value = value.encode()
  if isinstance(value, bytes):
    return varint(number << 3 | 2) + varint(len(value)) + value
  return varint(number << 3) + varint(value)


def parse_fields(data):
  """Decode bounded protobuf wire fields without a runtime/generated dependency."""
  pos = 0

  def integer():
    nonlocal pos
    result = 0
    for shift in range(0, 70, 7):
      if pos >= len(data):
        raise ValueError("Truncated protobuf varint")
      byte = data[pos]
      pos += 1
      if shift == 63 and byte > 1:
        raise ValueError("Overflowing protobuf varint")
      result |= (byte & 127) << shift
      if byte < 128:
        return result
    raise ValueError("Overlong protobuf varint")

  result = {}
  while pos < len(data):
    tag = integer()
    number, wire = tag >> 3, tag & 7
    if not 0 < number < 1 << 29:
      raise ValueError("Invalid protobuf field number")
    if wire == 0:
      value = integer()
    else:
      if wire == 2:
        size = integer()
      elif wire in (1, 5):
        size = 8 if wire == 1 else 4
      else:
        raise ValueError(f"Unsupported protobuf wire type {wire}")
      if size > len(data) - pos:
        raise ValueError("Truncated protobuf field")
      value = data[pos:pos + size]
      pos += size
    result.setdefault(number, []).append(value)
  return result


def one(fields, number, default=None):
  return fields.get(number, [default])[0]


def json_fields(fields):
  """Keep unknown protobuf bytes inspectable without guessing their schema."""
  return {number: [{"hex": value.hex()} if isinstance(value, bytes) else value for value in values]
          for number, values in fields.items()}


class Session:
  authenticated = False

  def __init__(self, peer, cert, key, log, ca=None, *, receive_timeout=2.0, send_timeout=2.0, handshake_timeout=15.0):
    self.peer = peer
    self.log = log
    self.receive_timeout = receive_timeout
    self.send_timeout = send_timeout
    self.handshake_timeout = handshake_timeout
    self.authenticated = False
    self.incoming = ssl.MemoryBIO()
    self.outgoing = ssl.MemoryBIO()
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.maximum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(cert, key)
    self.peer_verification_enabled = ca is not None
    if ca is not None:
      context.load_verify_locations(cafile=ca)
      context.verify_mode = ssl.CERT_REQUIRED
    self.tls = context.wrap_bio(self.incoming, self.outgoing, server_side=True)
    self.fragments = {}

  @staticmethod
  def _timeout(value):
    value = float(value)
    if not math.isfinite(value) or value <= 0:
      raise ValueError("Message timeout must be finite and positive")
    return value

  @property
  def receive_timeout(self):
    """Absolute receive deadline, in seconds, after authentication succeeds."""
    return getattr(self, "_receive_timeout", 2.0)

  @receive_timeout.setter
  def receive_timeout(self, value):
    self._receive_timeout = self._timeout(value)

  @property
  def send_timeout(self):
    """Absolute deadline for one outgoing message, including all fragments."""
    return getattr(self, "_send_timeout", 2.0)

  @send_timeout.setter
  def send_timeout(self, value):
    self._send_timeout = self._timeout(value)

  @property
  def handshake_timeout(self):
    return getattr(self, "_handshake_timeout", 15.0)

  @handshake_timeout.setter
  def handshake_timeout(self, value):
    self._handshake_timeout = self._timeout(value)

  def event(self, name, **values):
    self.log.write(json.dumps({"event": name, **values}) + "\n")
    self.log.flush()

  def send(self, channel, kind, body=b"", encrypted=True, control=False):
    """Send a whole message within one deadline; discard the session on failure.

    A timeout per fragment alone permits an arbitrarily slow receiver to hold
    the projection loop past its freshness watchdog. Each sendall instead gets
    only the unspent portion of this message's budget.
    """
    if self.authenticated and not encrypted:
      raise ValueError("Plaintext is forbidden after authentication")
    data = struct.pack(">H", kind) + body
    if len(data) > MAX_MESSAGE:
      raise ValueError("Message exceeds experimental size limit")
    deadline = time.monotonic() + self.send_timeout
    timed_peer = hasattr(self.peer, "gettimeout") and hasattr(self.peer, "settimeout")
    previous_timeout = self.peer.gettimeout() if timed_peer else None
    try:
      for offset in range(0, len(data), 16000):
        chunk = data[offset:offset + 16000]
        flags = (1 if offset == 0 else 0) | (2 if offset + len(chunk) == len(data) else 0)
        flags |= (8 if encrypted else 0) | (4 if control else 0)
        if encrypted:
          written = self.tls.write(chunk)
          if written != len(chunk):
            raise ValueError("Incomplete TLS write")
          chunk = self.outgoing.read()
        header = struct.pack(">BBH", channel, flags, len(chunk))
        if flags & 3 == 1:
          header += struct.pack(">I", len(data))
        remaining = deadline - time.monotonic()
        if remaining <= 0:
          raise TimeoutError("Android Auto message send deadline exceeded")
        if timed_peer:
          self.peer.settimeout(remaining)
        self.peer.sendall(header + chunk)
        if time.monotonic() >= deadline:
          raise TimeoutError("Android Auto message send deadline exceeded")
    finally:
      if timed_peer:
        self.peer.settimeout(previous_timeout)
    if kind not in (0, 1):
      self.event("tx", channel=channel, kind=kind, bytes=len(body))

  def receive(self):
    """Read a complete message within one deadline, including partial reads.

    A failed receive invalidates this session: its partially consumed transport
    must be discarded rather than retried. Restore the caller's socket timeout
    so send/select policy remains owned by the transport. In-memory test peers
    need only implement recv(); real transports must support socket timeouts.
    """
    deadline = time.monotonic() + (self.receive_timeout if self.authenticated else self.handshake_timeout)
    timed_peer = hasattr(self.peer, "gettimeout") and hasattr(self.peer, "settimeout")
    previous_timeout = self.peer.gettimeout() if timed_peer else None

    def read_exact(size):
      data = bytearray()
      while len(data) < size:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
          raise TimeoutError("Android Auto message receive deadline exceeded")
        if timed_peer:
          self.peer.settimeout(remaining)
        chunk = self.peer.recv(size - len(data))
        if time.monotonic() >= deadline:
          raise TimeoutError("Android Auto message receive deadline exceeded")
        if not chunk:
          raise EOFError(f"Peer disconnected after {len(data)}/{size} bytes")
        data.extend(chunk)
      return bytes(data)

    try:
      return self._receive_message(read_exact)
    finally:
      if timed_peer:
        self.peer.settimeout(previous_timeout)

  def _receive_message(self, read_exact):
    while True:
      channel, flags, size = struct.unpack(">BBH", read_exact(4))
      if flags & ~15 or size == 0:
        raise ValueError("Invalid frame header")
      if self.authenticated and not flags & 8:
        raise ValueError("Plaintext is forbidden after authentication")
      segment = flags & 3
      total = struct.unpack(">I", read_exact(4))[0] if segment == 1 else None
      if total is not None and not 2 <= total <= MAX_MESSAGE:
        raise ValueError("Invalid fragmented message size")
      if total is not None and total + sum(item[0] for item in self.fragments.values()) > MAX_FRAGMENT_BYTES:
        raise ValueError("Aggregate fragmented message limit exceeded")
      payload = read_exact(size)
      if flags & 8:
        self.incoming.write(payload)
        plain = bytearray()
        while True:
          try:
            part = self.tls.read(65536)
            if not part:
              raise EOFError("TLS closed")
            plain.extend(part)
          except ssl.SSLWantReadError:
            break
        payload = bytes(plain)
      if segment == 3:
        if channel in self.fragments:
          raise ValueError("Full message interrupted fragment sequence")
      elif segment == 1:
        if channel in self.fragments or len(payload) >= total:
          raise ValueError("Invalid first fragment")
        self.fragments[channel] = (total, flags & 12, bytearray(payload))
        continue
      else:
        if channel not in self.fragments:
          raise ValueError("Continuation without first fragment")
        expected, mode, assembled = self.fragments[channel]
        if mode != flags & 12 or len(assembled) + len(payload) > expected:
          raise ValueError("Inconsistent fragment sequence")
        assembled.extend(payload)
        if segment == 0:
          continue
        del self.fragments[channel]
        if len(assembled) != expected:
          raise ValueError("Incorrect assembled message size")
        payload = bytes(assembled)
      if len(payload) < 2:
        raise ValueError("Missing message type")
      kind = struct.unpack(">H", payload[:2])[0]
      self.event("rx", channel=channel, kind=kind, bytes=len(payload) - 2)
      return channel, kind, payload[2:]

  def authenticate(self):
    channel, kind, data = self.receive()
    if channel != 0 or kind != 1 or len(data) != 4:
      raise ValueError("Expected version request")
    major, minor = struct.unpack(">HH", data)
    if major != 1:
      raise ValueError(f"Unsupported version {major}.{minor}")
    self.send(0, 2, struct.pack(">HHH", 1, min(minor, 5), 0), encrypted=False)
    while True:
      channel, kind, data = self.receive()
      if channel != 0 or kind != 3:
        raise ValueError("Expected TLS handshake")
      self.incoming.write(data)
      done = False
      try:
        self.tls.do_handshake()
        done = True
      except ssl.SSLWantReadError:
        pass
      response = self.outgoing.read()
      if response:
        self.send(0, 3, response, encrypted=False)
      if done:
        break
    self.event("tls_established", version=self.tls.version(), cipher=self.tls.cipher()[0])
    if self.peer_verification_enabled:
      peer_cert = self.tls.getpeercert()
      organizations = [value for rdn in peer_cert["subject"] for name, value in rdn if name == "organizationName"]
      if any(name in ("CarService", "Google Automotive Link") for name in organizations):
        raise ValueError("Head unit presented a phone or CA identity")
      self.event("head_unit_verified", subject=peer_cert["subject"], expires=peer_cert["notAfter"])
    channel, kind, data = self.receive()
    if channel != 0 or kind != 4:
      raise ValueError(f"Expected authentication status; got {channel}/{kind}")
    status = one(parse_fields(data), 1)
    if status is None or not isinstance(status, int):
      raise ValueError("Missing or invalid authentication status")
    if status >= 1 << 63:
      status -= 1 << 64
    if status != 0:
      self.event("authentication_rejected", status=status)
      raise AuthenticationRejected(f"Head unit rejected the phone certificate (status {status}); TLS alone is not authentication")
    self.authenticated = True
    self.event("authenticated", version=self.tls.version(), cipher=self.tls.cipher()[0])

  def wait_for(self, channel, kind):
    for _ in range(100):
      ch, message, data = self.receive()
      if ch == channel and message == kind:
        return data
      if ch == 0 and message == 11:
        self.send(0, 12, field(1, one(parse_fields(data), 1)))
      else:
        raise ValueError(f"Unexpected message {ch}/{message} while waiting for {channel}/{kind}: {data.hex()}")
    raise ValueError("Expected message did not arrive")

  def discover(self):
    self.send(0, 5, field(4, "Automaxxing local experiment") + field(5, "Zoompilot"))
    raw = self.wait_for(0, 6)
    descriptors = parse_fields(raw).get(1, [])
    channels = []
    for descriptor in descriptors:
      fields = parse_fields(descriptor)
      item = {"id": one(fields, 1), "service_fields": [number for number in fields if number != 1]}
      if 3 in fields:
        media = parse_fields(one(fields, 3))
        item["media_type"] = one(media, 1)
        item["video_configs"] = [json_fields(parse_fields(c)) for c in media.get(4, [])]
      if 4 in fields:
        from tools.android_auto.input import repeated_integers
        item["input_keycodes"] = repeated_integers(parse_fields(one(fields, 4)).get(1, []))
      channels.append(item)
    self.event("discovered", channels=channels, raw_hex=raw.hex())
    return channels
