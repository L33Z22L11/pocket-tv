"""Bounded parallel prefetch for simple MPEG-TS HLS playlists.

FFmpeg consumes ordered cached segments through a file-like reader. Encrypted,
byte-range, fMP4 and alternate-track playlists keep the native FFmpeg path.
"""
from dataclasses import dataclass
import io
import queue
import threading
import time
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen

MAX_SEGMENTS = 8
MAX_SEGMENT_BYTES = 8 * 1024 * 1024


class UnsupportedHLS(ValueError):
    pass


def parse_manifest(text, origin):
    if not text.lstrip().startswith('#EXTM3U') or any(tag in text for tag in
            ('#EXT-X-STREAM-INF', '#EXT-X-MEDIA:', '#EXT-X-KEY:', '#EXT-X-MAP:', '#EXT-X-BYTERANGE', '#EXT-X-I-FRAMES-ONLY')):
        raise UnsupportedHLS('Playlist requires native HLS demuxer')
    sequence = 0
    duration = 0
    segments = []
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith('#EXT-X-MEDIA-SEQUENCE:'):
            sequence = int(line.split(':', 1)[1])
        elif line.startswith('#EXTINF:'):
            duration = float(line.split(':', 1)[1].split(',', 1)[0])
        elif line and not line.startswith('#'):
            url = urljoin(origin, line)
            if urlparse(url).scheme not in ('http', 'https') or not urlparse(url).path.lower().endswith('.ts'):
                raise UnsupportedHLS('Only complete MPEG-TS segments are prefetched')
            segments.append((sequence, url, duration))
            sequence += 1
    if not segments:
        raise UnsupportedHLS('No complete TS segments')
    return segments, '#EXT-X-ENDLIST' in text


@dataclass
class Segment:
    sequence: int
    url: str
    duration: float
    data: bytes = b''
    done: bool = False
    error: str = ''
    read_offset: int = 0


class HLSReader(io.RawIOBase):
    def __init__(self, url, headers, stop, opener=urlopen):
        super().__init__()
        self.url, self.headers, self.stop = url, dict(headers), stop
        self.opener = opener
        self.cv = threading.Condition()
        self.pending = queue.Queue(maxsize=MAX_SEGMENTS)
        self.entries = {}
        self.consumed = -1
        self.ended = False
        self.last_sequence = -1
        self.refresh_error = None
        self.stopping = threading.Event()
        self.downloaded = self.failures = 0
        self._refresh()  # Detect unsupported playlists before starting workers.
        self.threads = [threading.Thread(target=self._worker, daemon=True, name='hls-fetch') for _ in range(4)]
        self.threads.append(threading.Thread(target=self._poll, daemon=True, name='hls-list'))
        for thread in self.threads:
            thread.start()

    def _stopped(self):
        return self.stop.is_set() or self.stopping.is_set()

    def _request(self, url):
        return self.opener(Request(url, headers=self.headers), timeout=10)

    def _refresh(self):
        with self._request(self.url) as response:
            text = response.read(1024 * 1024 + 1)
            if len(text) > 1024 * 1024:
                raise ValueError('HLS playlist exceeds 1 MiB')
            segments, ended = parse_manifest(text.decode('utf-8-sig'), response.geturl())
        with self.cv:
            self.ended = ended
            self.last_sequence = segments[-1][0]
            for sequence, url, duration in segments:
                if sequence <= self.consumed or sequence in self.entries:
                    continue
                if len(self.entries) >= MAX_SEGMENTS:
                    break
                segment = Segment(sequence, url, duration)
                self.entries[sequence] = segment
                self.pending.put_nowait(segment)
            self.refresh_error = None
            self.cv.notify_all()

    def _poll(self):
        while not self.stopping.wait(1):
            if self.stop.is_set():
                return
            try:
                self._refresh()
            except Exception as exc:
                with self.cv:
                    self.refresh_error = exc
                    self.cv.notify_all()

    def _worker(self):
        while not self._stopped():
            try:
                segment = self.pending.get(timeout=.2)
            except queue.Empty:
                continue
            for attempt in range(2):
                try:
                    data = bytearray()
                    started = time.monotonic()
                    with self._request(segment.url) as response:
                        while not self._stopped():
                            chunk = response.read(65536)
                            if not chunk:
                                break
                            data.extend(chunk)
                            if data[0] != 0x47:
                                raise ValueError('Invalid MPEG-TS segment')
                            if len(data) > MAX_SEGMENT_BYTES:
                                raise ValueError('HLS segment exceeds 8 MiB')
                            with self.cv:
                                segment.data=bytes(data)
                                self.cv.notify_all()
                            if time.monotonic()-started > 60:
                                raise TimeoutError('HLS segment download exceeded 60 seconds')
                    if self._stopped():
                        return
                    if not data or len(data) % 188 or data[0] != 0x47:
                        raise ValueError('Incomplete or invalid MPEG-TS segment')
                    with self.cv:
                        segment.data = bytes(data)
                        segment.done = True
                        self.downloaded += 1
                        self.cv.notify_all()
                    break
                except Exception as exc:
                    with self.cv:
                        # Once bytes reached the demuxer, restarting this segment
                        # would duplicate its prefix and corrupt the stream.
                        failed=attempt==1 or bool(segment.read_offset)
                        if failed:
                            segment.error=type(exc).__name__;segment.done=True
                            self.failures+=1
                        else:segment.data=b''
                        self.cv.notify_all()
                    if failed:break
                    if self.stopping.wait(.25):return

    def readable(self):
        return True

    def seekable(self):
        return False

    def read(self, size=-1):
        if size < 0:
            size = 32768
        if not size:
            return b''
        deadline = time.monotonic()+75
        with self.cv:
            while not self._stopped():
                if self.entries:
                    sequence=min(self.entries)
                    segment=self.entries[sequence]
                    if segment.error:
                        raise OSError('HLS segment failed: '+segment.error)
                    if segment.read_offset<len(segment.data):
                        data=segment.data[segment.read_offset:segment.read_offset+size]
                        segment.read_offset+=len(data)
                        if segment.done and segment.read_offset==len(segment.data):
                            del self.entries[sequence];self.consumed=sequence
                        return data
                    if segment.done:
                        del self.entries[sequence];self.consumed=sequence
                        continue
                elif self.ended and self.consumed >= self.last_sequence:
                    return b''
                if time.monotonic() > deadline:
                    raise TimeoutError('No HLS segment ready within 75 seconds')
                self.cv.wait(.1)
        return b''

    def close(self):
        self.stopping.set()
        with self.cv:
            self.cv.notify_all()
        super().close()
