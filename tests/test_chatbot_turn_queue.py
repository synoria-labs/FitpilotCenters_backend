import asyncio

import pytest

from app.services.chatbot import turn_queue


async def test_image_and_followup_text_coalesce_before_model_execution(monkeypatch):
    monkeypatch.setattr(turn_queue, "DEBOUNCE_SECONDS", 0)
    preparing = asyncio.Event()
    calls = []

    async def image():
        preparing.set()
        await asyncio.Event().wait()  # waiting for the attachment download, before any tools
        calls.append("obsolete image turn")

    async def text():
        turn_queue.mark_started("customer", 101)
        calls.append("image plus followup text")

    first = turn_queue.schedule_turn("customer", 101, image)
    await preparing.wait()
    second = turn_queue.schedule_turn("customer", 101, text)
    await asyncio.gather(first, second, return_exceptions=True)
    await asyncio.sleep(0)
    assert first.cancelled() and calls == ["image plus followup text"]
    assert ("customer", 101) not in turn_queue._states


async def test_running_tools_are_not_canceled_and_next_turn_waits(monkeypatch):
    monkeypatch.setattr(turn_queue, "DEBOUNCE_SECONDS", 0)
    running = asyncio.Event()
    release = asyncio.Event()
    calls = []

    async def first():
        turn_queue.mark_started("owner", 102)
        calls.append("first start")
        running.set()
        await release.wait()
        calls.append("first end")

    async def second():
        turn_queue.mark_started("owner", 102)
        calls.append("second start")

    one = turn_queue.schedule_turn("owner", 102, first)
    await running.wait()
    two = turn_queue.schedule_turn("owner", 102, second)
    await asyncio.sleep(0.01)
    assert calls == ["first start"] and not one.cancelled()
    release.set()
    await asyncio.gather(one, two)
    assert calls == ["first start", "first end", "second start"]


async def test_conversations_do_not_block_each_other(monkeypatch):
    monkeypatch.setattr(turn_queue, "DEBOUNCE_SECONDS", 0)
    release = asyncio.Event()
    running = asyncio.Event()
    independent = asyncio.Event()

    async def first():
        turn_queue.mark_started("customer", 103)
        running.set()
        await release.wait()

    async def second():
        independent.set()

    one = turn_queue.schedule_turn("customer", 103, first)
    await running.wait()
    two = turn_queue.schedule_turn("customer", 104, second)
    await asyncio.wait_for(independent.wait(), 1)
    release.set()
    await asyncio.gather(one, two)


def test_no_event_loop_does_not_create_an_orphaned_turn():
    with pytest.raises(RuntimeError):
        turn_queue.schedule_turn("customer", 105, lambda: None)
    assert ("customer", 105) not in turn_queue._states
