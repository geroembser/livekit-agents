"""FallbackAdapter request failures: an error the caller declares request-bound (a provider
content filter refusing one reply) must not cost the LLM its availability, must not trigger a
recovery probe, and must keep that LLM out of retries of the same request only."""

from __future__ import annotations

import asyncio

import pytest

from livekit.agents import APIConnectionError, APIStatusError
from livekit.agents.llm import LLM, ChatChunk, ChatContext, ChoiceDelta, FallbackAdapter, LLMStream
from livekit.agents.types import DEFAULT_API_CONNECT_OPTIONS, APIConnectOptions

pytestmark = [pytest.mark.unit]


class _ScriptedStream(LLMStream):
    def __init__(self, llm: ScriptedLLM, *, chat_ctx: ChatContext, tools, conn_options) -> None:
        super().__init__(llm, chat_ctx=chat_ctx, tools=tools, conn_options=conn_options)
        self._scripted = llm

    async def _run(self) -> None:
        llm = self._scripted
        llm.calls += 1
        for token in llm.tokens:
            self._event_ch.send_nowait(
                ChatChunk(id="scripted", delta=ChoiceDelta(role="assistant", content=token))
            )
        if llm.error is not None:
            raise llm.error


class ScriptedLLM(LLM):
    """Emits ``tokens`` and then raises ``error`` (if any); counts calls."""

    def __init__(self, *, tokens: tuple[str, ...] = (), error: Exception | None = None) -> None:
        super().__init__()
        self.tokens = tokens
        self.error = error
        self.calls = 0

    def chat(
        self,
        *,
        chat_ctx: ChatContext,
        tools=None,
        conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS,
        **kwargs,
    ) -> LLMStream:
        return _ScriptedStream(
            self, chat_ctx=chat_ctx, tools=tools or [], conn_options=conn_options
        )


def _filtered() -> APIStatusError:
    return APIStatusError("generation blocked: PROHIBITED_CONTENT", retryable=False)


def _is_filtered(error: BaseException) -> bool:
    return "PROHIBITED_CONTENT" in str(error)


def _ctx(text: str) -> ChatContext:
    ctx = ChatContext()
    ctx.add_message(role="user", content=text)
    return ctx


def _last_user_id(chat_ctx: ChatContext) -> str | None:
    for item in reversed(chat_ctx.items):
        if item.type == "message" and item.role == "user":
            return item.id
    return None


async def _collect(stream: LLMStream) -> str:
    text = ""
    async with stream:
        async for chunk in stream:
            if chunk.delta and chunk.delta.content:
                text += chunk.delta.content
    return text


async def _drain_recovery_tasks(adapter: FallbackAdapter) -> None:
    tasks = [s.recovering_task for s in adapter._status if s.recovering_task is not None]
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


async def test_request_failure_keeps_llm_available_and_skips_it_for_the_request() -> None:
    primary = ScriptedLLM(error=_filtered())
    fallback = ScriptedLLM(tokens=("rescued",))
    adapter = FallbackAdapter(
        [primary, fallback], request_failure=_is_filtered, request_key=_last_user_id
    )
    availability_events = []
    adapter.on("llm_availability_changed", availability_events.append)
    try:
        ctx = _ctx("I am fifteen years old")
        assert await _collect(adapter.chat(chat_ctx=ctx)) == "rescued"
        await _drain_recovery_tasks(adapter)

        assert adapter._status[0].available is True
        assert availability_events == []
        # no recovery probe replayed the refused request
        assert primary.calls == 1

        # a retry of the same request (same last user message) skips the primary
        retry_ctx = ctx.copy()
        retry_ctx.add_message(role="system", content="continue")
        assert await _collect(adapter.chat(chat_ctx=retry_ctx)) == "rescued"
        assert primary.calls == 1

        # the next request goes to the primary again
        primary.error = None
        primary.tokens = ("primary answers",)
        assert await _collect(adapter.chat(chat_ctx=_ctx("next turn"))) == "primary answers"
        assert primary.calls == 2
    finally:
        await adapter.aclose()


async def test_request_failure_after_chunks_still_raises_but_keeps_availability() -> None:
    primary = ScriptedLLM(tokens=("partial ",), error=_filtered())
    fallback = ScriptedLLM(tokens=("rescued",))
    adapter = FallbackAdapter([primary, fallback], request_failure=_is_filtered)
    try:
        ctx = _ctx("hello")
        with pytest.raises(APIStatusError):
            await _collect(adapter.chat(chat_ctx=ctx))
        await _drain_recovery_tasks(adapter)
        assert adapter._status[0].available is True
        assert primary.calls == 1

        # the caller's own retry with the same context object lands on the fallback
        assert await _collect(adapter.chat(chat_ctx=ctx)) == "rescued"
        assert primary.calls == 1
    finally:
        await adapter.aclose()


async def test_other_failures_still_mark_the_llm_unavailable() -> None:
    primary = ScriptedLLM(error=APIStatusError("quota", status_code=429))
    fallback = ScriptedLLM(tokens=("rescued",))
    adapter = FallbackAdapter([primary, fallback], request_failure=_is_filtered)
    try:
        assert await _collect(adapter.chat(chat_ctx=_ctx("hello"))) == "rescued"
        assert adapter._status[0].available is False
    finally:
        await _drain_recovery_tasks(adapter)
        await adapter.aclose()


async def test_all_llms_refusing_the_request_raise_without_touching_availability() -> None:
    llms = [ScriptedLLM(error=_filtered()), ScriptedLLM(error=_filtered())]
    adapter = FallbackAdapter(llms, request_failure=_is_filtered)
    try:
        ctx = _ctx("hello")
        with pytest.raises(APIConnectionError, match="all LLMs failed") as excinfo:
            await _collect(adapter.chat(chat_ctx=ctx))
        assert all(status.available for status in adapter._status)
        # nothing left to try for this request: the caller's stream must not retry
        assert excinfo.value.retryable is False

        # a further call for the same request skips every LLM and says so too
        with pytest.raises(APIConnectionError) as again:
            await _collect(adapter.chat(chat_ctx=ctx))
        assert again.value.retryable is False
        assert all(llm.calls == 1 for llm in llms)

        # a new request is tried again
        with pytest.raises(APIConnectionError):
            await _collect(adapter.chat(chat_ctx=_ctx("next")))
        assert all(llm.calls == 2 for llm in llms)
    finally:
        await adapter.aclose()


async def test_summary_stays_retryable_when_any_llm_failed_for_another_reason() -> None:
    llms = [
        ScriptedLLM(error=_filtered()),
        ScriptedLLM(error=APIStatusError("quota", status_code=429)),
    ]
    adapter = FallbackAdapter(llms, request_failure=_is_filtered)
    try:
        with pytest.raises(APIConnectionError) as excinfo:
            await _collect(adapter.chat(chat_ctx=_ctx("hello")))
        assert excinfo.value.retryable is True
        assert adapter._status[0].available is True
        assert adapter._status[1].available is False
    finally:
        await _drain_recovery_tasks(adapter)
        await adapter.aclose()


async def test_request_refusals_are_reported_as_recoverable_errors() -> None:
    primary = ScriptedLLM(tokens=("partial ",), error=_filtered())
    adapter = FallbackAdapter(
        [primary, ScriptedLLM(error=_filtered())], request_failure=_is_filtered
    )
    errors = []
    adapter.on("error", errors.append)
    try:
        # refused after chunks: the refusal itself is raised
        with pytest.raises(APIStatusError):
            await _collect(adapter.chat(chat_ctx=_ctx("hello")))
        # refused everywhere before any chunk: the summary is raised
        primary.tokens = ()
        with pytest.raises(APIConnectionError, match="all LLMs failed"):
            await _collect(adapter.chat(chat_ctx=_ctx("again")))
        assert [error.recoverable for error in errors] == [True, True]
    finally:
        await adapter.aclose()


async def test_a_sweep_with_a_real_failure_is_still_reported_as_unrecoverable() -> None:
    llms = [
        ScriptedLLM(error=_filtered()),
        ScriptedLLM(error=APIStatusError("quota", status_code=429, retryable=False)),
    ]
    adapter = FallbackAdapter([*llms], request_failure=_is_filtered)
    errors = []
    adapter.on("error", errors.append)
    try:
        with pytest.raises(APIConnectionError):
            await _collect(adapter.chat(chat_ctx=_ctx("hello")))
        assert [error.recoverable for error in errors] == [False]
    finally:
        await _drain_recovery_tasks(adapter)
        await adapter.aclose()
