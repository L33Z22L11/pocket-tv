"""Integration checks against generated, silent fixtures; skip when absent."""
import queue
import sys
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'host'))
from media import Decoder
from sources import Source

FIXTURES = Path(__file__).parent / 'fixtures'

class DecodeTests(unittest.TestCase):
    def test_orientation_metadata_and_audio(self):
        for name, landscape, audio in [('landscape', True, True), ('portrait', False, False), ('rotated', False, True)]:
            path = FIXTURES / f'{name}.mp4'
            if not path.exists():
                self.skipTest('Generate fixtures with tools/make-fixtures.sh')
            decoder = Decoder(Source(name, str(path)))
            video_count = audio_count = 0
            try:
                while True:
                    event = decoder.queue.get(timeout=16)
                    self.assertNotEqual(event.kind, 'error', event.detail)
                    if event.kind == 'end':
                        break
                    if event.kind == 'video':
                        video_count += 1
                        self.assertEqual(event.landscape, landscape)
                        self.assertLessEqual(len(event.payload), 49152)
                    elif event.kind == 'audio':
                        audio_count += 1
                        self.assertLessEqual(len(event.payload), 1024)
                self.assertGreaterEqual(video_count, 55)
                self.assertEqual(audio_count > 0, audio)
            finally:
                decoder.close()
