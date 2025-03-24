import asyncio

class ElevenlabsForwarder:
    def __init__(self, forward_callback):
        """
        Initialize the forwarder with a callback function.
        
        Args:
            forward_callback: An async function that will be called with the data to forward
        """
        self.queue = asyncio.Queue()
        self._running = False
        self._forward_callback = forward_callback

    def add_data(self, data):
        """This is a synchronous method to add data."""
        # Put data into the queue from a sync context
        asyncio.get_event_loop().call_soon_threadsafe(self.queue.put_nowait, data)

    async def _forward_loop(self):
        while self._running:
            data = await self.queue.get()
            try:
                await self._forward_callback(data)
            except Exception as e:
                print(f"Error forwarding data: {e}")
            self.queue.task_done()

    async def start(self):
        """Start the async forwarding loop."""
        self._running = True
        self._task = asyncio.create_task(self._forward_loop())

    async def stop(self):
        """Stop the loop gracefully."""
        self._running = False
        await self.queue.join()
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
