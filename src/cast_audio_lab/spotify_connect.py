# SPDX-License-Identifier: MPL-2.0
"""Official Soloist Connect input and private per-route Linux audio capture."""
from __future__ import annotations
import asyncio
import contextlib
import json
import logging
import math
import os
from pathlib import Path
import tempfile
import time
import uuid
from urllib.parse import urlsplit

from .backend import MediaMetadata
from .pcm import PcmBuffer
from .routes import atomic_json, text
from .soloist_assets import ensure_soloist

LOG = logging.getLogger(__name__)
LOG.setLevel(logging.INFO)
_STATES = {'starting', 'waiting_for_pairing', 'ready', 'playing', 'paused', 'stopped', 'failed'}
_last_diagnostic = None


def media_metadata(item):
    def label(value):
        return ''.join(c if ord(c) >= 32 else ' ' for c in value)[:512] if isinstance(value, str) else ''
    decorations = item.get('decorations') or {}
    duration = (decorations.get('playback') or {}).get('duration_ms')
    duration = duration / 1000 if type(duration) in (int, float) and math.isfinite(duration) and duration > 0 else None
    parent = (decorations.get('parent') or {}).get('entity') or {}
    creators = decorations.get('creators') or []
    artists = [label((((creator.get('entity') or {}).get('decorations') or {}).get('identity') or {}).get('name'))
               for creator in creators[:8] if isinstance(creator, dict)]
    artwork = ''
    for cover in ((decorations.get('visual_identity') or {}).get('cover') or [])[:8]:
        value = cover.get('url') if isinstance(cover, dict) else None
        if isinstance(value, str) and len(value) < 4096:
            parsed = urlsplit(value)
            if (parsed.scheme == 'https' and not parsed.username and
                    (parsed.hostname == 'i.scdn.co' or (parsed.hostname or '').endswith('.spotifycdn.com'))):
                artwork = value
                break
    return MediaMetadata(title=label((decorations.get('identity') or {}).get('name')),
        artist=', '.join(filter(None, artists))[:512],
        album=label(((parent.get('decorations') or {}).get('identity') or {}).get('name')),
        artwork_url=artwork, item_id=label(item.get('uri')), duration=duration)


def playback_position(data):
    """Resolve Soloist's timestamped position anchor at the current wall clock."""
    position = data.get('position') or {}
    if not isinstance(position, dict):
        raise ValueError('invalid Spotify position')
    milliseconds = position.get('position_ms', 0)
    speed = position.get('speed', 1 if data.get('status') == 'playing' else 0)
    stamp = position.get('timestamp_ms')
    for value in (milliseconds, speed):
        if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
            raise ValueError('invalid Spotify position')
    if speed > 8:
        raise ValueError('invalid Spotify playback speed')
    if stamp is not None:
        if type(stamp) not in (int, float) or not math.isfinite(stamp) or stamp < 0:
            raise ValueError('invalid Spotify position timestamp')
        if stamp > 0:
            milliseconds += max(0, time.time() * 1000 - stamp) * speed
    return milliseconds / 1000, speed


def diagnostic(state, reason='none'):
    global _last_diagnostic
    if state not in _STATES or reason not in {'none', 'startup_failed', 'process_exited',
            'capture_failed', 'transport_failed', 'control_failed', 'connection_failed', 'infrastructure_unavailable'}:
        state, reason = 'failed', 'startup_failed'
    if _last_diagnostic != (state, reason):
        _last_diagnostic = (state, reason)
        LOG.info('SPOTIFY_DIAGNOSTIC state=%s reason=%s', state, reason)


class SpotifyStore:
    def __init__(self, directory):
        self.directory = Path(directory)

    def path(self, ident):
        return self.directory / f'spotify-{uuid.UUID(ident)}.json'

    def load(self, ident):
        path = self.path(ident)
        if path.is_symlink():
            raise ValueError('invalid Spotify configuration')
        try:
            if path.stat().st_size > 20000 or path.stat().st_mode & 0o077:
                raise ValueError('invalid Spotify configuration')
            value = json.loads(path.read_text())
        except FileNotFoundError:
            return {'enabled': False, 'name': '', 'api_key': ''}
        if not isinstance(value, dict) or set(value) != {'enabled', 'name', 'api_key'}:
            raise ValueError('invalid Spotify configuration')
        return self.validate(value)

    @staticmethod
    def validate(value):
        if type(value.get('enabled')) is not bool:
            raise ValueError('invalid Spotify enabled flag')
        key = value.get('api_key', '')
        if not isinstance(key, str) or len(key) > 16384 or any(ord(c) < 33 or ord(c) > 126 for c in key):
            raise ValueError('invalid Soloist key')
        name = value.get('name', '')
        if name:
            name = text(name, 'Spotify speaker name', 80)
        if value['enabled'] and (not key or not name):
            raise ValueError('Soloist key and speaker name are required')
        return {'enabled': value['enabled'], 'name': name, 'api_key': key}

    def updated(self, ident, body, default_name):
        if not isinstance(body, dict) or set(body) - {'enabled', 'name', 'api_key'}:
            raise ValueError('invalid Spotify configuration')
        value = self.load(ident)
        value.update(body)
        if not value['name']:
            value['name'] = default_name
        return self.validate(value)

    def save(self, ident, value):
        atomic_json(self.path(ident), self.validate(value))

    def public(self, ident):
        value = self.load(ident)
        return {'enabled': value['enabled'], 'name': value['name'], 'key_set': bool(value['api_key'])}


async def reap(process, timeout=4):
    if process is None:
        return
    if process.returncode is None:
        with contextlib.suppress(ProcessLookupError):
            process.terminate()
    try:
        await asyncio.wait_for(asyncio.shield(process.wait()), timeout)
    except TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            process.kill()
        await asyncio.wait_for(process.wait(), 2)


async def wait_path(path, process, timeout=8):
    async with asyncio.timeout(timeout):
        while not path.exists():
            if process.returncode is not None:
                raise RuntimeError('Spotify helper exited during startup')
            await asyncio.sleep(.05)


def child_environment(**extra):
    # Helpers have no reason to inherit Home Assistant's supervisor token.
    env = {key: os.environ[key] for key in ('PATH', 'LANG', 'LC_ALL') if key in os.environ}
    env.update(extra)
    return env


class SpotifyInfrastructure:
    """One app-private D-Bus/Avahi instance, shared by its Connect routes.

    Runs as the application user. The image prepares Avahi's runtime directory
    for its container-local group; no host D-Bus or extra host privilege is used.
    """
    def __init__(self):
        self.temporary = None
        self.bus = self.avahi = None
        self.address = None

    async def start(self):
        if self.avahi is not None and self.avahi.returncode is None:
            return
        await self.close()
        self.temporary = tempfile.TemporaryDirectory(prefix='cast-spotify-')
        directory = Path(self.temporary.name)
        self.address = 'unix:path=' + str(directory / 'bus')
        env = child_environment(DBUS_SYSTEM_BUS_ADDRESS=self.address, XDG_RUNTIME_DIR=str(directory))
        try:
            self.bus = await asyncio.create_subprocess_exec('dbus-daemon', '--session', '--nofork',
                '--nopidfile', '--address=' + self.address, stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL, env=env)
            await wait_path(directory / 'bus', self.bus)
            config = directory / 'avahi.conf'
            config.write_text('[server]\nhost-name=cast-audio-spotify\nuse-ipv4=yes\nuse-ipv6=yes\n'
                              '[publish]\npublish-workstation=no\npublish-hinfo=no\n'
                              '[reflector]\nenable-reflector=no\n')
            self.avahi = await asyncio.create_subprocess_exec('avahi-daemon', '--no-chroot',
                '--no-drop-root', '--no-rlimits', '--file=' + str(config),
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL, env=env)
            await asyncio.sleep(.3)
            if self.avahi.returncode is not None:
                raise RuntimeError('Spotify discovery helper failed')
        except BaseException:
            await self.close()
            raise

    async def close(self):
        for process in (self.avahi, self.bus):
            await reap(process)
        self.avahi = self.bus = None
        self.address = None
        if self.temporary:
            self.temporary.cleanup()
            self.temporary = None


class SoloistInput:
    def __init__(self, source, *, config_path, state_dir, client_dir, bus_address):
        self.source = source
        source.on_preempt = self.preempt
        source.add_control_listener(self._receiver_control)
        source.add_status_listener(self._output_status)
        self.config_path, self.state_dir = Path(config_path), Path(state_dir)
        self.client_dir, self.bus_address = Path(client_dir), bus_address
        self.task = None
        self._processes = []
        self._tasks = []
        self._temporary = None
        self._ws = None
        self._buffer = None
        self._load_task = None
        self._closed = False
        self._suppressed = False
        self._status = 'stopped'
        self._active = False
        self._item = None
        self._speed = 0.0
        self._position = 0.0
        self._position_at = time.monotonic()
        self._transition = asyncio.Lock()
        self._control_tasks = set()

    async def _spawn(self, *args, **kwargs):
        # Closed stdin prevents accidental credential prompts. No native logs
        # or command-line arguments are ever relayed to app/support logs.
        task = asyncio.create_task(asyncio.create_subprocess_exec(*args,
            stdin=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL, **kwargs))
        try:
            process = await asyncio.shield(task)
        except asyncio.CancelledError:
            # Subprocess creation can complete after cancellation. Retain and
            # reap that child before allowing a replacement route to start.
            process = await task
            await reap(process)
            raise
        self._processes.append(process)
        return process

    async def run(self):
        if not self.bus_address:
            diagnostic('failed', 'infrastructure_unavailable')
            return
        for attempt in range(3):
            if self._closed:
                return
            diagnostic('starting')
            try:
                await self._instance()
                if not self._closed:
                    raise RuntimeError('Spotify input ended')
            except asyncio.CancelledError:
                raise
            except Exception:
                diagnostic('failed', 'startup_failed' if not self._processes else 'process_exited')
            finally:
                await self._cleanup()
            if not self._closed and attempt < 2:
                await asyncio.sleep(2 ** attempt)

    async def _instance(self):
        from aiohttp import ClientSession, ClientTimeout
        if (self.config_path.is_symlink() or self.config_path.stat().st_size > 20000
                or self.config_path.stat().st_mode & 0o077):
            raise ValueError('invalid Spotify configuration')
        config = SpotifyStore.validate(json.loads(self.config_path.read_text()))
        if not config['enabled']:
            return
        executable = await ensure_soloist(self.client_dir)
        self.state_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
        self._temporary = tempfile.TemporaryDirectory(prefix='spotify-audio-')
        private = Path(self._temporary.name)
        socket = private / 'pulse'
        env = child_environment(DBUS_SYSTEM_BUS_ADDRESS=self.bus_address,
            PULSE_SERVER='unix:' + str(socket), PULSE_SINK='spotify',
            PULSE_RUNTIME_PATH=str(private), PULSE_STATE_PATH=str(private / 'pulse-state'),
            XDG_RUNTIME_DIR=str(private))
        # This private sink only feeds a monitor. Disabling rewind caps its
        # render window at 50 ms instead of PulseAudio's two-second default.
        pulse = await self._spawn('pulseaudio', '--daemonize=no', '--exit-idle-time=-1',
            '--disable-shm=yes', '--use-pid-file=no', '-n',
            '--load=module-native-protocol-unix socket=' + str(socket) + ' auth-anonymous=1',
            '--load=module-null-sink sink_name=spotify rate=44100 channels=2 format=s16le norewinds=true',
            stdout=asyncio.subprocess.DEVNULL, env=env)
        await wait_path(socket, pulse)
        recorder = await self._spawn('parec', '--server=unix:' + str(socket),
            '--device=spotify.monitor', '--raw', '--format=s16le', '--rate=44100', '--channels=2',
            '--latency-msec=40',
            stdout=asyncio.subprocess.PIPE, env=env)
        session = self.state_dir / 'session'
        session.mkdir(mode=0o700, exist_ok=True)
        # Stale API endpoints are removed before startup; never connect to an
        # old file's port after a crash or to a non-loopback local API.
        for name in ('ws.addr', 'ws.port'):
            (session / name).unlink(missing_ok=True)
        soloist = await self._spawn(str(executable), '-n', config['name'], '-k', config['api_key'],
            '-D', str(session), '-C', str(self.state_dir / 'cache'), '-z', '100',
            '-i', '100', '-w', '127.0.0.1:0', stdout=asyncio.subprocess.DEVNULL, env=env)
        del config
        await wait_path(session / 'ws.port', soloist, 20)
        if (session / 'ws.addr').read_text().strip() != '127.0.0.1':
            raise ValueError('Spotify control API must be loopback')
        port = int((session / 'ws.port').read_text().strip())
        if not 1 <= port <= 65535:
            raise ValueError('invalid Spotify control API port')
        async with ClientSession(timeout=ClientTimeout(total=None)) as client:
            async with client.ws_connect(f'http://127.0.0.1:{port}', max_msg_size=262144,
                                         heartbeat=10) as ws:
                self._ws = ws
                tasks = [asyncio.create_task(self._capture(recorder.stdout)),
                         asyncio.create_task(self._receive(ws)),
                         *(asyncio.create_task(p.wait()) for p in (pulse, recorder, soloist))]
                self._tasks = tasks
                done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    task.result()

    async def command(self, command, **fields):
        if self._ws is None or self._ws.closed:
            raise RuntimeError('Spotify control API unavailable')
        await asyncio.wait_for(self._ws.send_json({'type': 'command', 'command': command, **fields}), 3)

    async def _capture(self, reader):
        while data := await reader.read(8192):
            current = self._buffer
            if current is not None and not current.feed(data):
                diagnostic('failed', 'capture_failed')
                await self.command('pause')
                await self.source.stop()
                self._buffer = None
            elif current is not None and current.nonzero and self.source.owns_output and self.source.status().state == 'PLAYING':
                diagnostic('playing')
        raise RuntimeError('Spotify capture ended')

    async def _receive(self, ws):
        from aiohttp import WSMsgType
        async for frame in ws:
            if frame.type != WSMsgType.TEXT:
                continue
            data = json.loads(frame.data)
            if not isinstance(data, dict):
                raise ValueError('invalid Spotify state')
            kind = data.get('type')
            if kind == 'auth_state':
                if not data.get('logged_in'):
                    diagnostic('waiting_for_pairing')
                else:
                    diagnostic('ready')
                    await self.command('get_state')
            elif kind == 'playback_state':
                await self._state(data)
            elif kind in {'playback_changed', 'track_changed', 'volume_changed', 'position_sync'}:
                await self.command('get_state')
            elif kind == 'error':
                diagnostic('failed', 'control_failed')

    async def _state(self, data):
        status = data.get('status')
        if status == 'idle':
            status = 'stopped'
        if status not in {'playing', 'paused', 'stopped', 'buffering'}:
            return
        active = bool(data.get('is_active'))
        item = data.get('item') or {}
        position, speed = playback_position(data)
        metadata = media_metadata(item)
        uri = item.get('uri')
        now = time.monotonic()
        discontinuity = (self._item == uri and self._status == 'playing'
                         and abs(position - self._position - (now - self._position_at) * self._speed) > 2)
        newly_playing = status == 'playing' and (self._status != 'playing' or not self._active)
        self._status, self._active = status, active
        self._position, self._position_at, self._item = position, now, uri
        self._speed = speed
        if status in {'paused', 'stopped'} or not active:
            self._suppressed = False
            await self._stop_audio()
            diagnostic(status if status in {'paused', 'stopped'} else 'ready')
            return
        if status != 'playing' or self._suppressed:
            return
        if newly_playing or (self.source.owns_output and discontinuity):
            diagnostic('starting')
            await self._start_audio(metadata, position)
        elif self.source.owns_output:
            await self.source.update_pcm_metadata(metadata, position)

    def _output_status(self, status):
        current = self._buffer
        if current is None or not self.source.owns_output:
            return
        if status.state == 'PLAYING' and current.nonzero:
            diagnostic('playing')
        elif status.state == 'IDLE' and status.idle_reason == 'ERROR':
            async def failed():
                if self._buffer is not current or not self.source.owns_output:
                    return
                diagnostic('failed', 'transport_failed')
                self._buffer = None
                current.close()
                with contextlib.suppress(OSError, RuntimeError, TimeoutError):
                    await self.command('pause')
                await self.source.stop()
            task = asyncio.create_task(failed())
            self._control_tasks.add(task)
            task.add_done_callback(self._control_tasks.discard)

    async def _start_audio(self, metadata, position):
        async with self._transition:
            # Intentional replacement cancels a buffering AirPlay load, which
            # publishes IDLE/ERROR during cleanup. Detach its buffer first so
            # that status cannot pause the new Spotify selection.
            previous, self._buffer = self._buffer, None
            if previous:
                previous.close()
            await self._cancel_load()
            current = self._buffer = PcmBuffer()
            await self.source.set_metadata(metadata)
            async def load():
                try:
                    await self.source.load_pcm(current, start_time=position)
                except asyncio.CancelledError:
                    raise
                except (OSError, ValueError, RuntimeError, TimeoutError):
                    diagnostic('failed', 'transport_failed')
                    current.close()
                    if self._buffer is current:
                        self._buffer = None
                    with contextlib.suppress(OSError, RuntimeError, TimeoutError):
                        await self.command('pause')
                    await self.source.stop()
            self._load_task = asyncio.create_task(load())

    async def _cancel_load(self):
        task, self._load_task = self._load_task, None
        if task and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def _stop_audio(self):
        async with self._transition:
            if self._buffer:
                self._buffer.close()
                self._buffer = None
            await self._cancel_load()
            await self.source.stop()

    async def preempt(self):
        # Called while output ownership is changing; never re-enter its lock.
        self._suppressed = True
        if self._buffer:
            self._buffer.close()
            self._buffer = None
        try:
            await self.command('pause')
        except (OSError, RuntimeError, TimeoutError):
            # Capture has already been disconnected. A missing cloud control
            # channel must not prevent Cast from taking the physical output.
            diagnostic('failed', 'control_failed')

    def _receiver_control(self, request):
        async def send():
            command = {'play': 'play', 'pause': 'pause', 'stop': 'pause',
                       'next': 'skip_next', 'previous': 'skip_prev', 'seek': 'seek',
                       'volume': 'set_volume'}.get(request.command)
            fields = {}
            if command == 'seek' and request.position is not None:
                fields['position_ms'] = max(0, round(request.position * 1000))
            elif command == 'set_volume' and request.level is not None:
                fields['volume'] = max(0, min(100, round(request.level * 100)))
            if command:
                try:await self.command(command, **fields)
                except (OSError, RuntimeError, TimeoutError):diagnostic('failed', 'control_failed')
        task = asyncio.create_task(send())
        self._control_tasks.add(task)
        task.add_done_callback(self._control_tasks.discard)

    async def _cleanup(self):
        try:
            await self._stop_audio()
        finally:
            await self._cleanup_helpers()

    async def _cleanup_helpers(self):
        self._ws = None
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        for process in reversed(self._processes):
            await reap(process)
        self._processes.clear()
        if self._temporary:
            self._temporary.cleanup()
            self._temporary = None
        self._status, self._active, self._item = 'stopped', False, None

    async def close(self):
        self._closed = True
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        await self._cleanup()
        for task in list(self._control_tasks):task.cancel()
        await asyncio.gather(*self._control_tasks, return_exceptions=True)
        self.source.remove_control_listener(self._receiver_control)
        self.source.remove_status_listener(self._output_status)
