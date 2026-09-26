"""One owned player subprocess; commands are stdin messages, never shell commands."""
import asyncio
from dataclasses import asdict
import json
from pathlib import Path
import sys
import tempfile
from transport import ROOT


class PlayerController:
    def __init__(self, on_event, port=None, wifi_host=None):
        self.on_event, self.port, self.wifi_host = on_event, port, wifi_host
        self.process = None
        self.reader = None
        self.catalog_file = None
        self.channels = []
        self.lock = asyncio.Lock()

    async def start(self, channels, index, restart=False):
        async with self.lock:
            if not restart and self.process and self.process.returncode is None and channels == self.channels:
                if not await self.send(f'select {index}'):
                    raise ConnectionError('播放器已断开，请重新换台')
                return
            await self._stop()
            if not 0 <= index < len(channels):
                raise ValueError('请选择频道')
            with tempfile.NamedTemporaryFile('w',prefix='pocket-tv-',suffix='.json',delete=False,encoding='utf-8') as out:
                json.dump([asdict(c) for c in channels],out,ensure_ascii=False)
                self.catalog_file = Path(out.name)
            command = [sys.executable,'-u',str(ROOT/'host/player.py'),'--catalog',str(self.catalog_file),'--start-index',str(index)]
            if self.port: command += ['--port',self.port]
            if self.wifi_host: command += ['--wifi-host',self.wifi_host]
            try:
                self.process = await asyncio.create_subprocess_exec(*command,stdin=asyncio.subprocess.PIPE,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.STDOUT,cwd=ROOT)
            except BaseException:
                self.catalog_file.unlink(missing_ok=True);self.catalog_file=None
                raise
            self.channels = list(channels)
            self.reader = asyncio.create_task(self._read(self.process))

    async def _read(self, process):
        (ROOT/'logs').mkdir(exist_ok=True)
        with (ROOT/'logs/tui-player.log').open('w',encoding='utf-8') as log:
            while True:
                line = await process.stdout.readline()
                if not line: break
                text = line.decode('utf-8',errors='replace').strip()
                log.write(text+'\n');log.flush()
                try:
                    data=json.loads(text.removeprefix('Control '))
                except ValueError:
                    data={'log':text}
                self.on_event(data)
        code=await process.wait()
        self.on_event({'exit':code})

    async def send(self, command):
        if not self.process or self.process.returncode is not None:
            return False
        try:
            self.process.stdin.write((command+'\n').encode())
            await self.process.stdin.drain()
            return True
        except (BrokenPipeError, ConnectionResetError):
            return False

    async def _stop(self):
        if self.process and self.process.returncode is None:
            await self.send('q')
            try:
                await asyncio.wait_for(self.process.wait(),15)
            except asyncio.TimeoutError:
                self.process.terminate()
                try:
                    await asyncio.wait_for(self.process.wait(),3)
                except asyncio.TimeoutError:
                    self.process.kill();await self.process.wait()
        if self.reader:
            await self.reader;self.reader=None
        if self.catalog_file:
            self.catalog_file.unlink(missing_ok=True);self.catalog_file=None
        self.process=None

    async def stop(self):
        async with self.lock:
            await self._stop()
