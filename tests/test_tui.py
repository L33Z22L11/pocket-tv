import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'host'))
from sources import Source
from catalog import Catalog
try:
    from tui import PocketTV, WifiDialog
    from textual.widgets import DataTable, Input, Select, Static
except ImportError:
    PocketTV=None


class CatalogTests(unittest.TestCase):
    def test_refresh_failure_uses_last_good_and_favorites_persist(self):
        with tempfile.TemporaryDirectory() as folder:
            catalog=Catalog(folder);channels=[Source('CCTV-1','https://example.org/a','央视'),Source('湖南卫视','https://example.org/b','卫视')]
            with patch('catalog.load',return_value=channels):self.assertEqual(catalog.refresh('url'),(channels,False))
            with patch('catalog.load',side_effect=OSError('offline')):self.assertEqual(catalog.refresh('url'),(channels,True))
            catalog.toggle(channels[1]);catalog=Catalog(folder)
            self.assertEqual(catalog.filter(channels,favorites=True),[1])
            self.assertEqual(catalog.filter(channels,'CCTV 央视'),[0])
            self.assertEqual(catalog.filter(channels,group='卫视'),[1])


@unittest.skipIf(PocketTV is None,'Install requirements-tui.txt for terminal UI tests')
class TUITests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        scan=patch('tui.scan_networks',return_value=[dict(ssid='Home24',rssi=-42,channel=6,secure=True)])
        scan.start();self.addCleanup(scan.stop)

    async def test_search_favorite_play_and_wifi_modal(self):
        with tempfile.TemporaryDirectory() as folder:
            app=PocketTV(cache=folder,autoload=False)
            app.controller.start=AsyncMock();app.controller.stop=AsyncMock();app.controller.send=AsyncMock(return_value=True)
            async with app.run_test(size=(110,36)) as pilot:
                self.assertEqual(app.theme,'catppuccin-mocha')
                app.channels=[Source('CCTV-1','https://example.org/a','央视'),Source('湖南卫视','https://example.org/b','卫视')]
                app.render_channels();await pilot.pause()
                self.assertEqual(app.query_one('#channels',DataTable).row_count,2)
                app.query_one('#search',Input).value='湖南';await pilot.pause()
                self.assertEqual(app.visible_channels,[1])
                app.query_one('#channels',DataTable).focus();await pilot.press('f')
                self.assertEqual(len(app.catalog.favorites),1)
                await pilot.press('enter');app.controller.start.assert_awaited_with(app.channels,1)
                await pilot.press('space');app.controller.send.assert_awaited_with('')
                await pilot.press('w');await pilot.pause()
                self.assertIsInstance(app.screen,WifiDialog)
                self.assertTrue(app.screen.query_one('#password',Input).password)
                await pilot.click('#cancel-wifi');await pilot.pause()
                self.assertNotIsInstance(app.screen,WifiDialog)

    async def test_wifi_errors_and_success_do_not_expose_password(self):
        with tempfile.TemporaryDirectory() as folder:
            app=PocketTV(cache=folder,autoload=False);app.controller.stop=AsyncMock()
            async with app.run_test(size=(100,34)) as pilot:
                await app.action_wifi();await pilot.pause()
                await pilot.click('#manual-wifi');await pilot.pause()
                app.screen.query_one('#ssid',Input).value='test'
                app.screen.query_one('#password',Input).value='not-a-real-password'
                with patch('tui.configure',side_effect=RuntimeError('USB not connected')):
                    await pilot.click('#connect-wifi');await pilot.pause(.2)
                self.assertIn('USB not connected',str(app.screen.query_one('#wifi-result',Static).content))
                with patch('tui.configure',return_value='192.0.2.1'):
                    await pilot.click('#connect-wifi');await pilot.pause(.2)
                self.assertNotIsInstance(app.screen,WifiDialog)

    async def test_small_terminal_and_remote_text_are_literal(self):
        with tempfile.TemporaryDirectory() as folder:
            app=PocketTV(cache=folder,autoload=False);app.controller.stop=AsyncMock()
            async with app.run_test(size=(80,24)) as pilot:
                app.channels=[Source('[red]频道[/red]','https://example.org/a')];app.render_channels()
                app.controller.channels=app.channels
                app.player_event({'device':dict(volume=40,index=0,playing=True,transport='wifi',wifi_ip='192.0.2.1')})
                await pilot.pause()
                self.assertIn('[red]',str(app.query_one('#now',Static).content))
                self.assertGreaterEqual(app.query_one('#now',Static).content_size.height,3)
                self.assertGreaterEqual(app.query_one('#device',Static).content_size.height,4)

    async def test_wifi_network_selection_and_full_button_highlight(self):
        with tempfile.TemporaryDirectory() as folder:
            app=PocketTV(cache=folder,autoload=False);app.controller.stop=AsyncMock()
            async with app.run_test(size=(100,36)) as pilot:
                await app.action_wifi();await pilot.pause(.2)
                screen=app.screen
                self.assertFalse(screen.query_one('#ssid',Input).display)
                screen.query_one('#networks',Select).value='Home24'
                screen.query_one('#password',Input).value='test-password'
                await pilot.pause()
                with patch('tui.configure',return_value='192.0.2.1') as configure:
                    await pilot.click('#connect-wifi');await pilot.pause(.2)
                    configure.assert_called_once_with('Home24','test-password',None)
                button=app.query_one('#louder')
                button.add_class('-active');await pilot.pause()
                self.assertEqual(button.styles.background.hex.lower(),'#1677b8')
                self.assertFalse(button.styles.text_style.reverse)

    async def test_switch_lifecycle_ignores_old_channel_and_resets_on_frame(self):
        with tempfile.TemporaryDirectory() as folder:
            app=PocketTV(cache=folder,autoload=False)
            app.controller.start=AsyncMock();app.controller.stop=AsyncMock()
            async with app.run_test(size=(120,36)) as pilot:
                app.channels=[Source('Old','https://example.org/a'),Source('New','https://example.org/b')]
                app.controller.channels=app.channels;app.render_channels()
                app.query_one('#channels',DataTable).move_cursor(row=1)
                await pilot.pause();await app.play_selected()
                self.assertEqual(str(app.query_one('#play').label),'切换中…')
                app.player_event({'phase':'PLAYING','device':dict(volume=40,index=0,playing=True,preview=False)})
                self.assertEqual(app.pending_index,1)
                self.assertIn('New',str(app.query_one('#now',Static).content))
                app.player_event({'phase':'PLAYING','device':dict(volume=40,index=1,playing=True,preview=True)})
                self.assertEqual(app.pending_index,1)
                app.player_event({'phase':'PLAYING','device':dict(volume=40,index=1,playing=True,preview=False)})
                self.assertIsNone(app.pending_index)
                self.assertEqual(str(app.query_one('#play').label),'换台')
                self.assertFalse(app.query_one('#play').has_class('busy'))
                self.assertNotIn('切换中',str(app.query_one('#now',Static).content))
                await app.play_selected()
                self.assertEqual(app.controller.start.await_count,1,'same healthy channel must not restart')
                app.player_event({'exit':0})
                self.assertTrue(app.query_one('#pause').disabled)
                self.assertTrue(app.query_one('#stop').disabled)
                self.assertEqual(str(app.query_one('#play').label),'换台')

    async def test_failed_switch_and_empty_search_leave_no_stuck_actions(self):
        with tempfile.TemporaryDirectory() as folder:
            app=PocketTV(cache=folder,autoload=False)
            app.controller.start=AsyncMock(side_effect=OSError('cannot start'))
            app.controller.stop=AsyncMock()
            async with app.run_test(size=(120,36)) as pilot:
                app.channels=[Source('CCTV1','https://example.org/a')];app.render_channels()
                await app.play_selected()
                self.assertEqual(str(app.query_one('#play').label),'换台')
                self.assertNotIn('切换中',str(app.query_one('#now',Static).content))
                self.assertFalse(app.query_one('#play').disabled)
                app.pending_index=0;app.running=True
                app.player_event({'log':'播放失败：source timed out'})
                self.assertIsNone(app.pending_index)
                self.assertEqual(str(app.query_one('#play').label),'换台')
                app.query_one('#search',Input).value='no match';await pilot.pause()
                self.assertTrue(app.query_one('#play').disabled)
                self.assertTrue(app.query_one('#probe').disabled)

    async def test_no_signal_retry_and_buffering_after_success(self):
        with tempfile.TemporaryDirectory() as folder:
            app=PocketTV(cache=folder,autoload=False)
            app.controller.start=AsyncMock();app.controller.stop=AsyncMock()
            async with app.run_test(size=(120,36)) as pilot:
                app.channels=[Source('CCTV1','https://example.org/a')]
                app.controller.channels=app.channels;app.render_channels()
                app.player_event({'phase':'NO SIGNAL','device':dict(volume=40,index=0,playing=True)})
                await app.play_selected()
                app.controller.start.assert_awaited_with(app.channels,0,restart=True)
                app.player_event({'phase':'PLAYING','device':dict(volume=40,index=0,playing=True,preview=False)})
                app.player_event({'buffering':True})
                self.assertEqual(str(app.query_one('#play').label),'换台')
                app.player_event({'buffering':False})
                self.assertNotIn('BUFFERING',str(app.query_one('#now',Static).content))
