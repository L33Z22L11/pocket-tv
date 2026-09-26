"""USB-first failover without restarting the media decoder or losing its timeline."""
import time
from transport import Link, NetworkEndpoint, HELLO, STATUS, SELECT, KEY
from wifi import discover


class TransportChanged(Exception):
    """An established link changed; caller must rebuffer against the new generation."""


class RoamingLink:
    def __init__(self, locator, profile, count, host=None):
        self.locator, self.profile, self.count = locator, dict(profile), count
        self.explicit_host = host
        self.link = None
        self.state = None
        self.kind = None
        self.next_usb = self.next_retry = self.next_discovery = 0
        if profile.get('usb_serial'):
            self.locator.serial_number = profile['usb_serial']

    def _connect(self, endpoint, kind):
        candidate = Link(endpoint)
        try:
            # Probe before claiming: never alter an unrelated USB device.
            state = candidate.request(STATUS)
            if state.get('protocol') != 'PTV6' or state.get('device_id') != self.profile['device_id']:
                raise ConnectionError('连接的设备不是已配对的 Pocket TV')
            previous = self.state
            # Same running device is authoritative (physical keys may change offline).
            restarted = state.get('count', 0) == 0
            state = candidate.request(HELLO, argument=self.count)
            if restarted and previous:
                desired = previous['index'] % self.count
                if state['index'] != desired:
                    state = candidate.request(SELECT, argument=desired)
                if state['playing'] != previous['playing']:
                    state = candidate.request(KEY, argument=2)
            if state.get('wifi_ip') and state['wifi_ip'] != '0.0.0.0':
                self.profile['host'] = state['wifi_ip']
        except BaseException:
            candidate.close()
            raise
        old = self.link
        self.link, self.state, self.kind = candidate, state, kind
        if old:
            old.close()  # No STOP: it would silence the newly selected transport.
        print(f'传输已连接：{kind.upper()} {endpoint}', flush=True)

    def connect(self):
        now = time.monotonic()
        if now < self.next_retry:
            raise ConnectionError('等待设备重新连接')
        self.next_retry = now+1
        try:
            self._connect(self.locator.resolve(), 'usb')
            return
        except OSError:
            pass
        host = self.explicit_host or self.profile.get('host')
        if not self.explicit_host and now >= self.next_discovery:
            self.next_discovery = now+4
            try:
                host = discover(self.profile) or host
            except OSError:
                pass
        if not host:
            raise ConnectionError('等待已配对设备的 USB 或 Wi-Fi')
        self._connect(NetworkEndpoint(host, self.profile['device_id'], self.profile['key']), 'wifi')
        self.next_usb = time.monotonic()+1

    def _failed(self):
        if self.link:
            self.state = self.link.state or self.state
            self.link.close()
        self.link = None
        self.next_retry = 0

    def request(self, *args, **kwargs):
        if not self.link:
            raise ConnectionError('传输未连接')
        try:
            result = self.link.request(*args, **kwargs)
            self.state = self.link.state
            return result
        except OSError:
            self._failed()
            raise

    def read(self):
        if self.link is None:
            self.connect()
            raise TransportChanged()
        if self.kind == 'wifi' and time.monotonic() >= self.next_usb:
            self.next_usb = time.monotonic()+2
            try:
                port = self.locator.resolve()
            except OSError:
                port = None
            if port:
                try:
                    self._connect(port, 'usb')
                except OSError:
                    pass  # Keep the working Wi-Fi session if USB is not ready yet.
                else:
                    raise TransportChanged()
        try:
            messages = self.link.read()
            self.state = self.link.state
            return messages
        except OSError:
            self._failed()
            raise

    def close(self):
        if self.link:
            self.link.close()
            self.link = None
