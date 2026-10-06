"""Wakes up requests waiting for a decision made by another request.

Redis pub/sub is only a latency optimisation: waiters always re-read the
database, and fall back to polling if Redis is unavailable.
"""

from __future__ import annotations

import asyncio
import contextlib

import structlog
from redis.asyncio import Redis

log = structlog.get_logger(__name__)

POLL_INTERVAL_S = 0.05


def channel(auth_id: str) -> str:
    return f"auth-decided:{auth_id}"


class Notifier:
    def __init__(self, redis: Redis | None) -> None:
        self.redis = redis

    async def publish(self, auth_id: str) -> None:
        if self.redis is None:
            return
        try:
            await self.redis.publish(channel(auth_id), "1")
        except Exception as e:  # never let notification failures affect decisions
            log.warning("notify.publish_failed", auth_id=auth_id, error=str(e))

    @contextlib.asynccontextmanager
    async def subscription(self, auth_id: str):  # type: ignore[no-untyped-def]
        """Yields an awaitable `wait()` that returns on a publish or after a poll interval."""
        pubsub = None
        if self.redis is not None:
            try:
                pubsub = self.redis.pubsub()
                await pubsub.subscribe(channel(auth_id))
            except Exception as e:
                log.warning("notify.subscribe_failed", auth_id=auth_id, error=str(e))
                pubsub = None

        async def wait() -> None:
            if pubsub is None:
                await asyncio.sleep(POLL_INTERVAL_S)
                return
            try:
                await pubsub.get_message(ignore_subscribe_messages=True, timeout=POLL_INTERVAL_S)
            except Exception:
                await asyncio.sleep(POLL_INTERVAL_S)

        try:
            yield wait
        finally:
            if pubsub is not None:
                with contextlib.suppress(Exception):
                    await pubsub.unsubscribe()
                    await pubsub.aclose()
