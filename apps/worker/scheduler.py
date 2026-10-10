"""Serialize local task execution and renew its lease until it yields or finishes."""

import asyncio
import math
from collections.abc import Awaitable, Callable
from typing import TypeVar

from tool_agent_lab.runtime.leases import ExecutionLease, LeaseService

T = TypeVar("T")


class WorkerScheduler:
    def __init__(self, leases: LeaseService, *, heartbeat_interval: float = 10.0) -> None:
        if not math.isfinite(heartbeat_interval) or not 0 < heartbeat_interval < leases.lease_seconds:
            raise ValueError("heartbeat_interval must be finite, positive and shorter than the lease")
        self.leases = leases
        self.heartbeat_interval = heartbeat_interval
        self.slot = asyncio.Lock()

    async def _heartbeat(self, lease: ExecutionLease) -> None:
        while True:
            await asyncio.sleep(self.heartbeat_interval)
            lease = self.leases.heartbeat(lease)

    async def execute(self, lease: ExecutionLease, operation: Callable[[], Awaitable[T]]) -> T:
        """Propagate renewal failure, stop the invocation and release only this epoch."""
        renewal = asyncio.create_task(self._heartbeat(lease))
        execution = asyncio.create_task(operation())
        try:
            done, _ = await asyncio.wait((execution, renewal), return_when=asyncio.FIRST_COMPLETED)
            if renewal in done:
                renewal.result()
            return await execution
        finally:
            execution.cancel()
            renewal.cancel()
            await asyncio.gather(execution, renewal, return_exceptions=True)
            self.leases.release(lease)
