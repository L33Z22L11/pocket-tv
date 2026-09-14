import sys
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'host'))
from sources import parse_playlist
from media import encode_frame, select_hls_variant
from PIL import Image
import av
import struct
import zlib

class MediaTests(unittest.TestCase):
    def test_master_uses_low_bandwidth_and_keeps_external_audio(self):
        text = '#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1000000\nhigh.m3u8\n#EXT-X-STREAM-INF:BANDWIDTH=400000\nlow.m3u8'
        origin = 'https://example.org/live/index.m3u8?token=abc'
        self.assertEqual(select_hls_variant(text, origin), 'https://example.org/live/low.m3u8')
        self.assertEqual(select_hls_variant('#EXT-X-MEDIA:TYPE=AUDIO\n' + text, origin), origin)

    def test_hls_is_one_source(self):
        items = parse_playlist('#EXTM3U\n#EXT-X-TARGETDURATION:6\n#EXTINF:6,\na.ts', 'https://example.org/live/index.m3u8')
        self.assertEqual([s.url for s in items], ['https://example.org/live/index.m3u8'])

    def test_channels(self):
        items = parse_playlist('\ufeff#EXTM3U\n#EXTINF:-1 group-title="News, TV",新闻\n#EXTVLCOPT:http-referrer=https://example.org/\na.m3u8\n#EXTINF:-1,Second\nhttps://example.net/b', 'https://example.org/list/index.m3u')
        self.assertEqual(items[0].name, '新闻')
        self.assertEqual(items[0].group, 'News, TV')
        self.assertEqual(items[0].url, 'https://example.org/list/a.m3u8')
        self.assertEqual(items[0].headers, {'Referer': 'https://example.org/'})
        self.assertEqual(items[1].headers, {})

    def test_frame_orientation_and_wire_pixels(self):
        for size, expected in [((640,360),(320,180)), ((180,320),(180,320)), ((640,480),(320,240)), ((240,320),(240,320))]:
            event = encode_frame(av.VideoFrame.from_image(Image.new('RGB',size,(255,0,0))))
            self.assertEqual(struct.unpack('<HH',event.payload[:4]),expected)
            self.assertEqual(event.landscape, size[0]>size[1])
            indices=zlib.decompress(event.payload[516:])
            self.assertEqual(len(indices),expected[0]*expected[1])
            color=struct.unpack_from('>H', event.payload, 4+indices[0]*2)[0]
            self.assertEqual(color,0xf800)

if __name__ == '__main__':
    unittest.main()
