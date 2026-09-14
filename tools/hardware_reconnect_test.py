"""macOS/Linux: reset the attached ESP32-C3 and verify the same player reconnects.
Run with the ESP-IDF Python (esptool available). Leaves CCTV playing on success.
"""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
log_path = ROOT / 'logs' / 'cctv-reconnect-live.log'
with log_path.open('w') as output:
    player = subprocess.Popen([str(ROOT/'.venv/bin/python'), '-u', str(ROOT/'host/player.py'), str(ROOT/'config/cctv.m3u')], cwd=ROOT, stdin=subprocess.PIPE, stdout=output, stderr=subprocess.STDOUT, text=True, start_new_session=True)
(ROOT/'logs/cctv-live.pid').write_text(str(player.pid)+'\n')
print('PLAYER', player.pid, flush=True)


def states(text):
    result=[]
    for line in text.splitlines():
        try:
            item=json.loads(line.removeprefix('Control '))
            result.append(item.get('device',item))
        except ValueError:
            pass
    return result


def wait_for(predicate, offset=0, timeout=55):
    deadline=time.monotonic()+timeout
    while time.monotonic()<deadline:
        if player.poll() is not None:
            raise RuntimeError(f'Player exited {player.returncode}: {log_path.read_text()[-1000:]}')
        text=log_path.read_text()[offset:]
        if predicate(text): return text
        time.sleep(.25)
    raise TimeoutError(f'Checkpoint timed out; inspect {log_path}')


wait_for(lambda t: any(s.get('frames',0)>20 and not s.get('preview',True) for s in states(t)))
print('INITIAL PLAYBACK PASS', flush=True)
player.stdin.write('n\n');player.stdin.flush()
text=wait_for(lambda t: any(s.get('index')==1 and not s.get('preview',True) for s in states(t)))
before=states(text)[-1]
assert before['index']==1
print('CHANNEL 2 PLAYBACK PASS', flush=True)
offset=len(log_path.read_text())
# Suspend serial reads briefly so esptool can reset without competing readers.
os.kill(player.pid,signal.SIGSTOP)
try:
    with (ROOT/'logs/reconnect-reset.log').open('w') as out:
        result=subprocess.run([sys.executable,'-m','esptool','--chip','esp32c3','--port','/dev/cu.usbmodem1101','--after','hard_reset','chip_id'],stdout=out,stderr=subprocess.STDOUT,timeout=25)
    if result.returncode:
        raise RuntimeError('Reset failed; see logs/reconnect-reset.log')
finally:
    os.kill(player.pid,signal.SIGCONT)
print('HARDWARE RESET DONE', flush=True)
text=wait_for(lambda t: 'USB 握手成功：频道 2' in t and any(s.get('index')==1 and s.get('frames',0)>20 and not s.get('preview',True) for s in states(t)),offset)
after=states(text)[-1]
assert after['volume']==before['volume']
report=dict(result='PASS',player_pid=player.pid,same_process=player.poll() is None,before=before,after=after)
(ROOT/'logs/hardware-reconnect.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
print('AUTO RECONNECT PASS: same process, channel 2, saved volume',after['volume'],flush=True)
