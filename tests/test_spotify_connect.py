# SPDX-License-Identifier: MPL-2.0
import asyncio
import io
import json
from pathlib import Path
import tarfile
import tempfile
import time
import unittest
import uuid
from unittest.mock import AsyncMock, Mock, patch

from cast_audio_lab.spotify_connect import SpotifyStore, SoloistInput, child_environment, media_metadata
from cast_audio_lab.soloist_assets import _install, download_url
from cast_audio_lab.routes import RouteStore, Route
from cast_audio_lab.route_manager import RouteManager
from cast_audio_lab.audio_inputs import SharedAudioOutput
from cast_audio_lab.backend import MediaMetadata, NullAudioBackend
from cast_audio_lab.pcm import PcmBuffer


class SpotifyStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = SpotifyStore(self.temp.name)
        self.ident = str(uuid.uuid4())

    def test_disabled_default_and_secret_never_returned(self):
        self.assertFalse(self.store.public(self.ident)['enabled'])
        value = self.store.updated(self.ident, {'enabled': True, 'api_key': 'private-key'}, 'Speaker')
        self.store.save(self.ident, value)
        self.assertEqual(self.store.path(self.ident).stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.store.public(self.ident), {'enabled': True, 'name': 'Speaker', 'key_set': True})
        self.assertEqual(self.store.updated(self.ident, {'name': 'New'}, 'Speaker')['api_key'], 'private-key')

    def test_invalid_keys_flags_and_extra_fields_rejected(self):
        for body in ({'enabled': 1}, {'api_key': 'line\nbreak'}, {'enabled': True}, {'password': 'x'}, {'api_key': 'x'*16385}):
            with self.subTest(body=list(body)), self.assertRaises(ValueError):
                self.store.updated(self.ident, body, 'Speaker')

    def test_public_permissions_and_symlinks_rejected(self):
        self.store.save(self.ident, {'enabled': False, 'name': '', 'api_key': ''})
        path = self.store.path(self.ident)
        path.chmod(0o644)
        with self.assertRaises(ValueError):self.store.load(self.ident)
        path.unlink();path.symlink_to(Path(self.temp.name)/'missing')
        with self.assertRaises(ValueError):self.store.load(self.ident)

    def test_child_does_not_inherit_supervisor_or_other_credentials(self):
        with patch.dict('os.environ', {'SUPERVISOR_TOKEN': 'secret', 'SECRET': 'hidden'}):
            self.assertNotIn('SUPERVISOR_TOKEN', child_environment())
            self.assertNotIn('SECRET', child_environment())


class InstallerTests(unittest.TestCase):
    def archive(self, name='soloist', kind=None):
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode='w:gz') as archive:
            member = tarfile.TarInfo(name);member.size = 4
            if kind:member.type = kind;member.linkname = '/tmp/other'
            archive.addfile(member, io.BytesIO(b'test'))
        return buffer.getvalue()

    def response(self, data, url='https://soloist-builds.spotifycdn.com/file'):
        response = io.BytesIO(data);response.geturl = lambda: url
        return response

    def test_architecture_urls_and_unsupported_arch(self):
        self.assertIn('_arm64.', download_url('aarch64'))
        self.assertIn('_x86_64.', download_url('amd64'))
        with self.assertRaises(ValueError):download_url('riscv64')

    def test_private_runtime_install_records_provenance_and_reuses_fresh_binary(self):
        with tempfile.TemporaryDirectory() as directory, patch('cast_audio_lab.soloist_assets.build_timestamp', return_value=int(time.time())):
            opener = Mock(side_effect=lambda *a, **kw:self.response(self.archive()))
            executable = _install(directory, opener=opener)
            self.assertEqual(executable.stat().st_mode & 0o777, 0o700)
            record = json.loads((Path(directory)/'source.json').read_text())
            self.assertEqual(len(record['sha256']), 64)
            self.assertEqual(_install(directory, opener=opener), executable)
            self.assertEqual(opener.call_count, 1)

    def test_traversal_symlink_redirect_and_expired_download_rejected(self):
        cases = [(self.archive('../soloist'), None), (self.archive(kind=tarfile.SYMTYPE), None),
                 (self.archive(), 'https://example.org/file')]
        for payload, url in cases:
            with self.subTest(url=url), tempfile.TemporaryDirectory() as directory:
                with self.assertRaises(ValueError):
                    _install(directory, opener=lambda *a, **kw:self.response(payload, url or 'https://soloist-builds.spotifycdn.com/file'))
        with tempfile.TemporaryDirectory() as directory, patch('cast_audio_lab.soloist_assets.build_timestamp', return_value=int(time.time())-91*86400):
            with self.assertRaises(ValueError):_install(directory, opener=lambda *a, **kw:self.response(self.archive()))
            self.assertFalse((Path(directory)/'soloist').exists())


class SpotifyLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.output = SharedAudioOutput(NullAudioBackend())
        self.source = self.output.input('spotify')
        self.player = SoloistInput(self.source, config_path=Path(self.temp.name)/'config', state_dir=Path(self.temp.name)/'state', client_dir=Path(self.temp.name)/'client', bus_address='private')
        self.player.command = AsyncMock()
        self.addAsyncCleanup(self.output.close)
        self.addAsyncCleanup(self.player.close)

    def state(self, **changes):
        return dict(status='playing', is_active=True, position={'position_ms':1000}, item={'uri':'track:one','decorations':{'identity':{'name':'Track'}}}, **changes)

    async def test_pause_resume_seek_and_track_changes(self):
        self.player._start_audio = AsyncMock()
        self.player._stop_audio = AsyncMock()
        await self.player._state(self.state())
        self.player._start_audio.assert_awaited_once()
        await self.source.load('audio', 'audio/pcm', True, 0)
        changed = self.state();changed['position']={'position_ms':30000}
        await self.player._state(changed)
        self.assertEqual(self.player._start_audio.await_count, 2)
        paused=self.state();paused['status']='paused'
        await self.player._state(paused)
        self.player._stop_audio.assert_awaited()
        await self.player._state(self.state())
        self.assertEqual(self.player._start_audio.await_count, 3)

    async def test_cast_takeover_suppresses_late_spotify_state(self):
        await self.source.load('old', 'audio/pcm', True, 0)
        self.player._status='playing';self.player._active=True
        cast=self.output.input('cast')
        await cast.load('new', 'audio/mpeg', True, 0)
        self.player.command.assert_awaited_with('pause')
        self.player._start_audio=AsyncMock()
        await self.player._state(self.state())
        self.player._start_audio.assert_not_awaited()
        await self.player._stop_audio()
        self.assertEqual(cast.status().url, 'new')

    async def test_early_track_change_replaces_pcm_and_normal_end_keeps_it(self):
        self.player._start_audio = AsyncMock()
        self.source.update_pcm_metadata = AsyncMock()
        first = self.state()
        first["item"]["decorations"]["playback"] = {"duration_ms": 120000}
        await self.player._state(first)
        await self.source.load("audio", "audio/pcm", True, 0)

        second = self.state()
        second["item"]["uri"] = "track:two"
        second["item"]["decorations"]["playback"] = {"duration_ms": 120000}
        await self.player._state(second)
        self.assertEqual(self.player._start_audio.await_count, 2)

        self.player._position = 114
        third = self.state()
        third["item"]["uri"] = "track:three"
        await self.player._state(third)
        self.assertEqual(self.player._start_audio.await_count, 2)
        self.source.update_pcm_metadata.assert_awaited_once()

    async def test_pause_retains_output_briefly_and_then_releases_it(self):
        await self.source.load("audio", "audio/pcm", True, 0)
        self.output.backend.park_pcm = AsyncMock(return_value=True)
        previous = self.player._buffer = PcmBuffer()
        self.player._load_task = asyncio.get_running_loop().create_future()
        self.player._load_task.set_result(None)
        paused = self.state()
        paused["status"] = "paused"

        with patch("cast_audio_lab.spotify_connect._PAUSED_TRANSPORT_SECONDS", 0.01):
            await self.player._state(paused)
            expiry = self.player._park_expiry
            self.assertTrue(self.source.owns_output)
            self.assertFalse(previous.feed(b"stale PCM"))
            await self.player._state(paused)
            self.assertIs(self.player._park_expiry, expiry)
            self.output.backend.park_pcm.assert_awaited_once()
            await asyncio.wait_for(expiry, 1)
        self.assertFalse(self.source.owns_output)

    async def test_replacing_live_pcm_cancels_old_pump_before_closing_buffer(self):
        hardware = self.output.backend
        pump = None

        async def load_pcm(buffer, **kwargs):
            nonlocal pump
            if pump:
                pump.cancel()
                await asyncio.gather(pump, return_exceptions=True)

            async def read():
                if not await buffer.read(4):
                    hardware._status.state = "IDLE"
                    hardware._status.idle_reason = "ERROR"
                    hardware._emit_status()

            pump = asyncio.create_task(read())
            await hardware.load("pcm:live", "audio/pcm", True, 0)

        hardware.load_pcm = load_pcm
        try:
            await self.player._start_audio(MediaMetadata(), 0)
            await self.player._load_task
            previous = self.player._buffer
            await self.player._start_audio(MediaMetadata(), 30)
            await self.player._load_task
            self.player.command.assert_not_awaited()
            self.assertFalse(previous.feed(b"stale PCM"))
            self.assertTrue(self.source.owns_output)
        finally:
            if pump:
                pump.cancel()
                await asyncio.gather(pump, return_exceptions=True)

    async def test_control_failure_does_not_block_cast_takeover(self):
        await self.source.load('old', 'audio/pcm', True, 0)
        self.player.command.side_effect=RuntimeError('private-token')
        cast=self.output.input('cast')
        with self.assertLogs('cast_audio_lab.spotify_connect', level='INFO') as logs:
            await cast.load('new', 'audio/mpeg', True, 0)
        self.assertNotIn('private-token', ''.join(logs.output))
        self.assertEqual(cast.status().url, 'new')

    async def test_retry_is_bounded_and_errors_are_redacted(self):
        self.player._instance=AsyncMock(side_effect=RuntimeError('secret-account'))
        self.player._cleanup=AsyncMock()
        with patch('cast_audio_lab.spotify_connect.asyncio.sleep', new_callable=AsyncMock), self.assertLogs('cast_audio_lab.spotify_connect', level='INFO') as logs:
            await self.player.run()
        self.assertEqual(self.player._instance.await_count,3)
        self.assertEqual(self.player._cleanup.await_count,3)
        self.assertNotIn('secret-account', ''.join(logs.output))

    async def test_cancellation_during_spawn_reaps_completed_child(self):
        entered=asyncio.Event();finish=asyncio.Event()
        child=Mock(returncode=None,wait=AsyncMock(return_value=0))
        async def spawn(*a,**kw):entered.set();await finish.wait();return child
        with patch('cast_audio_lab.spotify_connect.asyncio.create_subprocess_exec', side_effect=spawn):
            task=asyncio.create_task(self.player._spawn('fake'))
            await entered.wait();task.cancel();await asyncio.sleep(0);finish.set()
            with self.assertRaises(asyncio.CancelledError):await task
        child.terminate.assert_called_once()
        child.wait.assert_awaited()
        self.assertEqual(self.player._processes,[])

    async def test_invalid_positions_rejected(self):
        for value in (float('nan'),-1,'token'):
            state=self.state();state['position']={'position_ms':value}
            with self.assertRaises(ValueError):await self.player._state(state)

    def test_metadata_is_bounded_and_artwork_uses_known_origin(self):
        item={'uri':'track', 'decorations':{'identity':{'name':'Song\nName'},'playback':{'duration_ms':120000},'visual_identity':{'cover':[{'url':'http://127.0.0.1/private'},{'url':'https://i.scdn.co/image/test'}]}}}
        result=media_metadata(item)
        self.assertEqual(result.title,'Song Name');self.assertEqual(result.duration,120)
        self.assertEqual(result.artwork_url,'https://i.scdn.co/image/test')


class SpotifyManagerTests(unittest.IsolatedAsyncioTestCase):
    async def test_key_replacement_clears_only_its_account_state_after_worker_stops(self):
        with tempfile.TemporaryDirectory() as directory:
            store=RouteStore(directory);manager=RouteManager(store)
            route=Route.parse({'id':str(uuid.uuid4()),'name':'Room (Cast)','backend':'airplay','enabled':True,'target':{'host':'192.0.2.1','port':7000,'protocol':'airplay2','device_id':'test-device','txt':{}}})
            manager.routes=[route];manager._ensure_spotify=AsyncMock();manager._start_missing=Mock()
            state=Path(directory)/'spotify'/route.id/'session';state.mkdir(parents=True)
            (state/'credentials').write_text('old-account')
            other=Path(directory)/'spotify'/'other';other.mkdir();(other/'keep').touch()
            manager.spotify_store.save(route.id,{'enabled':True,'name':'Room (Spotify)','api_key':'old'})
            stopped=asyncio.Event()
            async def worker():
                try:await asyncio.Event().wait()
                finally:stopped.set()
            task=asyncio.create_task(worker());await asyncio.sleep(0);manager.tasks[route.id]=task
            result=await manager.configure_spotify(route,{'api_key':'new'})
            self.assertTrue(stopped.is_set());self.assertFalse(state.exists());self.assertTrue((other/'keep').exists())
            self.assertNotIn('api_key',result)

class SpotifyApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from aiohttp.test_utils import TestClient, TestServer
        from cast_audio_lab.management import create_app
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.manager=RouteManager(RouteStore(self.temp.name))
        self.route=Route.parse({'id':str(uuid.uuid4()),'name':'Speaker','backend':'airplay','target':{'host':'192.0.2.1','device_id':'fixture'}})
        self.manager.routes=[self.route]
        self.manager.store.save([self.route])
        self.manager._start_missing=Mock();self.manager._ensure_spotify=AsyncMock()
        self.client=TestClient(TestServer(create_app(self.manager)))
        await self.client.start_server();self.addAsyncCleanup(self.client.close)
        self.url='/api/routes/'+self.route.id+'/spotify'

    async def test_save_get_support_and_errors_never_echo_key(self):
        response=await self.client.post(self.url,json={'enabled':True,'api_key':'private-api-key'})
        self.assertEqual(response.status,200)
        self.assertNotIn('private-api-key',await response.text())
        response=await self.client.get(self.url)
        self.assertTrue((await response.json())['key_set'])
        self.assertNotIn('private-api-key',json.dumps(self.manager.support_snapshot(version='test')))
        response=await self.client.post(self.url,json={'api_key':'private-api-key\nbad'})
        self.assertEqual(response.status,400)
        self.assertNotIn('private-api-key',await response.text())

    async def test_cross_origin_key_changes_and_unknown_routes_rejected(self):
        response=await self.client.post(self.url,json={'api_key':'secret'},headers={'Origin':'http://untrusted.invalid'})
        self.assertEqual(response.status,403)
        response=await self.client.get('/api/routes/'+str(uuid.uuid4())+'/spotify')
        self.assertEqual(response.status,404)

class SpotifyClockRegressionTests(unittest.IsolatedAsyncioTestCase):
    async def test_repeated_position_anchor_does_not_cancel_slow_airplay_start(self):
        import time
        hardware=NullAudioBackend()
        started=asyncio.Event();finish=asyncio.Event()
        async def load_pcm(source,**kwargs):
            started.set();await finish.wait()
        hardware.load_pcm=load_pcm
        hardware.update_pcm_metadata=AsyncMock()
        output=SharedAudioOutput(hardware)
        player=SoloistInput(output.input('spotify'),config_path='/unused',state_dir='/unused',client_dir='/unused',bus_address='private')
        player.command=AsyncMock()
        clock=Mock();clock.time.return_value=1000;clock.monotonic.return_value=100
        clock_patch=patch('cast_audio_lab.spotify_connect.time',clock);clock_patch.start()
        anchor=1000000
        state={'status':'playing','is_active':True,'position':{'position_ms':1000,'timestamp_ms':anchor,'speed':1},'item':{'uri':'synthetic:track'}}
        try:
            await player._state(state);await started.wait()
            original=player._buffer
            # The SDK can return the same timestamped anchor after startup
            # takes several seconds. It is not a seek back to position_ms.
            clock.time.return_value=1003
            clock.monotonic.return_value=103
            await player._state(state)
            self.assertIs(player._buffer,original)
            self.assertFalse(original._closed)
            player.command.assert_not_awaited()
        finally:
            finish.set();await player.close();await output.close();clock_patch.stop()

    async def test_seek_during_airplay_start_does_not_pause_the_new_selection(self):
        hardware=NullAudioBackend();started=asyncio.Event();finish=asyncio.Event()
        async def load_pcm(source,**kwargs):
            hardware._status.state='BUFFERING';hardware._emit_status();started.set()
            try:await finish.wait()
            except asyncio.CancelledError:
                hardware._status.state='IDLE';hardware._status.idle_reason='ERROR';hardware._emit_status()
                raise
        hardware.load_pcm=load_pcm
        output=SharedAudioOutput(hardware)
        player=SoloistInput(output.input('spotify'),config_path='/unused',state_dir='/unused',client_dir='/unused',bus_address='private')
        player.command=AsyncMock()
        state={'status':'playing','is_active':True,'position':{'position_ms':1000},'item':{'uri':'synthetic:track'}}
        try:
            await player._state(state);await started.wait()
            original=player._buffer
            newer={**state,'position':{'position_ms':30000}}
            await asyncio.wait_for(player._state(newer),2)
            await asyncio.sleep(0);await asyncio.sleep(0)
            self.assertIsNot(player._buffer,original)
            self.assertIsNotNone(player._buffer)
            finish.set()
            await player._load_task
            self.assertTrue(original._closed)
            player.command.assert_not_awaited()
        finally:
            finish.set();await player.close();await output.close()

    def test_position_uses_anchor_timestamp_and_playback_speed(self):
        from cast_audio_lab.spotify_connect import playback_position
        with patch('cast_audio_lab.spotify_connect.time') as clock:
            clock.time.return_value=1003
            state={'status':'playing','position':{'position_ms':1000,'timestamp_ms':1000000,'speed':1}}
            self.assertEqual(playback_position(state),(4,1))
            state['position']['speed']=0
            self.assertEqual(playback_position(state),(1,0))
            state['position']['speed']=1.5
            self.assertEqual(playback_position(state),(5.5,1.5))
            for value in ('private-string',float('nan'),-1,True):
                state['position']['timestamp_ms']=value
                with self.assertRaises(ValueError):playback_position(state)
