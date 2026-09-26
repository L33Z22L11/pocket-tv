import contextlib
import io
import json
import queue
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'host'))
import connection
import player
from transport import Link, DeviceRestarted, HELLO, SELECT, KEY, STOP


def port(path, serial):
    return SimpleNamespace(device=path,serial_number=serial,location='usb-'+serial,vid=0x303a,pid=0x1001)

class Clock:
    def __init__(self): self.now=0
    def monotonic(self): return self.now
    def sleep(self, seconds): self.now+=seconds

class ConnectionTests(unittest.TestCase):
    def test_port_renumbering_tracks_original_device(self):
        finder=connection.DeviceLocator('/dev/old')
        with patch.object(connection.list_ports,'comports',side_effect=[
            [port('/dev/old','A')], [port('/dev/unrelated','B')],
            [port('/dev/unrelated','B'),port('/dev/new','A')]]):
            self.assertEqual(finder.resolve(),'/dev/old')
            with self.assertRaises(FileNotFoundError): finder.resolve()
            self.assertEqual(finder.resolve(),'/dev/new')

    def test_boot_message_invalidates_session_without_losing_channel(self):
        link=Link.__new__(Link)
        link.established=True
        link.state={'index':1,'count':2,'playing':False,'generation':8}
        raw=(json.dumps({'kind':'boot','count':0,'index':0,'generation':1})+'\n').encode()
        link.serial=SimpleNamespace(in_waiting=len(raw),read=lambda n:raw)
        link.buffer=bytearray()
        with self.assertRaises(DeviceRestarted): link.read()
        self.assertEqual(link.state['index'],1)
        self.assertFalse(link.state['playing'])

    def test_supervisor_retries_and_keeps_resume_state(self):
        seen=[]
        def session(sources, path, seconds, fps, control, resume):
            seen.append((path,resume.copy(),control))
            if len(seen)==1:
                resume.update(index=1,playing=False)
                raise OSError('Device not configured')
        with patch.object(player,'DeviceLocator') as finder, patch.object(player,'_play_session',side_effect=session), patch.object(player.threading,'Thread') as thread, patch.object(player,'time',Clock()), contextlib.redirect_stdout(io.StringIO()):
            finder.return_value.resolve.side_effect=['/dev/old','/dev/new']
            player.play(['a','b'],None,10,20)
            thread.return_value.start.assert_called_once()
        self.assertEqual(seen[1][0],'/dev/new')
        self.assertEqual(seen[1][1],{'index':1,'playing':False})
        self.assertIs(seen[0][2],seen[1][2])

    def test_reconnect_restores_channel_pause_without_setting_volume(self):
        calls=[]
        class Device:
            def __init__(self, path):
                self.state=dict(protocol='PTV6',count=2,index=0,playing=True,volume=55)
            def request(self, kind, argument=0):
                calls.append(kind)
                if kind==SELECT: self.state['index']=argument
                if kind==KEY: self.state['playing']=not self.state['playing']
                return self.state.copy()
            def close(self): pass
        with patch.object(player,'Link',Device),contextlib.redirect_stdout(io.StringIO()):
            resume=dict(index=1,playing=False)
            player._play_session(['a','b'],'fake',-1,20,queue.Queue(),resume)
        self.assertEqual(calls,[HELLO,SELECT,KEY,STOP])
        self.assertEqual(resume,{'index':1,'playing':False})

    def test_packet_timeout_and_crc_error_trigger_supervisor_reconnect(self):
        for code in (1, 2):
            link=Link.__new__(Link)
            link.replies={7:dict(error=code)}
            with self.assertRaises(ConnectionError):link.collect(7)
            link.replies={7:dict(error=code)}
            self.assertEqual(link.collect(7,allow_error=True)['error'],code)
        link=Link.__new__(Link);link.replies={7:dict(error=131)}
        with self.assertRaises(RuntimeError):link.collect(7)
