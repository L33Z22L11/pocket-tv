#!/usr/bin/env python3
"""Pocket TV: play local files, network video and IPTV over USB or 2.4 GHz Wi-Fi."""
import argparse
import heapq
import json
import queue
import sys
import threading
import time
from pathlib import Path
from connection import DeviceLocator
from roaming import RoamingLink, TransportChanged
from wifi import load_profile
from media import Decoder
from sources import PRESETS, Source, load, remote
from transport import ROOT, Link, instance_lock, HELLO, FRAME, STATUS, KEY, VOLUME, LAYOUT, AUDIO, STOP, SELECT, NOTICE, CHANNEL_NAME


def commands(out):
    for line in sys.stdin:
        out.put(line.strip().lower())


def inspect(source, seconds):
    decoder = Decoder(source, prefetch=False)
    counts = {'video': 0, 'audio': 0, 'landscape': 0, 'portrait': 0}
    started = time.monotonic()
    try:
        while time.monotonic() - started < max(20, seconds * 3):
            event = decoder.queue.get(timeout=16)
            if event.kind == 'error':
                raise RuntimeError(event.detail)
            if event.kind == 'end':
                break
            counts[event.kind] += 1
            if event.kind == 'video':
                counts['landscape' if event.landscape else 'portrait'] += 1
            if event.pts >= seconds:
                break
        if not counts['video']:
            raise RuntimeError('No video frames decoded')
        print(json.dumps({'source': source.name, **counts}, ensure_ascii=False))
    finally:
        decoder.close()


def play(sources, port, seconds, fps, profile=None, wifi_host=None, start_index=0):
    control = queue.Queue()
    threading.Thread(target=commands, args=(control,), daemon=True).start()
    locator = DeviceLocator(port)
    resume = dict(index=start_index, playing=True) if start_index else {}
    deadline = time.monotonic() + seconds if seconds else None
    attempts = 0
    while deadline is None or time.monotonic() < deadline:
        try:
            if profile:
                roaming = RoamingLink(locator, profile, len(sources), wifi_host)
                roaming.state = dict(resume, count=len(sources)) if resume else None
                roaming.connect()
                selected = roaming
            else:
                selected = locator.resolve()
            remaining = max(.001, deadline-time.monotonic()) if deadline else 0
            print(f'播放连接：{selected.kind if profile else selected}', flush=True)
            _play_session(sources, selected, remaining, fps, control, resume)
            return
        except OSError as exc:
            attempts += 1
            if attempts == 1 or attempts % 10 == 0:
                print(f'连接中断，正在自动重连：{exc}', flush=True)
            # One stdin reader for the whole process; q also works while waiting.
            until = time.monotonic() + 1
            while time.monotonic() < until:
                if deadline is not None and time.monotonic() >= deadline:
                    return
                while not control.empty():
                    if control.get_nowait() == 'q':
                        return
                time.sleep(.05)


def _play_session(sources, port, seconds, fps, control, resume):
    roaming = isinstance(port, RoamingLink)
    link, decoder = (port if roaming else Link(port)), None
    disconnected = False
    start = time.monotonic()
    shown = dropped = 0
    index, origin, pause_at = None, None, None
    video, audio = [], []
    serial = 0
    ending = None
    last_underruns = 0
    buffering_reported = False
    load_started=time.monotonic();last_progress=0;preview_sent=False
    previous_phase=None;phase_since=load_started;last_video_sent=load_started
    try:
        state = link.request(HELLO, argument=len(sources))
        if state.get('protocol') != 'PTV6':
            raise RuntimeError('Pocket TV PTV6 firmware is required')
        if resume:
            desired = resume['index'] % len(sources)
            if state['index'] != desired:
                state = link.request(SELECT, argument=desired)
            if state['playing'] != resume['playing']:
                state = link.request(KEY, argument=2)
        print(f"握手成功：频道 {state['index']+1}，音量 {state['volume']}，{'播放' if state['playing'] else '暂停'}", flush=True)
        print(f"Pocket TV — 音量 {state['volume']}（记忆）。短按↑↓调音量，长按↑↓换台，OK启停，长按OK开关帮助。", flush=True)
        last_status = last_report = retry_until = 0
        while not seconds or time.monotonic() - start < seconds:
            if roaming and link.link is None:
                pending = []
                while not control.empty():
                    command = control.get_nowait()
                    if command == 'q':
                        return
                    pending.append(command)
                for command in pending:
                    control.put(command)
            try:
                link.read()
                now = time.monotonic()
                if now - last_status > .2:
                    link.request(STATUS)
                    last_status = now
                while not control.empty():
                    command = control.get_nowait()
                    if command == 'q':
                        return
                    if command.startswith('select '):
                        try:
                            target = int(command[7:])
                        except ValueError:
                            continue
                        if 0 <= target < len(sources):
                            link.request(SELECT, argument=target)
                            if not link.state['playing']:
                                link.request(KEY, argument=2)
                        continue
                    key = {'p': 0, 'n': 1, '': 2, '+': 3, '-': 4, 'h': 5}.get(command)
                    if key is not None:
                        link.request(KEY, argument=key)
                if now-last_progress>.5:
                    phase=('NO SIGNAL' if (ending and ending.kind=='error') or link.state.get('notice')==4 else
                           'BUFFERING' if origin is None and video else
                           'LOADING' if origin is None else
                           'PAUSED' if not link.state['playing'] else
                           'NO SIGNAL' if now-last_video_sent>15 else
                           'BUFFERING' if now-last_video_sent>2.5 else 'PLAYING')
                    if phase!=previous_phase:phase_since=now;previous_phase=phase
                    print(json.dumps({'phase':phase,'elapsed':round(now-phase_since,1),'device':link.state}),flush=True)
                    last_progress=now
                if now < retry_until and index==link.state['index']:
                    time.sleep(.01)
                    continue
                state = link.state
                if index != state['index']:
                    if decoder:
                        decoder.close()
                    index = state['index']
                    print(f'[{index+1}/{len(sources)}] {sources[index].name}', flush=True)
                    decoder = Decoder(sources[index], fps)
                    video.clear(); audio.clear()
                    origin, pause_at, ending = None, None, None
                    last_underruns = state.get("audio_underruns", 0)
                    load_started=now;preview_sent=False;retry_until=0;last_video_sent=now
                    label=sources[index].name.encode('utf-8')[:120].decode('utf-8',errors='ignore').encode('utf-8') or b'CHANNEL'
                    link.request(CHANNEL_NAME,generation=state['generation'],payload=label)
                    link.request(NOTICE,generation=state['generation'],argument=1)
                    buffering_reported = remote(sources[index].url)
                    print(json.dumps({"buffering": buffering_reported}), flush=True)
                # Separate timelines prevent a future video frame from blocking PCM.
                while len(video) + len(audio) < 256 and ending is None:
                    try:
                        event = decoder.queue.get_nowait()
                    except queue.Empty:
                        break
                    if event.kind in ('end', 'error'):
                        ending = event
                        break
                    serial += 1
                    heapq.heappush(video if event.kind == 'video' else audio, (event.pts, serial, event))
                now = time.monotonic()
                if not state['playing'] and not state['preview']:
                    if pause_at is None:
                        pause_at = now
                    time.sleep(.01)
                    continue
                if pause_at is not None:
                    if origin is not None:
                        origin += now - pause_at
                    pause_at = None
                # Live playlists may skip expired segments or reset timestamps.
                # If every available sample is far in the future and the device
                # has drained, wait for content, not for an obsolete wall clock.
                heads = ([video[0][0]] if video else []) + ([audio[0][0]] if audio else [])
                if (remote(sources[index].url) and origin is not None and heads
                        and state['playing'] and not state.get('audio_queued', 0)
                        and origin + min(heads) - now > 2):
                    print('直播时间戳跳变，重新对齐播放时钟', flush=True)
                    origin = None
                # Real live inputs arrive in bursts. Once the FIFO runs dry,
                # accumulate actual PCM instead of restarting for every tiny chunk.
                live_audio = remote(sources[index].url) and getattr(decoder, 'has_audio', False)
                underruns = state.get('audio_underruns', 0)
                if live_audio and underruns > last_underruns and origin is not None:
                    origin = None
                    print('直播音频断流，等待 2 秒内容重新缓冲', flush=True)
                last_underruns = underruns
                if origin is None and video and live_audio and state['volume'] and state['playing']:
                    pcm_seconds = sum(len(item[2].payload) for item in audio) / 32000
                    if pcm_seconds < 2 and ending is None and len(video)+len(audio)<256:
                        if not buffering_reported:
                            print(json.dumps({'buffering': True}), flush=True)
                            buffering_reported = True
                        if not preview_sent:
                            event=video[0][2]
                            if state['landscape']!=event.landscape:link.request(LAYOUT,argument=int(event.landscape))
                            link.request(FRAME,generation=state['generation'],argument=1,payload=event.payload)
                            link.request(NOTICE,generation=state['generation'],argument=2)
                            preview_sent=True
                        time.sleep(.01)
                        continue
                if origin is None and video:
                    if buffering_reported:
                        print(json.dumps({'buffering': False}), flush=True)
                        buffering_reported = False
                    first = min(video[0][0], audio[0][0] if audio else video[0][0])
                    origin = now + 1.0 - first  # one second of network/decode preroll
                if origin is None:
                    if ending:
                        print('播放失败：' + (ending.detail or 'Source has no video frames'), file=sys.stderr, flush=True)
                        link.request(NOTICE,generation=state['generation'],argument=4)
                        retry_until = now + 2
                        if len(sources) > 1:
                            link.request(KEY, argument=1)
                        else:
                            index = None
                        continue
                    time.sleep(.003)
                    continue
                # Send PCM up to 450 ms early, bounded by the device's FIFO credit.
                # Never throw away a chunk merely because a video transfer ran late.
                generation = state['generation']
                for _ in range(24):
                    if not audio or not state['playing']:
                        break
                    now = time.monotonic()
                    target = origin + audio[0][0]
                    if not state['volume'] and target <= now:
                        heapq.heappop(audio)
                        continue
                    if not state['volume'] or target > now + .45:
                        break
                    event = audio[0][2]
                    if state.get('audio_queued', 0) + len(event.payload) > 16000:
                        break
                    if now-target > .25 and state.get('audio_queued', 0) == 0:
                        origin += now-target + .25
                        target = origin + event.pts
                    state = link.request(AUDIO, generation=generation,
                                         argument=max(0, min(2000, round((target-now)*1000))), payload=event.payload)
                    if state['generation'] != generation:
                        break
                    heapq.heappop(audio)
                if link.state['generation'] != generation:
                    continue
                now = time.monotonic()
                if video and origin + video[0][0] <= now + .005:
                    _, _, event = heapq.heappop(video)
                    target = origin + event.pts
                    if not state['volume'] and now-target > .5 and not video and decoder.queue.empty():
                        origin = now-event.pts
                        target = now
                    if now-target > .2 and not state['preview']:
                        dropped += 1
                    else:
                        if state['landscape'] != event.landscape:
                            link.request(LAYOUT, argument=int(event.landscape))
                        response = link.request(FRAME, generation=generation, argument=1, payload=event.payload)
                        shown += int(response['rendered'])
                        if response['rendered']:last_video_sent=time.monotonic()
                if ending and not video and not audio and not link.state.get('audio_queued', 0):
                    if ending.kind == 'error':
                        print(f'播放失败：{ending.detail}', file=sys.stderr, flush=True)
                        retry_until = now + 2
                    if len(sources) > 1:
                        link.request(KEY, argument=1)
                    else:
                        # Reset the FIFO and timeline when looping a single file.
                        link.request(HELLO, argument=len(sources))
                        index = None
                if now-last_report > 5:
                    print(json.dumps({'shown': shown, 'dropped': dropped, 'clock': now-origin, 'next_audio': audio[0][0] if audio else None, 'next_video': video[0][0] if video else None, 'audio_buffer': len(audio), 'video_buffer': len(video), 'decode_queue': decoder.queue.qsize(), 'device': link.state}), flush=True)
                    last_report = now
                time.sleep(.001)
            except TransportChanged:
                # Decoder and buffered PTS survive a handover. HELLO flushes the
                # device FIFO, so start a new one-second preroll at the next PTS.
                origin = pause_at = None
                last_status = 0
                print(json.dumps({'phase':'RECONNECTING'}),flush=True)
                print('连接已切换，正在按当前位置重新缓冲', flush=True)
                continue
            except OSError:
                if not roaming:
                    raise
                origin = pause_at = None
                time.sleep(.05)
                continue
    except OSError:
        disconnected = True
        raise
    finally:
        if link.state and link.state.get('count', 0):
            resume.update(index=link.state['index'], playing=link.state['playing'])
        try:
            if not disconnected:
                link.request(STOP)  # stop output without changing the saved volume
            print(json.dumps({'shown': shown, 'dropped': dropped, 'device': link.state}), flush=True)
        except OSError:
            pass  # A failed STOP must not hide the disconnect or stop retrying.
        finally:
            try:
                link.close()
            except OSError:
                pass
            if decoder:
                decoder.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', nargs='?', default=str(ROOT / 'media'), help='文件、目录、视频 URL 或 M3U/M3U8')
    parser.add_argument('--preset', choices=PRESETS)
    parser.add_argument('--search', default='', help='按频道名/分组筛选')
    parser.add_argument('--list', action='store_true', help='只列出节目，不连接设备')
    parser.add_argument('--inspect', action='store_true', help='解码验证，不连接设备或输出声音')
    parser.add_argument('--seconds', type=float, default=0)
    parser.add_argument('--fps', type=float, default=20)
    parser.add_argument('--port')
    parser.add_argument('--catalog', type=Path, help=argparse.SUPPRESS)
    parser.add_argument('--start-index', type=int, default=0, help='从第几个频道开始，0 为第一个')
    parser.add_argument('--usb-only', action='store_true', help='禁用 Wi-Fi 自动切换')
    parser.add_argument('--wifi-host', help='指定已配对设备 IP（跨网段时跳过广播发现）')
    args = parser.parse_args()
    if not 1 <= args.fps <= 30 or args.seconds < 0:
        parser.error('fps must be 1–30; seconds must be nonnegative')
    sources = ([Source(**item) for item in json.loads(args.catalog.read_text())]
               if args.catalog else load(PRESETS[args.preset] if args.preset else args.source))
    sources = [s for s in sources if args.search.casefold() in (s.name + ' ' + s.group).casefold()]
    if not sources:
        parser.error('没有找到视频；请提供文件、URL 或 --preset')
    if not 0 <= args.start_index < len(sources):
        parser.error('start-index 超出频道范围')
    if args.list:
        for i, source in enumerate(sources):
            print(f'{i+1}\t{source.group}\t{source.name}\t{source.url}')
        return
    if args.inspect:
        for source in sources:
            inspect(source, args.seconds or 3)
        return
    if len(sources) > 10000:
        parser.error('频道超过 10000，请用 --search 筛选')
    profile = None if args.usb_only else load_profile()
    if args.wifi_host and not profile:
        parser.error('--wifi-host 需要先通过 USB 配网，且不能与 --usb-only 同用')
    with instance_lock(ROOT / 'logs' / 'player.lock'):
        play(sources, args.port, args.seconds, args.fps,
             profile, args.wifi_host, args.start_index)


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        print(f'Pocket TV: {exc}', file=sys.stderr)
        sys.exit(1)
