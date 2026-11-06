"""Bounded, single-process upstream work with cancellation-safe single flight."""

import asyncio
import json
from datetime import timedelta

from aiohttp import web

from .common import iso_utc, utc_now


def temporary_error(code="upstream_unavailable", seconds=60):
    return web.HTTPServiceUnavailable(
        text=json.dumps({"error": code, "temporary": True,
                         "retry_after_seconds": seconds,
                         "retry_at": iso_utc(utc_now() + timedelta(seconds=seconds))}),
        content_type="application/json", headers={"Retry-After": str(seconds)},
    )


class LookupPool:
    def __init__(self, active=4, queued=16, timeout=20):
        self.limit = active + queued
        self.timeout = timeout
        self.slots = asyncio.Semaphore(active)
        self.inflight = {}

    async def run(self, key, operation, on_timeout=None):
        task = self.inflight.get(key)
        if task is None:
            if len(self.inflight) >= self.limit:
                raise temporary_error("lookup_overloaded", 5)

            async def work():
                try:
                    # Includes queue time, resolution, backoff and the one retry.
                    async with asyncio.timeout(self.timeout):
                        async with self.slots:
                            return await operation()
                except TimeoutError:
                    if on_timeout:
                        # Record failure before removing the shared task, so
                        # a newer lookup cannot race the older timeout handler.
                        await on_timeout()
                    raise
                finally:
                    self.inflight.pop(key, None)

            task = asyncio.create_task(work())
            self.inflight[key] = task
            # Retrieve exceptions even if every HTTP caller disconnects.
            task.add_done_callback(lambda done: done.exception() if not done.cancelled() else None)
        return await asyncio.shield(task)

    async def close(self):
        tasks = list(self.inflight.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
