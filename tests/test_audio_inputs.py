# SPDX-License-Identifier: MPL-2.0
import asyncio
import unittest
from unittest.mock import AsyncMock
from cast_audio_lab.backend import NullAudioBackend
from cast_audio_lab.audio_inputs import SharedAudioOutput


class AudioInputTests(unittest.IsolatedAsyncioTestCase):
    async def test_takeover_retires_old_source_and_ignores_its_late_controls(self):
        hardware = NullAudioBackend()
        pause_spotify = AsyncMock()
        output = SharedAudioOutput(hardware)
        cast = output.input('cast')
        spotify = output.input('spotify', pause_spotify)
        await spotify.load('spotify-audio', 'audio/pcm', True, 0)
        await cast.load('cast-audio', 'audio/mpeg', True, 0)
        pause_spotify.assert_awaited_once()
        await spotify.stop()
        await spotify.pause()
        self.assertEqual(hardware.status().url, 'cast-audio')
        self.assertEqual(hardware.status().state, 'PLAYING')
        self.assertEqual(spotify.status().state, 'IDLE')
        self.assertEqual(spotify.status().idle_reason, 'CANCELLED')
        await output.close()

    async def test_takeover_cancels_buffering_before_new_source_starts(self):
        started, cancelled = asyncio.Event(), asyncio.Event()
        hardware = NullAudioBackend()
        original = hardware.load
        async def load(url, *args):
            if url == 'old':
                started.set()
                try:await asyncio.Event().wait()
                finally:cancelled.set()
            else:
                self.assertTrue(cancelled.is_set())
                await original(url, *args)
        hardware.load = load
        output = SharedAudioOutput(hardware)
        old, new = output.input('old'), output.input('new')
        pending = asyncio.create_task(old.load('old', 'audio/pcm', True, 0))
        await started.wait()
        await new.load('new', 'audio/mpeg', True, 0)
        with self.assertRaises(asyncio.CancelledError):await pending
        self.assertEqual(hardware.status().url, 'new')
        await output.close()

    async def test_sources_cannot_report_other_sources_audio_and_speakers_are_independent(self):
        one, two = SharedAudioOutput(NullAudioBackend()), SharedAudioOutput(NullAudioBackend())
        cast_one, spotify_one = one.input('cast'), one.input('spotify')
        spotify_two = two.input('spotify')
        observed=[];cast_one.add_status_listener(lambda value: observed.append(value.url))
        await spotify_one.load('one', 'audio/pcm', True, 0)
        await spotify_two.load('two', 'audio/pcm', True, 0)
        self.assertEqual(observed, [])
        self.assertIsNone(cast_one.status().url)
        await cast_one.load('cast', 'audio/mpeg', True, 0)
        self.assertEqual(spotify_two.status().url, 'two')
        await one.close();await two.close()
