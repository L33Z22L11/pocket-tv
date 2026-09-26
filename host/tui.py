#!/usr/bin/env python3
"""Pocket TV terminal channel manager: search, favorites, playback and USB Wi-Fi setup."""
import argparse
import asyncio
import time
import queue
from pathlib import Path
from rich.text import Text
from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Footer, Input, Select, Button, DataTable, Static, RichLog
from catalog import Catalog, channel_key
from controller import PlayerController
from media import Decoder
from sources import PRESETS
from transport import ROOT
from wifi import configure, scan_networks


class ActionButton(Button):
    """Paint the entire button, including padding, during focus/activation."""
    DEFAULT_CSS = '''
    /* Keep Textual's colored buttons with top/bottom edges, no side frame. */
    ActionButton:focus {
        text-style: none !important;
        background-tint: $foreground 12%;
    }
    ActionButton.-active, ActionButton.busy {
        background: #1677b8 !important;
        border-top: tall #86caff !important;
        border-bottom: tall #105684 !important;
        color: #ffffff !important; text-style: bold !important; tint: transparent !important;
    }
    ActionButton:disabled { opacity: 65%; }
    '''
    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs)
        self.active_effect_duration=.35

    async def _on_click(self,event):
        event.stop()
        self.press()  # Keep the highlight without swallowing rapid repeat clicks.


class WifiDialog(ModalScreen):
    CSS = '''
    WifiDialog { align: center middle; background: $background 70%; }
    #wifi-form { width: 72; height: auto; padding: 1 2; border: round $accent; background: $surface; }
    #wifi-form Input { margin-top: 1; }
    #wifi-result { height: auto; min-height: 2; margin-top: 1; }
    #wifi-buttons, #wifi-scan-buttons { height: 3; margin-top: 1; }
    #wifi-buttons Button, #wifi-scan-buttons Button { width: 1fr; }
    #ssid { display: none; }
    '''
    def __init__(self, port=None):
        super().__init__();self.port=port;self.busy=False;self.manual=False

    def compose(self) -> ComposeResult:
        with Vertical(id='wifi-form'):
            yield Static('连接 2.4 GHz Wi-Fi', classes='title')
            yield Static('通过 USB 使用设备扫描附近网络，再选择网络并输入密码。')
            yield Select([],prompt='请选择 2.4 GHz 网络',id='networks')
            with Horizontal(id='wifi-scan-buttons'):
                yield ActionButton('重新扫描',id='scan-wifi')
                yield ActionButton('手动输入',id='manual-wifi')
            yield Input(placeholder='隐藏网络 / 手动输入 SSID',id='ssid')
            yield Input(placeholder='密码（开放网络留空）',password=True,id='password')
            yield Static('准备扫描…',id='wifi-result',markup=False)
            with Horizontal(id='wifi-buttons'):
                yield ActionButton('连接并保存',variant='primary',id='connect-wifi',disabled=True)
                yield ActionButton('取消',id='cancel-wifi')

    def on_mount(self):self.run_worker(self.scan_wifi(),group='wifi-scan')

    def selected_ssid(self):
        if self.manual:return self.query_one('#ssid',Input).value
        value=self.query_one('#networks',Select).value
        return value if isinstance(value,str) else ''

    @on(Input.Changed,'#ssid')
    @on(Select.Changed,'#networks')
    def selection_changed(self):
        self.query_one('#connect-wifi',Button).disabled=self.busy or not self.selected_ssid()

    def busy_state(self,busy):
        self.busy=busy
        for name in ('scan-wifi','manual-wifi','cancel-wifi'):
            self.query_one('#'+name,Button).disabled=busy
        self.selection_changed()

    @on(Button.Pressed,'#manual-wifi')
    def manual_entry(self):
        self.manual=not self.manual
        self.query_one('#ssid',Input).display=self.manual
        self.query_one('#networks',Select).display=not self.manual
        self.query_one('#manual-wifi',Button).label='选择网络' if self.manual else '手动输入'
        if self.manual:self.query_one('#ssid',Input).focus()
        self.selection_changed()

    @on(Button.Pressed,'#scan-wifi')
    def rescan(self):
        if not self.busy:self.run_worker(self.scan_wifi(),group='wifi-scan')

    async def scan_wifi(self):
        self.busy_state(True)
        button=self.query_one('#scan-wifi',Button);button.label='扫描中…';button.add_class('busy')
        self.query_one('#wifi-result',Static).update('设备正在扫描附近的 2.4 GHz 网络…')
        try:
            networks=await asyncio.to_thread(scan_networks,self.port)
            options=[(Text(f"{n['ssid']}  ·  {n['rssi']} dBm  ·  {'加密' if n['secure'] else '开放'}"),n['ssid']) for n in networks]
            self.query_one('#networks',Select).set_options(options)
            self.query_one('#wifi-result',Static).update(f'找到 {len(networks)} 个 2.4 GHz 网络；请选择。隐藏网络可手动输入。' if networks else '未发现网络，可重新扫描或手动输入隐藏网络。')
        except Exception as exc:
            self.query_one('#wifi-result',Static).update(f'扫描失败：{exc}；仍可手动输入。')
        finally:
            button.label='重新扫描';button.remove_class('busy');self.busy_state(False)

    @on(Button.Pressed,'#cancel-wifi')
    def cancel(self):
        if not self.busy:self.dismiss(None)

    @on(Button.Pressed,'#connect-wifi')
    async def connect_wifi(self):
        if self.busy or not self.selected_ssid():return
        ssid=self.selected_ssid();password=self.query_one('#password',Input).value
        self.busy_state(True)
        button=self.query_one('#connect-wifi',Button);button.label='连接中…';button.add_class('busy')
        self.query_one('#wifi-result',Static).update('正在连接网络，等待设备获取地址…')
        try:
            address=await asyncio.to_thread(configure,ssid,password,self.port)
            self.query_one('#password',Input).value=''
            self.dismiss(address)
        except Exception as exc:
            self.query_one('#wifi-result',Static).update(str(exc))
        finally:
            password='';button.label='连接并保存';button.remove_class('busy');self.busy_state(False)


def probe_source(source):
    started=time.monotonic();decoder=Decoder(source,fps=1,prefetch=False)
    try:
        while time.monotonic()-started<20:
            event=decoder.queue.get(timeout=max(.1,20-(time.monotonic()-started)))
            if event.kind=='video':return time.monotonic()-started
            if event.kind in ('end','error'):raise RuntimeError(event.detail or '没有可播放画面')
        raise TimeoutError('20 秒内未解码到首帧')
    except queue.Empty:
        raise TimeoutError('20 秒内未解码到首帧')
    finally:
        decoder.close()


class ChannelTable(DataTable):
    def on_resize(self, event):
        # Textual caches column geometry; invalidate it when the viewport changes.
        width=max(8,event.size.width-12)
        if 'name' in self.columns and self.columns['name'].width!=width:
            self.columns['name'].width=width
            self._require_update_dimensions=True
            self._clear_caches()
            self.refresh(layout=True)


class PocketTV(App):
    TITLE='Pocket TV · 口袋电视'
    SUB_TITLE='国内频道 / USB + Wi-Fi'
    CSS='''
    Screen { background: $background; }
    #source-row { height: 3; margin: 0 1; }
    #source { width: 24; }
    #source-path { width: 1fr; min-width: 10; }
    #refresh { width: 12; min-width: 12; }
    #body { height: 1fr; margin: 1; }
    #library { width: 1fr; min-width: 26; }
    #search, #library-filters { height: 3; }
    #group { width: 1fr; min-width: 10; }
    #favorites { width: 14; min-width: 14; }
    #channels { height: 1fr; border: round $primary; }
    #summary { height: auto; padding: 0 1; color: $text-muted; }
    #workspace { width: 3fr; margin-left: 1; }
    #middle { width: 1fr; height: 1fr; min-width: 20; }
    #right { width: 2fr; height: 1fr; margin-left: 1; }
    #now, #selection, #device, #help {
        height: auto; padding: 1; border: round $primary;
        overflow-x: hidden;
    }
    #now { border: round $accent; }
    #selection { margin-top: 1; }
    #log { height: 1fr; min-height: 6; margin-top: 1; border: round $primary; }
    #controls { height: 3; margin: 0 1; }
    #controls Button { width: 1fr; min-width: 8; }
    .control-row { height: 3; }
    .control-row Button { width: 1fr; min-width: 8; }
    #help { margin-top: 1; color: $text-muted; }
    .compact #source-row { layout: grid; grid-size: 2; grid-columns: 1fr 12; height: 3; }
    .compact #source { width: 1fr; }
    .compact #source-path { display: none; column-span: 2; width: 1fr; }
    .compact.custom-source #source-row { height: 6; }
    .compact.custom-source #source-path { display: block; }
    .compact #workspace { layout: vertical; overflow-y: auto; }
    .compact #middle { width: 1fr; height: auto; overflow-y: hidden; }
    .compact #right { width: 1fr; height: auto; margin: 1 0 0 0; overflow-y: hidden; }
    .compact #log { height: 8; }
    .tiny #body { layout: vertical; overflow-y: auto; }
    .tiny #library { width: 1fr; height: 18; }
    .tiny #workspace { width: 1fr; height: auto; margin: 1 0 0 0; overflow-y: hidden; }
    '''
    BINDINGS=[Binding('ctrl+q','leave','退出'),Binding('ctrl+f','search','搜索'),
              Binding('f5','refresh','刷新',show=False),Binding('space','pause','启停',show=False),
              Binding('f','favorite','收藏'),Binding('n','next','下一台'),
              Binding('p','previous','上一台'),Binding('+','louder','音量+',show=False),
              Binding('-','quieter','音量-',show=False),Binding('h','hints','帮助'),Binding('w','wifi','配网')]

    def __init__(self,source=None,port=None,wifi_host=None,cache=None,autoload=True,autoplay=False):
        super().__init__()
        self.theme='catppuccin-mocha'
        self.initial_source=source or PRESETS['vbskycn'];self.port=port
        self.catalog=Catalog(cache or ROOT/'cache/tui')
        self.channels=[];self.visible_channels=[];self.only_favorites=False;self.health={}
        self.controller=PlayerController(self.player_event,port,wifi_host)
        self.autoload=autoload;self.loading=False;self.autoplay=autoplay;self.buffering=False;self.phase="";self.phase_elapsed=0
        self.device_state={};self.running=False;self.pending_index=None;self.pending_name=""
        self.starting=False;self.probing=False

    def compose(self) -> ComposeResult:
        with Horizontal(id='source-row'):
            yield Select([('国内电视 · IPv4','vbskycn'),('国内电视 · IPv6','vbskycn-ipv6'),('iptv-org 中国','china'),('自定义文件 / URL','custom')],value='vbskycn' if self.initial_source==PRESETS['vbskycn'] else 'custom',allow_blank=False,id='source')
            yield ActionButton('刷新频道',id='refresh',variant='primary')
            yield Input(value=self.initial_source,placeholder='M3U 地址或本地文件',id='source-path')
        with Horizontal(id='body'):
            with Vertical(id='library'):
                yield Input(placeholder='搜索频道…  Ctrl+F',id='search')
                with Horizontal(id='library-filters'):
                    yield Select([('全部','')],value='',allow_blank=False,id='group')
                    yield ActionButton('☆ 只看收藏',id='favorites')
                yield ChannelTable(id='channels',cursor_type='row',zebra_stripes=True)
                yield Static('正在准备频道列表…',id='summary',markup=False)
            with Horizontal(id='workspace'):
                with VerticalScroll(id='middle'):
                    yield Static('尚未播放\n选择频道，回车换台',id='now',markup=False)
                    yield Static('选择频道可查看完整名称、分组和检测结果。',id='selection',markup=False)
                with VerticalScroll(id='right'):
                    yield Static('设备未连接\nUSB 优先\nWi-Fi 自动接续\n按 W 通过 USB 配网',id='device',markup=False)
                    yield RichLog(id='log',wrap=True,min_width=1,max_lines=150,markup=False,highlight=False)
        with Horizontal(id='controls'):
            yield ActionButton('换台',id='play',variant='success')
            yield ActionButton('启停',id='pause')
            yield ActionButton('音量−',id='quieter')
            yield ActionButton('音量+',id='louder')
            yield ActionButton('测首帧',id='probe')
            yield ActionButton('停止',id='stop',variant='error')
        yield Footer()

    def on_mount(self):
        table=self.query_one('#channels',DataTable)
        table.add_column('★',width=2,key='favorite')
        table.add_column('频道',width=20,key='name')
        for name,title in [('channels','频道列表'),('now','正在播放'),('selection','所选频道'),('device','设备状态'),('log','播放日志')]:
            self.query_one('#'+name).border_title=title
        self.update_layout()
        if self.autoload:self.action_refresh()
        else:self.render_channels()

    def selected(self):
        table=self.query_one('#channels',DataTable)
        return self.visible_channels[table.cursor_row] if self.visible_channels and table.cursor_row<len(self.visible_channels) else None

    def render_channels(self):
        table=self.query_one('#channels',DataTable);old=self.selected()
        self.visible_channels=self.catalog.filter(self.channels,self.query_one('#search',Input).value,
                                        self.query_one('#group',Select).value or '',self.only_favorites)
        table.clear()
        for i in self.visible_channels:
            source=self.channels[i];key=channel_key(source)
            table.add_row('★' if key in self.catalog.favorites else '☆',Text(source.name,overflow='ellipsis',no_wrap=True),key=str(i))
        if old in self.visible_channels:table.move_cursor(row=self.visible_channels.index(old))
        self.selection_changed()
        self.call_after_refresh(self.update_layout)
        self.query_one('#summary',Static).update(f'{len(self.visible_channels)} / {len(self.channels)} 个频道 · 回车换台')

    def on_resize(self):
        if self.is_mounted:self.call_after_refresh(self.update_layout)

    def update_layout(self):
        self.screen.set_class(self.query_one('#source',Select).value=='custom','custom-source')
        self.screen.set_class(self.size.width<110,'compact')
        self.screen.set_class(self.size.width<65,'tiny')
    @on(DataTable.RowHighlighted,'#channels')
    def selection_changed(self):
        index=self.selected()
        self.sync_controls()
        if index is None:
            self.query_one('#selection',Static).update('没有匹配的频道')
            return
        source=self.channels[index]
        health=self.health.get(channel_key(source),'未检测')
        self.query_one('#selection',Static).update(f'{source.name}\n\n分组  {source.group or "其他"}\n检测  {health}')

    @on(Input.Changed,'#search')
    @on(Select.Changed,'#group')
    def filter_changed(self):self.render_channels()

    @on(Select.Changed,'#source')
    def source_changed(self,event):
        self.screen.set_class(event.value=='custom','custom-source')
        if event.value in PRESETS:self.query_one('#source-path',Input).value=PRESETS[event.value]

    @on(Input.Submitted,'#source-path')
    def source_submitted(self):self.action_refresh()

    def action_refresh(self):
        if not self.loading:self.run_worker(self.refresh_channels(),group='catalog')

    async def refresh_channels(self):
        self.loading=True;self.query_one('#refresh',Button).disabled=True
        self.query_one('#refresh',Button).label='刷新中…'
        self.query_one('#refresh',Button).add_class('busy')
        self.query_one('#summary',Static).update('正在更新频道，可继续使用播放控制…')
        try:
            channels,cached=await asyncio.to_thread(self.catalog.refresh,self.query_one('#source-path',Input).value.strip())
            self.channels=channels
            self.query_one('#group',Select).set_options([('全部','')]+[(g,g) for g in sorted({c.group for c in channels if c.group})])
            self.query_one('#group',Select).value=''
            self.render_channels()
            if self.autoplay:
                self.autoplay=False
                self.query_one('#channels',DataTable).focus()
                await self.play_selected()
            self.notify('网络暂不可用，使用上次频道缓存' if cached else f'已载入 {len(channels)} 个频道',severity='warning' if cached else 'information')
        except Exception as exc:
            self.query_one('#summary',Static).update(f'加载失败：{exc} · 可以更换地址后重试')
        finally:
            self.loading=False;self.query_one('#refresh',Button).disabled=False
            self.query_one('#refresh',Button).label='刷新频道'
            self.query_one('#refresh',Button).remove_class('busy')

    def sync_controls(self):
        selected=self.selected() is not None
        button=self.query_one('#play',Button)
        button.label='切换中…' if self.pending_index is not None else '换台'
        button.set_class(self.pending_index is not None,'busy')
        button.disabled=not selected
        button.tooltip='切换到左侧选中的频道；再次选择其他频道可打断等待'
        self.query_one('#probe',Button).disabled=not selected or self.probing
        for name in ('pause','louder','quieter'):
            self.query_one('#'+name,Button).disabled=not self.running or not self.device_state
        self.query_one('#stop',Button).disabled=not self.running

    async def play_selected(self):
        index=self.selected()
        if index is None:
            self.notify('没有匹配频道，请清除搜索或收藏筛选');return
        if self.pending_index==index:return
        if (self.running and self.pending_index is None and self.controller.channels==self.channels
                and self.device_state.get('index')==index and self.phase in ('PLAYING','PAUSED')):
            self.notify('已经在这个频道；暂停后请点击继续');return
        restart=self.phase in ('NO SIGNAL','ERROR') and self.device_state.get('index')==index
        self.pending_index=index;self.pending_name=self.channels[index].name
        self.phase='LOADING';self.phase_elapsed=0;self.buffering=True;self.running=True
        self.sync_controls();self.render_playback()
        self.starting=True
        try:
            if restart:await self.controller.start(self.channels,index,restart=True)
            else:await self.controller.start(self.channels,index)
        except Exception as exc:
            self.pending_index=None;self.buffering=False;self.phase='ERROR';self.running=False
            self.device_state={}
            self.query_one('#now',Static).update(f'换台失败\n{exc}\n可重新选择频道重试')
            self.notify(str(exc),severity='error')
        finally:
            self.starting=False;self.sync_controls()

    @on(DataTable.RowSelected,'#channels')
    async def row_selected(self):await self.play_selected()

    def render_playback(self):
        state=self.device_state
        descriptions={'LOADING':'正在连接节目源并解码首帧','BUFFERING':'正在积累音频，可继续选择其他频道',
                      'NO SIGNAL':'暂未收到画面；可换台或停止等待','RECONNECTING':'正在恢复设备连接',
                      'ERROR':'节目源播放失败，可重新选择频道重试'}
        index=state.get('index',-1)
        name=(self.controller.channels[index].name if 0<=index<len(self.controller.channels) else '等待设备回应')
        if self.pending_index is not None:name=self.pending_name
        waiting=self.phase in descriptions
        title=(f'{self.phase} · {self.phase_elapsed:.0f}s' if waiting else
               '已暂停' if not state.get('playing',True) else '播放中')
        if self.pending_index is not None:title='切换中 · '+title
        detail='\n'+descriptions[self.phase] if waiting else ''
        volume=f"\n音量 {state['volume']} · 帮助{'开' if state.get('hints',True) else '关'}" if 'volume' in state else ''
        self.query_one('#now',Static).update(f'{title}\n{name}{detail}{volume}')

    def player_event(self,event):
        if 'log' in event:
            self.query_one('#log',RichLog).write(event['log'])
            if event['log'].startswith('播放失败：'):
                self.pending_index=None;self.buffering=False;self.phase='ERROR'
        if 'exit' in event:
            # Replacing the catalog stops the old child before starting the new one.
            if self.starting:return
            self.running=False;self.pending_index=None;self.buffering=False;self.phase='STOPPED';self.device_state={}
            self.query_one('#now',Static).update('已停止\n选择频道后点击换台' if event['exit']==0 else f"播放器异常退出（{event['exit']}）\n可重新换台；详情见右侧日志")
            self.query_one('#device',Static).update('设备连接已释放\n可重新换台或通过 USB 配网')
            self.query_one('#pause',Button).label='启停'
            self.sync_controls();return
        state=event.get('device',event)
        if isinstance(state,dict) and 'volume' in state:
            self.device_state=state;self.running=True
            self.query_one('#pause',Button).label='暂停' if state.get('playing') else '继续'
            ip=state.get('wifi_ip')
            wifi_text=ip if ip and ip!='0.0.0.0' else '未连接'
            self.query_one('#device',Static).update(f"{state.get('transport','usb').upper()} 已连接\nWi-Fi {wifi_text}\n帧数 {state.get('frames',0)}\n音频缓冲 {state.get('audio_queued',0)/32:.0f} ms\n音频断粮 {state.get('audio_underruns',0)}\n状态每 0.5 秒刷新")
        matches=(self.pending_index is None or self.device_state.get('index')==self.pending_index or event.get('phase')=='RECONNECTING')
        if 'phase' in event and matches:
            self.phase=event['phase'];self.phase_elapsed=event.get('elapsed',0)
            if self.phase in ('PLAYING','PAUSED'):
                if self.pending_index is not None and self.device_state.get('preview',False):
                    self.phase='BUFFERING'
                else:self.pending_index=None;self.buffering=False
            elif self.phase=='NO SIGNAL':self.pending_index=None
        if 'buffering' in event and matches:
            self.buffering=event['buffering']
            if self.buffering:self.phase='BUFFERING'
            elif self.pending_index is None:self.phase='PLAYING' if self.device_state.get('playing',True) else 'PAUSED'
        if self.running or self.pending_index is not None:self.render_playback()
        self.sync_controls()

    async def control(self,command):
        if not await self.controller.send(command):self.notify('请先选择频道播放')
    async def action_pause(self):await self.control('')
    async def action_louder(self):await self.control('+')
    async def action_quieter(self):await self.control('-')
    async def action_next(self):await self.control('n')
    async def action_previous(self):await self.control('p')
    async def action_hints(self):await self.control('h')
    def action_search(self):self.query_one('#search',Input).focus()
    def action_favorite(self):
        index=self.selected()
        if index is not None:self.catalog.toggle(self.channels[index]);self.render_channels()
    async def action_wifi(self):
        await self.controller.stop()
        self.push_screen(WifiDialog(self.port),lambda ip:self.notify(f'Wi-Fi 已连接：{ip}') if ip else None)
    async def action_leave(self):
        await self.controller.stop();self.exit()
    async def on_unmount(self):await self.controller.stop()

    async def probe_selected(self):
        index=self.selected()
        if index is None:return
        source=self.channels[index];key=channel_key(source)
        self.probing=True
        self.health[key]='检测中';self.render_channels()
        button=self.query_one('#probe',Button);button.label='检测中…';button.disabled=True;button.add_class('busy')
        try:
            seconds=await asyncio.to_thread(probe_source,source)
            self.health[key]=f'首帧 {seconds:.1f}s'
        except Exception as exc:
            self.health[key]='不可用';self.notify(f'{source.name}：{exc}',severity='warning')
        self.probing=False;self.render_channels()
        button.label='测首帧';button.remove_class('busy');self.sync_controls()

    @on(Button.Pressed)
    async def button_pressed(self,event):
        name=event.button.id
        if name=='refresh':self.action_refresh()
        elif name=='favorites':
            self.only_favorites=not self.only_favorites
            event.button.label='★ 显示全部' if self.only_favorites else '☆ 只看收藏'
            self.render_channels()
        elif name=='play':await self.play_selected()
        elif name=='favorite':self.action_favorite()
        elif name=='probe':self.run_worker(self.probe_selected(),group='probe',exclusive=True)
        elif name=='wifi':await self.action_wifi()
        elif name=='stop':
            await self.controller.stop()
            self.player_event({'exit':0})
        elif name in ('pause','quieter','louder'):await getattr(self,'action_'+name)()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source',help='初始 M3U 文件或地址（默认 vbskycn IPv4）')
    parser.add_argument('--port')
    parser.add_argument('--wifi-host')
    parser.add_argument('--autoplay',action='store_true',help='频道载入后自动播放第一台')
    args=parser.parse_args()
    PocketTV(args.source,args.port,args.wifi_host,autoplay=args.autoplay).run()


if __name__=='__main__':main()
