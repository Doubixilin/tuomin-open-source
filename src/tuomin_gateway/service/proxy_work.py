"""Bounded CPU work off the event loop, with disconnect-aware cancellation."""
from __future__ import annotations

import asyncio
import threading
import time

from tuomin_gateway import cancellation


class ProxyBusy(Exception):
    pass


async def connected(request, event):
    if event.is_set() or await request.is_disconnected():
        event.set()
        raise cancellation.Cancelled()


async def watch_disconnect(request, event, stop):
    while not stop.is_set():
        await connected(request, event)
        try:
            await asyncio.wait_for(stop.wait(), 0.05)
        except asyncio.TimeoutError:
            pass


def _consume(task):
    if not task.cancelled():
        task.exception()


async def await_connected(request, event, task):
    stop = asyncio.Event()
    watcher = asyncio.create_task(watch_disconnect(request, event, stop))
    try:
        done, _ = await asyncio.wait((task, watcher), return_when=asyncio.FIRST_COMPLETED)
        if watcher in done:
            await watcher
        return await asyncio.shield(task)
    except BaseException:
        event.set()
        raise
    finally:
        # Starlette's nonblocking disconnect probe uses a cancelled AnyIO
        # scope; task.cancel() racing that scope can be swallowed. Stop the
        # watcher cooperatively instead of waiting on a swallowed cancellation.
        stop.set()
        await asyncio.gather(watcher, return_exceptions=True)


class ProxyWorkQueue:
    """One CPU job at a time; queued requests do not occupy worker threads."""
    def __init__(self, capacity=8):
        self.capacity = capacity
        self._gate = threading.Lock()
        self._state = threading.Lock()
        self._pending = 0

    async def run(self, request, event, function, metrics):
        started = time.perf_counter()
        with self._state:
            if self._pending >= self.capacity:
                raise ProxyBusy()
            self._pending += 1
        acquired = submitted = False
        try:
            while not acquired:
                await connected(request, event)
                acquired = self._gate.acquire(False)
                if not acquired:
                    await asyncio.sleep(0.02)
            metrics["queue_ms"] = round((time.perf_counter() - started) * 1000, 3)

            def work():
                try:
                    with cancellation.use_event(event):
                        cancellation.check()
                        return function()
                finally:
                    self._gate.release()
                    with self._state:
                        self._pending -= 1

            task = asyncio.create_task(asyncio.to_thread(work))
            task.add_done_callback(_consume)
            submitted = True
            # A cancelled await does NOT release the gate while CPU work lives.
            return await await_connected(request, event, task)
        finally:
            if not submitted:
                if acquired:
                    self._gate.release()
                with self._state:
                    self._pending -= 1


async def forward_connected(request, event, coroutine):
    try:
        await connected(request, event)
    except BaseException:
        coroutine.close()
        raise
    task = asyncio.create_task(coroutine)
    try:
        return await await_connected(request, event, task)
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
