# SPDX-License-Identifier: MPL-2.0
"""Bounded live PCM input for the existing persistent output transport."""
import asyncio
from collections import deque


class PcmBuffer:
    """Single-event-loop reader; overflow fails instead of growing latency."""
    def __init__(self, maximum_bytes=44_100 * 2 * 2 * 15):
        if maximum_bytes <= 0:
            raise ValueError('invalid PCM buffer limit')
        self.maximum_bytes = maximum_bytes
        self._chunks = deque()
        self._size = 0
        self._ready = asyncio.Event()
        self._closed = False
        self._error = None
        self.received_bytes = 0
        self.nonzero = False

    def feed(self, data):
        if self._closed:
            return False
        if not data:
            return True
        if self._size + len(data) > self.maximum_bytes:
            self.close('PCM_BUFFER_OVERFLOW')
            return False
        self._chunks.append(bytes(data))
        self._size += len(data)
        self.received_bytes += len(data)
        self.nonzero |= any(data)
        self._ready.set()
        return True

    def close(self, error=None):
        self._closed = True
        self._error = error
        self._chunks.clear()
        self._size = 0
        self._ready.set()

    async def read(self, maximum):
        if maximum <= 0:
            raise ValueError('PCM reads require a positive bound')
        while not self._chunks:
            if self._error:
                raise OSError(self._error)
            if self._closed:
                return b''
            self._ready.clear()
            await self._ready.wait()
        data = self._chunks.popleft()
        result, rest = data[:maximum], data[maximum:]
        if rest:
            self._chunks.appendleft(rest)
        self._size -= len(result)
        return result
