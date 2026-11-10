# SPDX-License-Identifier: MPL-2.0
"""Exclusive per-speaker ownership of one physical audio backend."""
import asyncio
from .backend import NullAudioBackend, PlaybackStatus


class SharedAudioOutput:
    def __init__(self, backend):
        self.backend = backend
        self.owner = None
        self.generation = 0
        self._load = None
        self._lock = asyncio.Lock()
        backend.add_status_listener(self._status)
        backend.add_control_listener(self._control)

    def input(self, name, on_preempt=None):
        return AudioInput(self, name, on_preempt)

    def _status(self, status):
        if self.owner is not None:
            self.owner._emit_status()

    def _control(self, request):
        if self.owner is not None:
            self.owner._emit_control(request)

    async def _claim_locked(self, source):
        if self.owner is source:
            return self.generation
        previous, self.owner = self.owner, None
        self.generation += 1
        if previous is not None:
            previous._retire()
        task, self._load = self._load, None
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await self.backend.stop()
        if previous is not None and previous.on_preempt is not None:
            await asyncio.wait_for(previous.on_preempt(), 5)
        self.owner = source
        return self.generation

    async def load(self, source, operation):
        async with self._lock:
            generation = await self._claim_locked(source)
            task = asyncio.create_task(operation())
            self._load = task
        try:
            await task
            if self.owner is not source or self.generation != generation:
                raise asyncio.CancelledError()
        finally:
            async with self._lock:
                if self._load is task:
                    self._load = None

    async def release(self, source):
        async with self._lock:
            if self.owner is not source:
                return
            self.owner = None
            self.generation += 1
            source._retire()
            task, self._load = self._load, None
            if task is not None and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            await self.backend.stop()

    async def close(self):
        if self.owner is not None:
            await self.release(self.owner)
        await self.backend.shutdown()


class AudioInput(NullAudioBackend):
    def __init__(self, output, name, on_preempt):
        super().__init__()
        self.output, self.name, self.on_preempt = output, name, on_preempt
        self.local_audio_cache = getattr(output.backend, 'local_audio_cache', False)

    @property
    def owns_output(self):
        return self.output.owner is self

    def status(self):
        return self.output.backend.status() if self.owns_output else super().status()

    def _retire(self):
        hardware = self.output.backend.status()
        self._status = PlaybackStatus(volume=hardware.volume, muted=hardware.muted,
                                      idle_reason='CANCELLED')
        self._set_position(0)
        self._emit_status()

    async def load(self, url, content_type, autoplay, start_time):
        metadata = self.metadata
        async def operation():
            await self.output.backend.set_metadata(metadata)
            await self.output.backend.load(url, content_type, autoplay, start_time)
        await self.output.load(self, operation)

    async def load_pcm(self, source, *, autoplay=True, start_time=0):
        metadata = self.metadata
        async def operation():
            await self.output.backend.set_metadata(metadata)
            await self.output.backend.load_pcm(source, autoplay=autoplay, start_time=start_time)
        await self.output.load(self, operation)

    async def update_pcm_metadata(self, metadata, position):
        self.metadata = metadata
        if self.owns_output:
            await self.output.backend.update_pcm_metadata(metadata, position)

    async def play(self):
        if self.owns_output:
            await self.output.backend.play()

    async def pause(self):
        if self.owns_output:
            await self.output.backend.pause()

    async def park_pcm(self):
        async with self.output._lock:
            park = getattr(self.output.backend, "park_pcm", None)
            return self.owns_output and park is not None and await park()

    async def seek(self, position):
        if self.owns_output:
            await self.output.backend.seek(position)

    async def set_volume(self, level, muted):
        # Receiver volume remains output-wide, including pre-LOAD restoration.
        await self.output.backend.set_volume(level, muted)
        self._status.volume, self._status.muted = level, muted
        if not self.owns_output:
            self._emit_status()

    async def stop(self):
        await self.output.release(self)

    async def shutdown(self):
        await self.stop()
