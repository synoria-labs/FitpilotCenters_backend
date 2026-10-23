"""Coalesce adjacent inputs while preparing context; serialize turns before any tools run."""
import asyncio
import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)
DEBOUNCE_SECONDS = 0.5


@dataclass
class TurnState:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    pending: asyncio.Task | None = None
    tasks: set = field(default_factory=set)


_states: dict[tuple, TurnState] = {}


def mark_started(scope, conversation_id):
    state = _states.get((scope, conversation_id))
    if state and state.pending is asyncio.current_task():
        state.pending = None  # From here, a new message must not cancel tool execution.


def schedule_turn(scope, conversation_id, run):
    loop = asyncio.get_running_loop()
    key = (scope, conversation_id)
    state = _states.setdefault(key, TurnState())
    if state.pending and not state.pending.done():
        state.pending.cancel()

    async def invoke():
        await asyncio.sleep(DEBOUNCE_SECONDS)
        async with state.lock:
            await run()

    task = loop.create_task(invoke())
    state.pending = task
    state.tasks.add(task)

    def done(finished):
        state.tasks.discard(finished)
        if state.pending is finished:
            state.pending = None
        if not finished.cancelled() and finished.exception() is not None:
            logger.error("chatbot_turn_queue scope=%s conversation=%s result=failed", scope, conversation_id)
        if not state.tasks and _states.get(key) is state:
            _states.pop(key, None)

    task.add_done_callback(done)
    return task
