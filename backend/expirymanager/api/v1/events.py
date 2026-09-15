"""API.md section 11: the one multiplexed SSE stream.

The stream is a refresh accelerator and never the source of truth. Every frame it carries has a
REST equivalent and every screen keeps its own slow refetch, so a dropped stream degrades the
refresh rate and never the correctness of what is displayed. That is what makes the drop policy in
`pipeline/events.py` acceptable: a subscriber that falls behind loses its oldest undelivered frame
and carries a counter, rather than stalling the worker that published it.

Three mechanical details decide whether this route works at all:

`BaseHTTPMiddleware` must not appear anywhere above it. It runs the downstream app in an anyio
task group and buffers the response, which turns this endpoint into a response that arrives only
once the stream ends. Every middleware in `app.py` is pure ASGI for this one reason, and this
docstring is the second place that says so.

Resume comes from two places. The browser sends `Last-Event-ID` by itself on its own automatic
reconnects. When it gives up and the hook builds a fresh `EventSource`, a header cannot be set on
the constructor, so `useEventStream.ts` carries the id forward as `?last_event_id=`. Both are
honoured, the header first, because that is the one the browser controls.

The generator polls for closure rather than blocking forever on the queue. A bus closed at
shutdown would otherwise leave every open stream parked in `await queue.get()` until its socket
died, which is a process that will not exit.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, Query, Request
from sse_starlette.event import ServerSentEvent
from sse_starlette.sse import EventSourceResponse

from expirymanager.api.deps import RequiredSessionDep, StreamLimiterDep, SupervisorDep
from expirymanager.api.errors import ApiError, CODE_RATE_LIMITED
from expirymanager.pipeline.events import EventBus, Frame

__all__ = [
    "router",
    "PING_SECONDS",
    "CLOSE_POLL_SECONDS",
    "CODE_TOO_MANY_STREAMS",
    "parse_last_event_id",
    "frame_to_sse",
    "stream_frames",
]

log = logging.getLogger(__name__)

router = APIRouter()

# API.md section 11 and PIPELINE.md section 8. A comment frame every fifteen seconds is what keeps
# an idle stream alive through anything that times out a silent connection.
PING_SECONDS = 15

# How long the generator waits on the queue before looking at whether the bus has closed. Short
# enough that shutdown is not perceptibly delayed, long enough that an idle stream is not a spin.
CLOSE_POLL_SECONDS = 1.0

CODE_TOO_MANY_STREAMS = "too_many_streams"


def parse_last_event_id(header: str | None, query_value: str | None) -> int | None:
    """The replay position, from the header first and the query string second.

    Anything that is not a positive integer is None, which means "start from now". That is the
    honest reading of a client that has no position: replaying the whole ring to a browser that
    never had one would deliver frames about jobs it has already refetched over REST.
    """
    for candidate in (header, query_value):
        if candidate is None:
            continue
        text = str(candidate).strip()
        if not text:
            continue
        try:
            value = int(text)
        except ValueError:
            continue
        if value > 0:
            return value
    return None


def frame_to_sse(frame: Frame) -> ServerSentEvent:
    """One bus frame on the wire. `data` is compact JSON, `id` is the monotonic frame id."""
    return ServerSentEvent(
        data=json.dumps(dict(frame.data), separators=(",", ":"), default=str),
        event=frame.event,
        id=str(frame.id),
    )


async def stream_frames(
    bus: EventBus,
    *,
    after_id: int | None,
    on_close: Any = None,
) -> AsyncIterator[ServerSentEvent]:
    """Replay what was missed, then follow the bus until the client or the bus goes away.

    Subscribing before the replay is deliberate. A frame published between reading the ring and
    attaching the subscriber would otherwise be the one frame that is never delivered, and it
    would be the job_finished frame often enough to matter. Subscribing first can deliver a frame
    twice instead, which the browser resolves by id.
    """
    subscription = bus.subscribe()
    try:
        for frame in bus.replay(after_id):
            yield frame_to_sse(frame)
        while True:
            if subscription.closed and subscription.queue.empty():
                return
            try:
                frame = await asyncio.wait_for(subscription.get(), CLOSE_POLL_SECONDS)
            except TimeoutError:
                continue
            yield frame_to_sse(frame)
    finally:
        subscription.close()
        if on_close is not None:
            on_close()


@router.get("/events/stream", summary="Job progress, budget and notification frames")
async def events_stream(
    request: Request,
    session: RequiredSessionDep,
    supervisor: SupervisorDep,
    limiter: StreamLimiterDep,
    last_event_id: str | None = Query(None),
) -> EventSourceResponse:
    key = session.id_hash.hex()
    if not limiter.acquire(key):
        raise ApiError(
            429,
            CODE_RATE_LIMITED,
            f"This session already holds {limiter.limit} open event streams. "
            "Close a tab and try again.",
            detail={"limit": limiter.limit},
        )

    released = False

    def release() -> None:
        # Guarded, because the generator's finally runs once per stream and a double release
        # would hand this session a slot it never held.
        nonlocal released
        if not released:
            released = True
            limiter.release(key)

    after_id = parse_last_event_id(request.headers.get("last-event-id"), last_event_id)
    log.debug(
        "event stream opened",
        extra={"session_prefix": session.id_prefix, "resume_after": after_id or 0},
    )
    return EventSourceResponse(
        stream_frames(supervisor.bus, after_id=after_id, on_close=release),
        ping=PING_SECONDS,
        # API.md names both. `no-cache` rather than the library's `no-store` default, because a
        # stream is revalidated rather than stored, and `X-Accel-Buffering` is what stops a
        # buffering proxy from holding frames until the response ends.
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
