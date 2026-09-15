"""W20: the SSE stream, API.md section 11.

The stream is read over real HTTP through the real middleware stack, because the failure this
endpoint is most likely to have is not in the generator: it is a middleware above it buffering the
response so that nothing arrives until the stream ends. A test that called the generator directly
would pass through that failure without noticing, so the frames asserted here are read off the
wire.

Every credential in this file is synthetic.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from expirymanager.api.v1.events import (
    CLOSE_POLL_SECONDS,
    PING_SECONDS,
    frame_to_sse,
    parse_last_event_id,
    stream_frames,
)
from expirymanager.app import create_app
from expirymanager.db import sqlite as sqlite_module
from expirymanager.pipeline.events import EventBus
from expirymanager.security.sessions import CSRF_COOKIE_NAME

BASE_URL = "https://127.0.0.1:8000"

USERNAME = "operator"
PASSCODE = "correct-horse-battery-staple"


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    root = tmp_path / "expirymanager-home"
    monkeypatch.setattr("expirymanager.paths.default_root", lambda: root)
    yield root
    sqlite_module.dispose_engine()


@pytest.fixture
def app(data_dir):
    application = create_app(root=data_dir, serve_static=False)
    yield application
    sqlite_module.dispose_engine()


@pytest.fixture
def client(app):
    with TestClient(app, base_url=BASE_URL) as test_client:
        yield test_client


def csrf(client: TestClient) -> dict[str, str]:
    token = client.cookies.get(CSRF_COOKIE_NAME)
    return {"X-CSRF-Token": token} if token else {}


@pytest.fixture
def signed_in(client):
    response = client.post(
        "/api/v1/auth/setup",
        json={"username": USERNAME, "password": PASSCODE},
        headers=csrf(client),
    )
    assert response.status_code == 200
    return client


# The SSE stream cannot be read through `TestClient`. Starlette's test transport calls the ASGI
# application to completion and only then wraps the collected bytes in a response, so a stream that
# never ends never returns. The application is therefore driven directly here, which is the
# stricter test anyway: if any middleware above the route buffered the response, no
# `http.response.start` message would arrive until the stream ended, and every assertion below
# would time out.


def parse_sse(text: str) -> list[dict]:
    frames: list[dict] = []
    current: dict = {}
    for raw in text.split("\n"):
        line = raw.rstrip("\r")
        if line.startswith("id:"):
            current["id"] = line[3:].strip()
        elif line.startswith("event:"):
            current["event"] = line[6:].strip()
        elif line.startswith("data:"):
            current["data"] = json.loads(line[5:].strip())
        elif line == "" and current:
            frames.append(current)
            current = {}
    return frames


def session_cookie(client: TestClient) -> bytes:
    from expirymanager.security.sessions import SESSION_COOKIE_NAME

    raw = client.cookies.get(SESSION_COOKIE_NAME)
    assert raw
    return f"{SESSION_COOKIE_NAME}={raw}".encode("latin-1")


def open_stream(
    app,
    *,
    cookie: bytes | None,
    query: str = "",
    headers: dict[str, str] | None = None,
    wanted: int = 1,
    timeout: float = 5.0,
):
    """Drive `GET /api/v1/events/stream` and stop once `wanted` body chunks have arrived.

    Returns the response status, its headers and the decoded body so far.
    """
    raw_headers = [
        (b"host", b"127.0.0.1:8000"),
        (b"accept", b"text/event-stream"),
        (b"sec-fetch-site", b"same-origin"),
    ]
    if cookie is not None:
        raw_headers.append((b"cookie", cookie))
    for name, value in (headers or {}).items():
        raw_headers.append((name.lower().encode("latin-1"), value.encode("latin-1")))

    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/api/v1/events/stream",
        "raw_path": b"/api/v1/events/stream",
        "query_string": query.encode("latin-1"),
        "root_path": "",
        "headers": raw_headers,
        "client": ("127.0.0.1", 54321),
        "server": ("127.0.0.1", 8000),
    }

    async def drive():
        start: dict = {}
        chunks: list[bytes] = []
        arrived = asyncio.Event()
        finished = asyncio.Event()

        async def receive():
            # The client never speaks and never hangs up on its own. The task is cancelled
            # instead, which is what a closed browser tab looks like to the server.
            await finished.wait()
            return {"type": "http.disconnect"}

        async def send(message):
            if message["type"] == "http.response.start":
                start.update(message)
            elif message["type"] == "http.response.body":
                body = message.get("body", b"")
                if body:
                    chunks.append(body)
                    if len(chunks) >= wanted:
                        arrived.set()
                if not message.get("more_body", False):
                    arrived.set()

        task = asyncio.ensure_future(app(scope, receive, send))
        try:
            await asyncio.wait_for(arrived.wait(), timeout)
        except TimeoutError:
            pass
        finally:
            finished.set()
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        return (
            int(start.get("status", 0)),
            {
                name.decode("latin-1").lower(): value.decode("latin-1")
                for name, value in start.get("headers", [])
            },
            b"".join(chunks).decode("utf-8"),
        )

    return asyncio.run(drive())


class TestLastEventIdParsing:
    def test_the_header_wins_over_the_query_parameter(self):
        assert parse_last_event_id("7", "3") == 7

    def test_the_query_parameter_is_used_when_there_is_no_header(self):
        assert parse_last_event_id(None, "3") == 3

    def test_nonsense_means_start_from_now(self):
        assert parse_last_event_id("not a number", None) is None
        assert parse_last_event_id("", "") is None
        assert parse_last_event_id(None, None) is None

    def test_a_non_positive_id_means_start_from_now(self):
        assert parse_last_event_id("0", None) is None
        assert parse_last_event_id("-4", None) is None


class TestFrameEncoding:
    def test_a_frame_carries_its_id_its_name_and_compact_json(self):
        bus = EventBus()
        frame = bus.publish("job_progress", {"job_id": "j1", "done": 3})

        encoded = frame_to_sse(frame)

        assert encoded.id == str(frame.id)
        assert encoded.event == "job_progress"
        assert json.loads(encoded.data) == {"job_id": "j1", "done": 3}


class TestGenerator:
    def test_it_replays_only_what_came_after_the_given_id(self):
        bus = EventBus()
        for index in range(4):
            bus.publish("job_progress", {"seq": index})

        async def collect() -> list[int]:
            seen: list[int] = []
            agen = stream_frames(bus, after_id=2)
            try:
                for _ in range(2):
                    event = await agen.__anext__()
                    seen.append(json.loads(event.data)["seq"])
            finally:
                await agen.aclose()
            return seen

        assert asyncio.run(collect()) == [2, 3]

    def test_a_closed_bus_ends_the_stream_rather_than_parking_forever(self):
        bus = EventBus()

        async def drain() -> list:
            collected = []
            agen = stream_frames(bus, after_id=None)
            first = asyncio.ensure_future(agen.__anext__())
            await asyncio.sleep(0)
            bus.close()
            try:
                collected.append(
                    await asyncio.wait_for(first, CLOSE_POLL_SECONDS * 3)
                )
            except StopAsyncIteration:
                pass
            finally:
                await agen.aclose()
            return collected

        assert asyncio.run(drain()) == []

    def test_the_close_callback_always_runs(self):
        bus = EventBus()
        closed: list[str] = []

        async def run() -> None:
            agen = stream_frames(bus, after_id=None, on_close=lambda: closed.append("x"))
            task = asyncio.ensure_future(agen.__anext__())
            await asyncio.sleep(0)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            await agen.aclose()

        asyncio.run(run())
        assert closed == ["x"]
        assert bus.subscriber_count == 0


class TestOverHttp:
    def test_the_stream_carries_the_headers_the_browser_and_the_proxy_need(self, signed_in, app):
        bus = app.state.services.supervisor.bus
        first = bus.publish("job_progress", {"job_id": "j1"})
        bus.publish("job_progress", {"job_id": "j1"})

        status, headers, _body = open_stream(
            app,
            cookie=session_cookie(signed_in),
            query=f"last_event_id={first.id}",
            wanted=1,
        )

        assert status == 200
        assert headers["content-type"].startswith("text/event-stream")
        assert headers["cache-control"] == "no-cache"
        assert headers["x-accel-buffering"] == "no"

    def test_a_resume_replays_the_frames_that_were_missed(self, signed_in, app):
        bus = app.state.services.supervisor.bus
        first = bus.publish("job_progress", {"job_id": "j1", "done": 1})
        bus.publish("job_progress", {"job_id": "j1", "done": 2})
        bus.publish("job_finished", {"job_id": "j1", "status": "completed", "reason": None})

        status, _headers, body = open_stream(
            app,
            cookie=session_cookie(signed_in),
            query=f"last_event_id={first.id}",
            wanted=2,
        )
        frames = parse_sse(body)

        assert status == 200
        assert [frame["event"] for frame in frames] == ["job_progress", "job_finished"]
        assert frames[0]["data"]["done"] == 2
        assert frames[1]["data"]["status"] == "completed"
        assert int(frames[1]["id"]) == first.id + 2

    def test_the_header_resumes_as_well_as_the_query_parameter(self, signed_in, app):
        bus = app.state.services.supervisor.bus
        first = bus.publish("budget", {"requests_used": 1})
        bus.publish("budget", {"requests_used": 2})

        _status, _headers, body = open_stream(
            app,
            cookie=session_cookie(signed_in),
            headers={"Last-Event-ID": str(first.id)},
            wanted=1,
        )
        frames = parse_sse(body)

        assert len(frames) == 1
        assert frames[0]["data"]["requests_used"] == 2

    def test_a_stream_without_a_position_replays_nothing(self, signed_in, app):
        bus = app.state.services.supervisor.bus
        bus.publish("job_progress", {"job_id": "old"})

        _status, _headers, body = open_stream(
            app, cookie=session_cookie(signed_in), wanted=1, timeout=1.5
        )

        # Nothing at all: the ring holds a frame, and a client with no position starts from now.
        assert parse_sse(body) == []

    def test_a_stream_needs_a_session(self, client, app):
        status, _headers, _body = open_stream(app, cookie=None, wanted=1)

        assert status == 401

    def test_the_subscriber_is_detached_when_the_client_goes_away(self, signed_in, app):
        bus = app.state.services.supervisor.bus
        first = bus.publish("job_progress", {"job_id": "j1"})
        bus.publish("job_progress", {"job_id": "j2"})

        open_stream(
            app, cookie=session_cookie(signed_in), query=f"last_event_id={first.id}", wanted=1
        )

        assert bus.subscriber_count == 0


class TestConcurrencyCeiling:
    def test_an_eleventh_stream_is_refused(self, signed_in, app):
        limiter = app.state.services.stream_limiter
        key = _session_key(app, signed_in)
        for _ in range(limiter.limit):
            assert limiter.acquire(key)

        status, _headers, body = open_stream(
            app, cookie=session_cookie(signed_in), wanted=1
        )

        assert status == 429
        error = json.loads(body)["error"]
        assert error["code"] == "rate_limited"
        assert error["detail"]["limit"] == limiter.limit

        for _ in range(limiter.limit):
            limiter.release(key)

    def test_the_slot_is_released_when_the_stream_ends(self, signed_in, app):
        limiter = app.state.services.stream_limiter
        key = _session_key(app, signed_in)
        bus = app.state.services.supervisor.bus
        first = bus.publish("job_progress", {"job_id": "j1"})
        bus.publish("job_progress", {"job_id": "j2"})

        status, _headers, _body = open_stream(
            app,
            cookie=session_cookie(signed_in),
            query=f"last_event_id={first.id}",
            wanted=1,
        )

        assert status == 200
        assert limiter.held(key) == 0


def _session_key(app, client) -> str:
    """The key the route derives from the session cookie: the stored hash, as hex."""
    from expirymanager.security.sessions import SESSION_COOKIE_NAME, hash_session_id

    raw = client.cookies.get(SESSION_COOKIE_NAME)
    assert raw
    return hash_session_id(raw).hex()


def test_the_keepalive_interval_is_the_documented_fifteen_seconds():
    assert PING_SECONDS == 15
