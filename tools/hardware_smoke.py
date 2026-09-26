"""Silent hardware protocol verification. Requires Pocket TV firmware."""
import json
from pathlib import Path
import sys
import time
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'host'))
import av
from PIL import Image
from media import encode_frame
from transport import Link, HELLO, KEY, FRAME, LAYOUT, AUDIO, STATUS, VOLUME, STOP

link = Link(sys.argv[1])
try:
    state = link.request(HELLO, argument=2)
    assert state['protocol'] == 'PTV6'
    saved_volume = state['volume']
    saved_hints = state['hints']
    state = link.request(VOLUME, argument=0)
    if not state['playing']:
        state = link.request(KEY, argument=2)
    count = state['frames']
    for size in [(640, 360), (180, 320), (640, 480), (240, 320)]:
        event = encode_frame(av.VideoFrame.from_image(Image.new('RGB', size, (40, 160, 220))))
        state = link.request(LAYOUT, argument=int(event.landscape))
        assert state['landscape'] == event.landscape
        state = link.request(FRAME, generation=state['generation'], argument=1, payload=event.payload)
        assert state['rendered']
        import struct
        assert (state['frame_w'], state['frame_h']) == struct.unpack('<HH', event.payload[:4])
    state = link.request(KEY, argument=2)
    assert not state['playing']
    index, generation = state['index'], state['generation']
    frames = state['frames']
    for expected in (not saved_hints, saved_hints):
        state = link.request(KEY, argument=5)
        assert state['hints'] == expected and not state['playing']
        assert state['frames'] == frames and state['generation'] == generation and state['index'] == index
    state = link.request(KEY, argument=3)
    assert state['volume'] == 5 and state['index'] == index and state['generation'] == generation
    state = link.request(KEY, argument=4)
    assert state['volume'] == 0 and state['index'] == index
    state = link.request(FRAME, generation=state['generation'], argument=1, payload=event.payload)
    assert not state['rendered']
    old = state['index']
    state = link.request(KEY, argument=1)
    assert state['index'] == (old + 1) % 2 and state['preview']
    state = link.request(FRAME, generation=state['generation'], argument=1, payload=event.payload)
    assert state['rendered'] and not state['playing']
    state = link.request(KEY, argument=0)
    assert state['index'] == old
    state = link.request(KEY, argument=2)
    assert state['playing']
    before = state['audio_bytes']
    state = link.request(AUDIO, generation=state['generation'], payload=b'\x00' * 1024)
    assert state['volume'] == 0 and state['audio_bytes'] == before
    state = link.request(VOLUME, argument=0)
    print(json.dumps({'result': 'PASS', 'checks': ['landscape', 'portrait', 'full 320x240 / 240x320', 'paused overlay toggle without generation/frame change', 'pause', 'paused preview', 'next', 'previous', 'resume', 'volume up/down while paused', 'zero-volume audio discard'], 'state': state}))
finally:
    try:
        if 'saved_hints' in locals() and link.state['hints'] != saved_hints:
            link.request(KEY, argument=5)
        if 'saved_volume' in locals():
            link.request(VOLUME, argument=saved_volume)
        link.request(STOP)
    finally:
        link.close()
