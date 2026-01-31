import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

logger = logging.getLogger("cartesia-forwarder")

ForwardCallback = Callable[[str], Awaitable[None]]

class CartesiaForwarder:
    def __init__(self, forward_callback: ForwardCallback) -> None:
        """
        Initialize the forwarder with a callback function.
        
        Args:
            forward_callback: An async function that will be called with the data to forward
        """
        self.queue: asyncio.Queue[str | dict[str, Any]] = asyncio.Queue()
        self._running: bool = False
        self._forward_callback: ForwardCallback = forward_callback
        self._last_word_end_sec: float | None = None  # Track last word's end time across messages
        self._task: asyncio.Task[None] | None = None

    def add_data(self, data: str | dict[str, Any]) -> None:
        """This is a synchronous method to add data."""
        # Put data into the queue from a sync context
        asyncio.get_event_loop().call_soon_threadsafe(self.queue.put_nowait, data)

    async def _forward_loop(self) -> None:
        while self._running:
            data = await self.queue.get()
            try:
                # Parse the JSON data
                parsed_data: dict[str, Any] = json.loads(data) if isinstance(data, str) else data
                
                # Check if this is a timestamps event
                if "word_timestamps" in parsed_data:
                    # Transform to ElevenLabs character-level format
                    transformed_data = self._transform_timestamps(parsed_data)
                    await self._forward_callback(json.dumps(transformed_data))
                # Check if this is a done event
                elif parsed_data.get("done"):
                    # Transform to ElevenLabs isFinal format
                    final_data = {
                        "isFinal": True,
                        "alignment": None,
                        "normalizedAlignment": None
                    }
                    await self._forward_callback(json.dumps(final_data))
                    # Reset state for next synthesis
                    self._last_word_end_sec = None
                # Skip all other events
                
            except Exception as e:
                logger.error(f"Error forwarding data: {e}")
            self.queue.task_done()
    
    def _transform_timestamps(self, cartesia_data: dict[str, Any]) -> dict[str, Any]:
        """Transform Cartesia word-level timestamps to ElevenLabs character-level format.
        
        Cartesia returns cumulative timestamps across the entire response,
        while ElevenLabs returns timestamps relative to each message (starting from 0ms).
        This method makes each message's timestamps start from 0ms and handles
        spaces between words that are split across multiple messages.
        """
        word_timestamps = cartesia_data.get("word_timestamps", {})
        words = word_timestamps.get("words", [])
        starts = word_timestamps.get("start", [])
        ends = word_timestamps.get("end", [])
        
        if not starts:
            # No timestamps in this message
            return {
                "isFinal": False,
                "alignment": {
                    "charStartTimesMs": [],
                    "charDurationsMs": [],
                    "chars": []
                },
                "normalizedAlignment": {
                    "charStartTimesMs": [],
                    "charDurationsMs": [],
                    "chars": []
                }
            }
        
        # Find the minimum start time in this message to use as offset
        # This makes the timestamps start from 0ms (ElevenLabs style)
        min_start_sec = min(starts)
        offset_ms = int(min_start_sec * 1000)
        
        chars = []
        char_start_times_ms = []
        char_durations_ms = []
        
        # Handle space between previous message and current message
        if self._last_word_end_sec is not None:
            # There was a previous message, add a space for the gap
            gap_duration_ms = int(min_start_sec * 1000) - int(self._last_word_end_sec * 1000)
            if gap_duration_ms > 0:
                chars.append(" ")
                char_start_times_ms.append(0)
                char_durations_ms.append(gap_duration_ms)
        
        for idx, (word, start_sec, end_sec) in enumerate(zip(words, starts, ends)):
            # Convert seconds to milliseconds
            start_ms = int(start_sec * 1000)
            end_ms = int(end_sec * 1000)
            
            # Make relative to the start of this message (subtract offset)
            relative_start_ms = start_ms - offset_ms
            relative_end_ms = end_ms - offset_ms
            word_duration_ms = relative_end_ms - relative_start_ms
            
            # Distribute timing across characters in the word
            num_chars = len(word)
            if num_chars > 0:
                char_duration = word_duration_ms / num_chars
                
                for i, char in enumerate(word):
                    chars.append(char)
                    char_start = relative_start_ms + int(i * char_duration)
                    char_start_times_ms.append(char_start)
                    char_durations_ms.append(int(char_duration))
                
                # Add space after word (except for last word)
                if idx < len(words) - 1:
                    # Calculate space duration as the gap between this word's end and next word's start
                    next_start_ms = int(starts[idx + 1] * 1000) - offset_ms
                    space_duration_ms = next_start_ms - relative_end_ms
                    
                    chars.append(" ")
                    char_start_times_ms.append(relative_end_ms)
                    char_durations_ms.append(space_duration_ms)
        
        # Update last word end time for next message
        self._last_word_end_sec = ends[-1]
        
        # Create ElevenLabs-compatible format
        # Both alignment and normalizedAlignment use the same data for Cartesia
        alignment = {
            "charStartTimesMs": char_start_times_ms,
            "charDurationsMs": char_durations_ms,
            "chars": chars
        }
        
        return {
            "isFinal": False,
            "alignment": alignment,
            "normalizedAlignment": alignment
        }

    async def start(self) -> None:
        """Start the async forwarding loop."""
        self._running = True
        self._last_word_end_sec = None  # Reset state
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
