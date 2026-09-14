"""Bounded asynchronous FFmpeg/PyAV decoder; timestamps remain in seconds."""
from dataclasses import dataclass
import queue
import threading
import struct
import zlib
import re
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen
import av
from PIL import Image

@dataclass
class Event:
    kind: str
    pts: float = 0
    payload: bytes = b''
    landscape: bool = False
    detail: str = ''


def encode_frame(frame, sar=1):
    rotation = int(round(getattr(frame, 'rotation', 0) or 0)) % 360
    width, height = frame.width * float(sar or 1), frame.height
    if rotation in (90, 270):
        width, height = height, width
    landscape = width > height
    limit_w, limit_h = (320, 240) if landscape else (240, 320)
    scale = min(limit_w / width, limit_h / height)
    w, h = max(1, round(width * scale)), max(1, round(height * scale))
    pre_w, pre_h = (h, w) if rotation in (90, 270) else (w, h)
    picture = frame.reformat(width=pre_w, height=pre_h, format='rgb24').to_image()
    if rotation:
        picture = picture.rotate(rotation, expand=True)
    for colors in (256, 128, 64, 32, 16):
        indexed = picture.quantize(colors=colors, method=Image.Quantize.FASTOCTREE, dither=Image.Dither.NONE)
        palette = indexed.getpalette() or []
        palette += [0] * (768 - len(palette))
        rgb565 = b''.join(struct.pack('>H', ((palette[i] >> 3) << 11) | ((palette[i+1] >> 2) << 5) | (palette[i+2] >> 3)) for i in range(0, 768, 3))
        payload = struct.pack('<HH', *indexed.size) + rgb565 + zlib.compress(indexed.tobytes(), 1)
        if len(payload) <= 49152:
            return Event('video', payload=payload, landscape=landscape)
    raise ValueError('Unable to fit video frame into USB packet')


def select_hls_variant(text, origin):
    # Keep the master for external audio/subtitle groups, which FFmpeg must join.
    if '#EXT-X-MEDIA:' in text:
        return origin
    variants = []
    bandwidth = None
    for line in text.splitlines():
        line = line.strip()
        if line.startswith('#EXT-X-STREAM-INF:'):
            match = re.search(r'(?:[:,])BANDWIDTH=(\d+)', line)
            bandwidth = int(match.group(1)) if match else None
        elif line and not line.startswith('#') and bandwidth is not None:
            variants.append((bandwidth, urljoin(origin, line)))
            bandwidth = None
    return min(variants)[1] if variants else origin


def media_url(source):
    if urlparse(source.url).scheme not in ('http', 'https') or not urlparse(source.url).path.lower().endswith('.m3u8'):
        return source.url
    try:
        with urlopen(Request(source.url, headers=source.headers), timeout=10) as response:
            text = response.read(1024 * 1024).decode('utf-8-sig')
        return select_hls_variant(text, source.url)
    except (OSError, UnicodeError):
        return source.url


class Decoder:
    def __init__(self, source, fps=20):
        self.source, self.fps = source, fps
        self.queue = queue.Queue(maxsize=256)
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self.run, daemon=True, name='media-decoder')
        self.thread.start()

    def put(self, event):
        while not self.stop.is_set():
            try:
                self.queue.put(event, timeout=.1)
                return True
            except queue.Full:
                pass
        return False

    def close(self):
        self.stop.set()
        self.thread.join(timeout=12)

    def run(self):
        try:
            options = {'rw_timeout': '10000000'}
            if self.source.headers:
                options['headers'] = ''.join(f'{k}: {v}\r\n' for k, v in self.source.headers.items() if '\n' not in v and '\r' not in v)
            with av.open(media_url(self.source), timeout=(20, 10), options=options) as container:
                if not container.streams.video:
                    raise ValueError('Source has no video stream')
                video = container.streams.video[0]
                streams = [video]
                if container.streams.audio:
                    streams.append(container.streams.audio[0])
                resampler = av.AudioResampler(format='s16', layout='mono', rate=16000)
                first_pts, next_video, fallback = None, 0, 0
                for packet in container.demux(streams):
                    if self.stop.is_set():
                        return
                    for frame in packet.decode():
                        pts = float(frame.pts * frame.time_base) if frame.pts is not None else fallback
                        if first_pts is None:
                            first_pts = pts
                        pts -= first_pts
                        if isinstance(frame, av.VideoFrame):
                            fallback = pts + first_pts + 1 / float(video.average_rate or self.fps)
                            if pts + .001 < next_video:
                                continue
                            next_video = max(next_video + 1 / self.fps, pts)
                            event = encode_frame(frame, video.sample_aspect_ratio or 1)
                            event.pts = max(0, pts)
                            if not self.put(event):
                                return
                        else:
                            for audio in resampler.resample(frame):
                                data = bytes(audio.planes[0])[:audio.samples * 2]
                                audio_pts = float(audio.pts * audio.time_base) - first_pts if audio.pts is not None else pts
                                for offset in range(0, len(data), 1024):
                                    if not self.put(Event('audio', max(0, audio_pts + offset / 32000), data[offset:offset+1024])):
                                        return
                self.put(Event('end'))
        except Exception as exc:
            self.put(Event('error', detail=str(exc)))
