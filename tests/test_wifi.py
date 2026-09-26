"""Pairing, identity validation, framed TCP, and automatic transport handover."""
import contextlib
import hmac
import io
import json
import queue
import socket
import struct
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'host'))
import roaming
import player
import wifi
from media import Event, encode_frame
from sources import Source
from transport import Link, NetworkEndpoint, SocketStream, STATUS, HELLO, SELECT, KEY, FRAME, STOP, AUDIO, MAX_PAYLOAD

PROFILE = dict(device_id='aabbccddeeff', key='12'*32, usb_serial='A', host='192.0.2.1')


def state(**changes):
    result = dict(device_id=PROFILE['device_id'], protocol='PTV6', count=2, index=1,
                  playing=False, generation=5, volume=40, preview=True, landscape=True,
                  wifi_ip='192.0.2.1', audio_queued=0, rendered=False)
    result.update(changes)
    return result


class FakeLink:
    def __init__(self, endpoint, data=None):
        self.endpoint = endpoint
        self.state = data or state()
        self.calls = []
        self.closed = False

    def request(self, kind, argument=0, **kwargs):
        self.calls.append(kind)
        if kind == HELLO:
            self.state['count'] = argument
            self.state['generation'] += 1
        if kind == SELECT:
            self.state['index'] = argument
        if kind == KEY:
            self.state['playing'] = not self.state['playing']
        return self.state.copy()

    def read(self):
        if self.closed:
            raise OSError('unplugged')
        return []

    def close(self): self.closed = True


class WifiTests(unittest.TestCase):
    def test_scan_filters_band_deduplicates_and_closes(self):
        rows = [('Home', -70, 1), ('Home', -40, 6), ('Five', -30, 36), ('', -20, 11), ('Guest', -60, 11)]
        class ScanLink(FakeLink):
            def request(self, kind, argument=0, **kwargs):
                ssid, rssi, channel = rows[argument]
                return dict(scan_done=True, scan_count=len(rows), scan_ssid_hex=ssid.encode().hex(),
                            scan_rssi=rssi, scan_channel=channel, scan_auth=3)
        link=ScanLink('usb')
        with patch.object(wifi, 'Link', return_value=link), patch.object(wifi, 'DeviceLocator'), \
             patch.object(wifi, 'instance_lock', return_value=contextlib.nullcontext()):
            networks=wifi.scan_networks()
        self.assertEqual([n['ssid'] for n in networks], ['Home', 'Guest'])
        self.assertEqual(networks[0]['rssi'], -40)
        self.assertEqual(networks[0]['channel'], 6)
        self.assertTrue(link.closed)

    def test_provision_boundaries_and_no_password_in_profile(self):
        key = bytes.fromhex(PROFILE['key'])
        payload = wifi.provision_payload('网络', 'password', key)
        self.assertEqual(len(payload), 130)
        a,b,ssid,password,wirekey = struct.unpack('BB32s64s32s', payload)
        self.assertEqual(ssid[:a].decode(), '网络')
        self.assertEqual(password[:b], b'password')
        self.assertEqual(wirekey, key)
        for ssid,password in [('', 'password'),('x'*33,'password'),('x','short'),('x','x'*64),('x\0','password')]:
            with self.assertRaises(ValueError): wifi.provision_payload(ssid,password,key)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'profile.json'
            wifi.save_profile(PROFILE,path)
            self.assertEqual(wifi.load_profile(path), PROFILE)
            self.assertNotIn('password',path.read_text())
            if sys.platform != 'win32': self.assertEqual(path.stat().st_mode & 0o777,0o600)

    def test_tcp_mutual_auth_and_fragmented_ack(self):
        challenge = b'PTV6'+PROFILE['device_id'].encode()+bytes(range(32))
        key = bytes.fromhex(PROFILE['key'])
        proof = b'OK\n'+hmac.digest(key,b'server'+challenge,'sha256')
        ack = (json.dumps(dict(state(),kind='ack',seq=1,error=0))+'\n').encode()
        class Socket:
            def __init__(self): self.data=bytearray(challenge+proof+ack);self.sent=[];self.closed=False
            def recv(self,n):
                n=min(n,7);chunk=bytes(self.data[:n]);del self.data[:n];return chunk
            def sendall(self,data): self.sent.append(data)
            def setsockopt(self,*args): pass
            def settimeout(self,*args): pass
            def close(self): self.closed=True
        sock=Socket()
        with patch('socket.create_connection',return_value=sock):
            link=Link(NetworkEndpoint('example',PROFILE['device_id'],PROFILE['key']))
            self.assertEqual(link.request(STATUS)['index'],1)
            link.close()
        self.assertEqual(sock.sent[0],hmac.digest(key,b'client'+challenge,'sha256'))
        self.assertTrue(sock.closed)

    def test_wrong_device_and_wrong_server_proof_fail_closed(self):
        for wrong_id,wrong_proof in [(True,False),(False,True)]:
            challenge=b'PTV6'+(b'000000000000' if wrong_id else PROFILE['device_id'].encode())+b'n'*32
            raw=bytearray(challenge+b'OK\n'+b'\0'*32)
            closed=[]
            def recv(n):
                chunk=bytes(raw[:n]);del raw[:n];return chunk
            sock=SimpleNamespace(recv=recv,sendall=lambda data:None,setsockopt=lambda *a:None,settimeout=lambda *a:None,close=lambda:closed.append(True))
            with patch('socket.create_connection',return_value=sock),self.assertRaises(ConnectionError):
                SocketStream(NetworkEndpoint('example',PROFILE['device_id'],PROFILE['key']))
            self.assertTrue(closed)

    def test_usb_preferred_and_no_network_discovery_when_available(self):
        locator=SimpleNamespace(resolve=lambda:'usb',serial_number=None)
        with patch.object(roaming,'Link',FakeLink),patch.object(roaming,'discover') as discovery,contextlib.redirect_stdout(io.StringIO()):
            link=roaming.RoamingLink(locator,PROFILE,2)
            link.connect()
            self.assertEqual(link.kind,'usb')
            self.assertEqual(locator.serial_number,'A')
            discovery.assert_not_called()

    def test_usb_wifi_usb_roundtrip_preserves_device_controls(self):
        available=[True];made=[]
        def resolve():
            if not available[0]: raise FileNotFoundError()
            return 'usb'
        def factory(endpoint):
            device=FakeLink(endpoint);made.append(device);return device
        with patch.object(roaming,'Link',side_effect=factory),patch.object(roaming,'discover',return_value=PROFILE['host']),contextlib.redirect_stdout(io.StringIO()):
            link=roaming.RoamingLink(SimpleNamespace(resolve=resolve),PROFILE,2)
            link.connect();available[0]=False;made[-1].closed=True
            with self.assertRaises(OSError):link.read()
            with self.assertRaises(roaming.TransportChanged):link.read()
            self.assertEqual(link.kind,'wifi')
            self.assertFalse(link.state['playing']);self.assertEqual(link.state['index'],1)
            available[0]=True;link.next_usb=0
            with self.assertRaises(roaming.TransportChanged):link.read()
            self.assertEqual(link.kind,'usb')
            self.assertTrue(made[1].closed)
            self.assertTrue(all(STOP not in device.calls for device in made))

    def test_boot_restores_pause_and_channel_and_rejects_unrelated_usb(self):
        locator=SimpleNamespace(resolve=lambda:'usb')
        bad=FakeLink('usb',state(device_id='000000000000'))
        good=FakeLink('wifi',state(count=0,index=0,playing=True))
        with patch.object(roaming,'Link',side_effect=[bad,good]),patch.object(roaming,'discover',return_value=PROFILE['host']),contextlib.redirect_stdout(io.StringIO()):
            link=roaming.RoamingLink(locator,PROFILE,2);link.state=state();link.connect()
            self.assertNotIn(HELLO,bad.calls)
            self.assertEqual(link.state['index'],1);self.assertFalse(link.state['playing'])
            self.assertIn(SELECT,good.calls);self.assertIn(KEY,good.calls)

    def test_player_keeps_decoder_and_advances_pts_across_handover(self):
        class Clock:
            now=0
            def monotonic(self):return self.now
            def sleep(self,n):self.now+=n
        clock=Clock();opened=[];frames=[]
        class Handover(roaming.RoamingLink):
            def __init__(self):
                self.link=True;self.state=state(playing=True,index=0);self.did_switch=False
            def request(self,kind,**kw):
                if kind==FRAME:
                    frames.append((self.did_switch,float(kw['payload'])))
                    self.state.update(rendered=True,preview=False)
                    clock.now+=.02
                return self.state.copy()
            def read(self):
                if clock.now>1.5 and not self.did_switch:
                    self.did_switch=True;self.state['generation']+=1
                    self.state['preview']=True
                    raise roaming.TransportChanged()
            def close(self):pass
        class Decoder:
            def __init__(self,*args):
                opened.append(True);self.queue=queue.Queue()
                for i in range(200):
                    self.queue.put(Event('audio',i*.05,b'\0'*1024))
                    self.queue.put(Event('video',i*.05,str(i*.05).encode(),True))
            def close(self):pass
        with patch.object(player,'time',clock),patch.object(player,'Decoder',Decoder),contextlib.redirect_stdout(io.StringIO()):
            player._play_session([Source('x','x')],Handover(),4,20,queue.Queue(),{})
        self.assertEqual(len(opened),1)
        before=[pts for switched,pts in frames if not switched]
        after=[pts for switched,pts in frames if switched]
        self.assertTrue(before and after)
        self.assertGreater(min(after),max(before))

    def test_full_size_noisy_frame_fits_bounded_packet(self):
        import av
        from PIL import Image
        import random,zlib
        noise=random.Random(42).randbytes(320*240*3)
        event=encode_frame(av.VideoFrame.from_image(Image.frombytes('RGB',(320,240),noise)))
        self.assertLessEqual(len(event.payload),MAX_PAYLOAD)
        self.assertEqual(len(zlib.decompress(event.payload[516:])),320*240)
