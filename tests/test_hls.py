import io
import sys
import threading
import time
import unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'host'))
from hls import HLSReader, UnsupportedHLS, parse_manifest, MAX_SEGMENTS

class Response(io.BytesIO):
    def __init__(self,data,url):super().__init__(data);self.url=url
    def geturl(self):return self.url

class HLSTests(unittest.TestCase):
    def test_redirect_base_and_sequence(self):
        segments,end=parse_manifest('#EXTM3U\n#EXT-X-MEDIA-SEQUENCE:50\n#EXTINF:10,\na.ts\n#EXTINF:9,\nb.ts\n#EXT-X-ENDLIST','https://cdn.example/live/index.m3u8')
        self.assertEqual(segments,[(50,'https://cdn.example/live/a.ts',10),(51,'https://cdn.example/live/b.ts',9)])
        self.assertTrue(end)

    def test_complex_playlists_stay_with_native_demuxer(self):
        for tag in ('#EXT-X-KEY:METHOD=AES-128','#EXT-X-MAP:URI="init.mp4"','#EXT-X-BYTERANGE:188','#EXT-X-MEDIA:TYPE=AUDIO'):
            with self.assertRaises(UnsupportedHLS):parse_manifest('#EXTM3U\n'+tag+'\n#EXTINF:10,\na.ts','https://example.org/a.m3u8')

    def test_parallel_downloads_are_read_in_order_and_bounded(self):
        manifest='#EXTM3U\n#EXT-X-MEDIA-SEQUENCE:0\n'+''.join('#EXTINF:10,\n%d.ts\n'%i for i in range(12))+'#EXT-X-ENDLIST'
        barrier=threading.Barrier(4);started=[];lock=threading.Lock()
        chunks=[bytes([0x47,i])+bytes(186) for i in range(12)]
        def opener(request,timeout):
            url=request.full_url
            if url.endswith('.m3u8'):return Response(manifest.encode(),'https://cdn.example/live/index.m3u8')
            i=int(url.rsplit('/',1)[1][:-3])
            with lock:started.append(i)
            if i<4:barrier.wait(timeout=2)
            if i==0:time.sleep(.05)
            return Response(chunks[i],url)
        reader=HLSReader('https://origin.example/a.m3u8',{},threading.Event(),opener)
        try:
            self.assertEqual(reader.read(188),chunks[0])
            self.assertGreaterEqual(len(started),4)
            self.assertLessEqual(len(reader.entries),MAX_SEGMENTS)
            output=chunks[0]
            while True:
                data=reader.read(188)
                if not data:break
                output+=data
            self.assertEqual(output,b''.join(chunks))
        finally:reader.close()

    def test_failed_segment_retries_and_close_unblocks(self):
        calls=[]
        def opener(request,timeout):
            if request.full_url.endswith('.m3u8'):
                return Response(b'#EXTM3U\n#EXTINF:10,\na.ts\n#EXT-X-ENDLIST','https://example.org/a.m3u8')
            calls.append(1);raise OSError('unavailable')
        reader=HLSReader('https://example.org/a.m3u8',{},threading.Event(),opener)
        try:
            with self.assertRaises(OSError):reader.read(188)
            self.assertEqual(len(calls),2)
        finally:reader.close()
        self.assertEqual(reader.read(188),b'')

    def test_live_refresh_does_not_repeat_consumed_segments(self):
        refreshes=[]
        chunks={i:bytes([0x47,i])+bytes(186) for i in range(10,15)}
        def opener(request,timeout):
            if request.full_url.endswith('.m3u8'):
                refreshes.append(1)
                first=10 if len(refreshes)==1 else 12
                text='#EXTM3U\n#EXT-X-MEDIA-SEQUENCE:%d\n'%first
                text+=''.join('#EXTINF:10,\n%d.ts\n'%i for i in range(first,first+3))
                return Response(text.encode(),'https://example.org/a.m3u8')
            i=int(request.full_url.rsplit('/',1)[1][:-3])
            return Response(chunks[i],request.full_url)
        reader=HLSReader('https://example.org/a.m3u8',{},threading.Event(),opener)
        try:
            output=b''.join(reader.read(188) for _ in range(5))
            self.assertEqual(output,b''.join(chunks.values()))
            self.assertEqual(reader.consumed,14)
        finally:reader.close()

    def test_close_interrupts_reader_waiting_on_download(self):
        gate=threading.Event();result=[]
        def opener(request,timeout):
            if request.full_url.endswith('.m3u8'):
                return Response(b'#EXTM3U\n#EXTINF:10,\na.ts','https://example.org/a.m3u8')
            gate.wait(2)
            return Response(bytes([0x47])+bytes(187),request.full_url)
        reader=HLSReader('https://example.org/a.m3u8',{},threading.Event(),opener)
        thread=threading.Thread(target=lambda:result.append(reader.read(188)))
        thread.start()
        try:
            reader.close();thread.join(timeout=.5)
            self.assertFalse(thread.is_alive())
            self.assertEqual(result,[b''])
        finally:gate.set()

    def test_startup_does_not_wait_for_second_segment(self):
        second=threading.Event();first_ready=threading.Event();result=[]
        def opener(request,timeout):
            if request.full_url.endswith('.m3u8'):
                return Response(b'#EXTM3U\n#EXTINF:10,\n0.ts\n#EXTINF:10,\n1.ts','https://example.org/a.m3u8')
            if request.full_url.endswith('1.ts'):second.wait(2)
            else:first_ready.set()
            return Response(bytes([0x47])+bytes(187),request.full_url)
        reader=HLSReader('https://example.org/a.m3u8',{},threading.Event(),opener)
        thread=threading.Thread(target=lambda:result.append(reader.read(188)))
        thread.start()
        try:
            self.assertTrue(first_ready.wait(1))
            thread.join(timeout=1)
            self.assertFalse(thread.is_alive())
            self.assertEqual(len(result[0]),188)
        finally:second.set();reader.close()

    def test_first_segment_streams_before_download_finishes(self):
        gate=threading.Event()
        chunk=bytes([0x47])+bytes(187)
        class SlowResponse(Response):
            def read(self,n=-1):
                if self.tell():gate.wait(2)
                return super().read(188)
        def opener(request,timeout):
            if request.full_url.endswith('.m3u8'):
                return Response(b'#EXTM3U\n#EXTINF:10,\na.ts\n#EXT-X-ENDLIST',request.full_url)
            return SlowResponse(chunk*2,request.full_url)
        reader=HLSReader('https://example.org/a.m3u8',{},threading.Event(),opener)
        try:
            start=time.monotonic()
            self.assertEqual(reader.read(188),chunk)
            self.assertLess(time.monotonic()-start,1)
            gate.set()
            self.assertEqual(reader.read(188),chunk)
            self.assertEqual(reader.read(188),b'')
        finally:gate.set();reader.close()

    def test_partial_segment_failure_is_not_replayed(self):
        gate=threading.Event();requests=[]
        chunk=bytes([0x47])+bytes(187)
        class FailingResponse(Response):
            def read(self,n=-1):
                if self.tell():
                    gate.wait(2)
                    raise OSError('connection dropped after prefix')
                return super().read(188)
        def opener(request,timeout):
            if request.full_url.endswith('.m3u8'):
                return Response(b'#EXTM3U\n#EXTINF:10,\na.ts\n#EXT-X-ENDLIST',request.full_url)
            requests.append(request.full_url)
            return FailingResponse(chunk,request.full_url)
        reader=HLSReader('https://example.org/a.m3u8',{},threading.Event(),opener)
        try:
            self.assertEqual(reader.read(188),chunk)
            gate.set()
            with self.assertRaises(OSError):reader.read(188)
            self.assertEqual(len(requests),1)
        finally:gate.set();reader.close()
