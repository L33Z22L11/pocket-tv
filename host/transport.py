#!/usr/bin/env python3
"""Pocket TV USB protocol and exclusive player lock."""
import argparse
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import statistics
import struct
import time
import zlib

from PIL import Image, ImageOps
import serial
from serial.tools import list_ports

ROOT = Path(__file__).resolve().parents[1]
HEADER = struct.Struct('<8I')
MAGIC = 0x36565450
MAX_PAYLOAD = 32768
HELLO, FRAME, STATUS, KEY, SINK, AUDIO, VOLUME, LAYOUT, STOP, SELECT, WIFI_CONFIG, WIFI_CLEAR, WIFI_SCAN, WIFI_SCAN_RESULT, NOTICE, CHANNEL_NAME = range(1, 17)


@contextlib.contextmanager
def instance_lock(path):
    """OS-released lock on Windows, macOS and Linux; no stale PID locks."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch(exist_ok=True)
    with path.open('r+b') as lock:
        if os.name == 'nt':
            import msvcrt
            if path.stat().st_size == 0:
                lock.write(b' '); lock.flush()
            lock.seek(0)
            try:
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise BlockingIOError('Another player or flasher is running') from exc
        else:
            import fcntl
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            lock.seek(0)
            lock.write(str(os.getpid()).encode().ljust(32))
            lock.truncate(32); lock.flush()
            yield
        finally:
            lock.seek(0)
            if os.name == 'nt':
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(lock, fcntl.LOCK_UN)


def packet(kind, sequence, generation=0, argument=0, payload=b''):
    if len(payload) > MAX_PAYLOAD:
        raise ValueError('frame exceeds device payload limit')
    head = struct.pack('<7I', MAGIC, kind, sequence, generation, argument,
                       len(payload), zlib.crc32(payload))
    return head + struct.pack('<I', zlib.crc32(head)) + payload


class DeviceRestarted(ConnectionError):
    """The current USB session was invalidated by a device reboot."""


class Link:
    def __init__(self, port):
        if isinstance(port, NetworkEndpoint):
            self.serial = SocketStream(port)
        else:
            self.serial = serial.Serial(port=None, baudrate=115200, timeout=0.001, write_timeout=3)
            self.serial.dtr = False
            self.serial.rts = False
            self.serial.port = port
            self.serial.open()
        self.established = False
        self.sequence = 0
        self.state = None
        self.buffer = bytearray()
        self.replies = {}
        self.started = {}

    def close(self):
        self.serial.close()

    def read(self):
        self.buffer.extend(self.serial.read(max(1, self.serial.in_waiting)))
        messages = []
        while b'\n' in self.buffer:
            line, _, rest = self.buffer.partition(b'\n')
            self.buffer = bytearray(rest)
            try:
                msg = json.loads(line)
            except (ValueError, UnicodeDecodeError):
                continue  # ROM boot output is not protocol data.
            if isinstance(msg, dict) and 'generation' in msg:
                if self.established and (msg.get('kind') == 'boot' or msg.get('count') == 0):
                    raise DeviceRestarted('设备已重启，需要重新握手')
                self.state = msg
                messages.append(msg)
                if msg.get('kind') == 'ack':
                    start = self.started.pop(msg['seq'], None)
                    if start is not None:
                        msg['roundtrip_ms'] = (time.monotonic() - start) * 1000
                    self.replies[msg['seq']] = msg
                if msg.get('kind') in ('key', 'injected_key'):
                    print('Control ' + json.dumps(msg), flush=True)
        if len(self.buffer) > 8192:
            self.buffer.clear()
        return messages

    def submit(self, kind, generation=0, argument=0, payload=b'', corrupt=False):
        self.sequence += 1
        wire = packet(kind, self.sequence, generation, argument, payload)
        if corrupt and payload:
            wire = wire[:-1] + bytes([wire[-1] ^ 1])
        self.started[self.sequence] = time.monotonic()
        if self.serial.write(wire) != len(wire):
            raise TimeoutError('Incomplete serial write')
        return self.sequence

    def collect(self, sequence, allow_error=False):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if sequence in self.replies:
                reply = self.replies.pop(sequence)
                if reply['error'] in (150, 151):
                    raise ConnectionError('传输所有权已改变，等待重新握手')
                if reply['error'] in (1, 2) and not allow_error:
                    raise ConnectionError('设备收包超时或 CRC 错误，重新建立传输')
                if reply['error'] and not allow_error:
                    raise RuntimeError(f'Device rejected request: {reply}')
                return reply
            self.read()
        raise TimeoutError('Device ACK timed out; reconnecting USB player')

    def request(self, kind, generation=0, argument=0, payload=b'', corrupt=False):
        seq = self.submit(kind, generation, argument, payload, corrupt)
        result = self.collect(seq, allow_error=corrupt)
        if kind == HELLO:
            self.established = True
        return result

    def wait_until(self, deadline, generation):
        while time.monotonic() < deadline:
            self.read()
            if self.state and self.state['generation'] != generation:
                break


class NetworkEndpoint:
    def __init__(self, host, device_id, key, port=5760):
        self.host, self.device_id, self.key, self.port = host, device_id, key, port

    def __str__(self):
        return f'wifi://{self.host}:{self.port}'


class SocketStream:
    """Serial-like bounded TCP stream; authenticate a fresh challenge before packets."""
    def __init__(self, endpoint):
        import socket
        import hmac
        self.socket = socket.create_connection((endpoint.host, endpoint.port), timeout=2)
        try:
            self.socket.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            challenge = self._exact(48)
            if challenge[:4] != b'PTV6' or challenge[4:16].decode('ascii') != endpoint.device_id:
                raise ConnectionError('Wi-Fi device identity mismatch')
            self.socket.sendall(hmac.digest(bytes.fromhex(endpoint.key), b'client'+challenge, 'sha256'))
            proof = b'OK\n'+hmac.digest(bytes.fromhex(endpoint.key), b'server'+challenge, 'sha256')
            if not hmac.compare_digest(self._exact(35), proof):
                raise ConnectionError('Wi-Fi pairing rejected; provision again over USB')
        except BaseException:
            self.socket.close()
            raise
        self.socket.settimeout(.001)

    def _exact(self, count):
        result = bytearray()
        while len(result) < count:
            chunk = self.socket.recv(count-len(result))
            if not chunk:
                raise ConnectionError('Wi-Fi connection closed')
            result.extend(chunk)
        return bytes(result)

    @property
    def in_waiting(self):
        return 4096

    def read(self, size):
        import socket
        try:
            result = self.socket.recv(size)
        except socket.timeout:
            return b''
        if not result:
            raise ConnectionError('Wi-Fi connection closed')
        return result

    def write(self, data):
        self.socket.settimeout(2)
        try:
            self.socket.sendall(data)
            return len(data)
        finally:
            self.socket.settimeout(.001)

    def close(self):
        self.socket.close()
