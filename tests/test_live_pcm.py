# SPDX-License-Identifier: MPL-2.0
import asyncio
import unittest
from unittest.mock import AsyncMock, Mock, patch
from cast_audio_lab.pcm import PcmBuffer
from cast_audio_lab.airplay import AirPlayAudioBackend, AirPlayTarget


class LivePcmTests(unittest.IsolatedAsyncioTestCase):
    async def test_failed_live_sender_cancels_parallel_feedback_startup(self):
        backend = AirPlayAudioBackend(AirPlayTarget('127.0.0.1'))
        entered, cancelled = asyncio.Event(), asyncio.Event()

        async def feedback():
            entered.set()
            try:
                await asyncio.Future()
            finally:
                cancelled.set()

        async def sender():
            await entered.wait()
            raise OSError('sender failed')

        with patch.object(backend, '_start_feedback', side_effect=feedback), \
             patch.object(backend, '_open_sender', side_effect=sender):
            with self.assertRaisesRegex(OSError, 'sender failed'):
                await asyncio.wait_for(backend._start_transport(parallel_feedback=True), 1)
        self.assertTrue(cancelled.is_set())

    async def test_backpressure_is_bounded_and_overflow_wakes_reader_with_error(self):
        source = PcmBuffer(8)
        self.assertTrue(source.feed(b'123456'))
        self.assertEqual(await source.read(2), b'12')
        self.assertTrue(source.feed(b'7890'))
        self.assertFalse(source.feed(b'!'))
        with self.assertRaisesRegex(OSError, 'PCM_BUFFER_OVERFLOW'):
            await source.read(8)
        self.assertFalse(source.feed(b'stale'))

    async def test_close_discards_old_samples_and_wakes_waiter(self):
        source = PcmBuffer(8)
        waiting = asyncio.create_task(source.read(4))
        await asyncio.sleep(0)
        source.close()
        self.assertEqual(await waiting, b'')
        newer = PcmBuffer(8)
        newer.feed(b'new')
        source.feed(b'old')
        self.assertEqual(await newer.read(4), b'new')

    async def test_direct_pcm_uses_transport_without_starting_url_decoder(self):
        backend = AirPlayAudioBackend(AirPlayTarget('127.0.0.1'))
        source = PcmBuffer()
        source.feed(b'\x01\x00\x02\x00' * 32)
        writer = Mock(drain=AsyncMock())
        backend._cli = Mock(stdin=writer, returncode=None)
        with patch.object(backend, '_prepare_transport', new_callable=AsyncMock), \
             patch.object(backend, '_wait_for_audio', new_callable=AsyncMock), \
             patch.object(backend, '_command', new_callable=AsyncMock), \
             patch.object(backend, '_start_pipeline', new_callable=AsyncMock) as decoder:
            try:
                await backend.load_pcm(source)
                await asyncio.sleep(0)
                self.assertEqual(b''.join(c.args[0] for c in writer.write.call_args_list), b'\x01\x00\x02\x00' * 32)
                decoder.assert_not_awaited()
                with self.assertRaisesRegex(ValueError, 'source controller'):
                    await backend.seek(5)
                source.close()
                await asyncio.wait_for(backend._pump_task, 1)
                self.assertEqual(backend.status().idle_reason, 'ERROR')
            finally:
                await backend._stop_track()
                backend._cli = None
