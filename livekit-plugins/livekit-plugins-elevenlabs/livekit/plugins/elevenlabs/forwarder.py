import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

logger = logging.getLogger("elevenlabs-forwarder")

ForwardCallback = Callable[[str | dict[str, Any]], Awaitable[None]]

class ElevenlabsForwarder:
    def __init__(self, forward_callback: ForwardCallback) -> None:
        """
        Initialize the forwarder with a callback function.
        
        Args:
            forward_callback: An async function that will be called with the data to forward
        """
        self.queue: asyncio.Queue[str | dict[str, Any]] = asyncio.Queue()
        self._running: bool = False
        self._forward_callback: ForwardCallback = forward_callback
        self._task: asyncio.Task[None] | None = None

    def add_data(self, data: str | dict[str, Any]) -> None:
        """This is a synchronous method to add data."""
        # Put data into the queue from a sync context
        asyncio.get_event_loop().call_soon_threadsafe(self.queue.put_nowait, data)

    async def _forward_loop(self) -> None:
        while self._running:
            data = await self.queue.get()
            try:
                await self._forward_callback(data)
            except Exception as e:
                logger.error(f"Error forwarding data: {e}")
            self.queue.task_done()

    async def start(self) -> None:
        """Start the async forwarding loop."""
        self._running = True
        self._task = asyncio.create_task(self._forward_loop())

    async def stop(self) -> None:
        """Stop the loop gracefully."""
        if self._task is None:
            return
        self._running = False
        await self.queue.join()
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
