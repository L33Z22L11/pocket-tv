"""Playback scheduling regression with a slow display and deterministic clock."""
import contextlib
import io
import queue
import sys
import unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'host'))
import player
from media import Event
from sources import Source
from transport import HELLO, FRAME, STATUS, KEY, AUDIO, VOLUME, LAYOUT, STOP

class Clock:
    def __init__(self): self.now=0
    def monotonic(self): return self.now
    def sleep(self, seconds): self.now+=seconds

class IdleThread:
    def __init__(self, **kwargs): pass
    def start(self): pass

class PlaybackTests(unittest.TestCase):
    def run_player(self, fail_first=False, switch_during_audio=False):
        clock=Clock()
        calls=[]
        opened=[]
        class Device:
            def __init__(self, port):
                self.switched=False
                self.state=dict(protocol='PTV5',volume=40,index=0,generation=1,count=2,
                                playing=True,preview=True,landscape=True,audio_queued=0,rendered=False)
            def read(self): return []
            def close(self): pass
            def request(self, kind, generation=0, argument=0, payload=b''):
                calls.append(kind)
                if kind==AUDIO and switch_during_audio:
                    if not self.switched:
                        self.state['index']=1
                        self.state['generation']+=1
                        self.switched=True
                    if generation==self.state['generation']:
                        assert payload.startswith(sources[self.state['index']].name.encode()), 'stale audio crossed channel generation'

                if kind==KEY and argument==1:
                    self.state['index']=(self.state['index']+1)%self.state['count']
                if kind==VOLUME: self.state['volume']=argument
                if kind==HELLO: self.state['count']=argument
                if kind==FRAME:
                    self.state['rendered']=True
                    self.state['preview']=False
                    clock.now+=.10 # slower than the video frame interval
                return self.state.copy()
        class Decode:
            def __init__(self, source, fps):
                opened.append(source.name)
                self.queue=queue.Queue()
                if source.name=='bad':
                    self.queue.put(Event('error',detail='network unavailable'))
                else:
                    for i in range(100):
                        self.queue.put(Event('audio', i*.02, source.name.encode().ljust(640,b'\0')))
                        if i%2==0: self.queue.put(Event('video',i*.02,b'frame',True))
                    self.queue.put(Event('end'))
            def close(self): pass
        sources=[Source('bad','bad'), Source('good','good')] if fail_first else [Source('good','good')]
        if switch_during_audio:
            sources=[Source('first','first'),Source('second','second')]
        with patch.object(player,'DeviceLocator') as locator, patch.object(player,'Link',Device), patch.object(player,'Decoder',Decode), patch.object(player,'time',clock), patch.object(player.threading,'Thread',IdleThread), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            locator.return_value.resolve.return_value='fake'
            player.play(sources,'fake',6 if fail_first else 3.5,20)
        return calls,opened

    def test_audio_refills_in_batches_around_slow_video(self):
        calls,_=self.run_player()
        batches=[];batch=0;seen_video=False
        for kind in calls:
            if kind==FRAME:
                if seen_video: batches.append(batch)
                batch=0;seen_video=True
            elif kind==AUDIO: batch+=1
        self.assertGreaterEqual(max(batches),4)
        self.assertEqual(calls[-1],STOP)
        self.assertNotIn(VOLUME,calls) # exiting must not overwrite remembered volume

    def test_unopenable_channel_advances_to_next(self):
        calls,opened=self.run_player(fail_first=True)
        self.assertEqual(opened[:2],['bad','good'])
        self.assertIn(FRAME,calls)
        self.assertEqual(calls[-1],STOP)

    def test_physical_channel_change_does_not_relabel_old_audio(self):
        calls,opened=self.run_player(switch_during_audio=True)
        self.assertEqual(opened[:2],['first','second'])
        self.assertEqual(calls[-1],STOP)

    def test_muted_video_recovers_after_network_gap(self):
        clock=Clock();frames=[]
        class Device:
            def __init__(self, path):
                self.state=dict(protocol='PTV5',volume=0,index=0,generation=1,count=1,
                                playing=True,preview=True,landscape=True,audio_queued=0,rendered=False)
            def read(self): pass
            def close(self): pass
            def request(self, kind, **kwargs):
                if kind==FRAME:
                    frames.append(clock.now)
                    self.state.update(rendered=True,preview=False)
                return self.state.copy()
        class TimedQueue:
            def __init__(self): self.items=[(0,Event('video',0,b'one',True)),(2.5,Event('video',.2,b'two',True))]
            def empty(self): return not self.items or self.items[0][0]>clock.now
            def qsize(self): return 0 if self.empty() else 1
            def get_nowait(self):
                if self.empty(): raise queue.Empty
                return self.items.pop(0)[1]
        class Decode:
            def __init__(self,*args): self.queue=TimedQueue()
            def close(self): pass
        with patch.object(player,'Link',Device),patch.object(player,'Decoder',Decode),patch.object(player,'time',clock),contextlib.redirect_stdout(io.StringIO()):
            player._play_session([Source('video','video')],'fake',3,20,queue.Queue(),{})
        self.assertEqual(len(frames),2)
        self.assertGreaterEqual(frames[1],2.5)
