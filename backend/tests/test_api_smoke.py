"""Every read route, against a real application on a database that has nothing in it yet.

This is the first-run sweep. A fresh install has no contracts, no candles, no coverage, no jobs and
no token, which is precisely the state in which an unguarded join, a `.one()` on an empty result or
a division by a zero row count turns into a 500. Every one of those looks fine on the developer's
machine, because the developer's machine has data.

So the routes are not listed by hand here. They are read off the running application, so a route
added later is swept without anyone remembering to add it, and the assertion is that none of them
answers 500 on an empty install.

Two named checks from SECURITY.md section 13 live here as well: the login ceiling answers 429 with
a Retry-After, and an OAuth state is single use.

Every credential in this file is synthetic.
"""

from __future__ import annotations

from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi.testclient import TestClient

from expirymanager.app import create_app
from expirymanager.brokers.fyers.client import FyersClient
from expirymanager.db import sqlite as sqlite_module
from expirymanager.security.sessions import CSRF_COOKIE_NAME

BASE_URL = "http://127.0.0.1:8000"

USERNAME = "operator"
PASSCODE = "correct-horse-battery-staple"

SYNTHETIC_APP_ID = "TESTAPP01-100"
SYNTHETIC_APP_SECRET = "synthetic-app-secret-not-a-real-value"
SYNTHETIC_AUTH_CODE = "synthetic-auth-code-not-a-real-value"

# Routes excluded from the sweep, each for a reason that is not "it fails".
SWEEP_SKIP = {
    # Server sent events: the response never completes, so a plain GET would hang the sweep.
    "/api/v1/events/stream",
}


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


def sign_in(client: TestClient) -> None:
    response = client.post(
        "/api/v1/auth/setup",
        json={"username": USERNAME, "password": PASSCODE},
        headers=csrf(client),
    )
    assert response.status_code == 200, response.text


def _callback_reason(response) -> str:
    """The `reason` the callback redirect carries, which is the only thing that varies."""
    query = parse_qs(urlsplit(response.headers["location"]).query)
    return (query.get("reason") or [""])[0]


def parameterless_get_paths(app) -> list[str]:
    """Every GET route that needs no path parameter, read off the application's own schema.

    The schema rather than `app.routes`, because the router nests and the nesting is an internal
    detail of the web framework. The schema is the route surface the product publishes, so a route
    that is added and documented is swept without anyone having to remember this file.
    """
    return sorted(
        path
        for path, operations in app.openapi()["paths"].items()
        if "get" in operations and "{" not in path and path not in SWEEP_SKIP
    )


class TestEveryReadRouteAnswersOnAnEmptyInstall:
    def test_no_get_route_returns_a_server_error_before_any_data_exists(self, app, client):
        sign_in(client)
        paths = parameterless_get_paths(app)
        assert len(paths) >= 10, f"the sweep found almost nothing to sweep: {paths}"

        broken: list[tuple[str, int, str]] = []
        for path in paths:
            response = client.get(path)
            if response.status_code >= 500:
                broken.append((path, response.status_code, response.text[:200]))
        assert broken == []

    def test_the_same_sweep_is_refused_rather_than_served_without_a_session(self, app, client):
        """An unauthenticated browser must be turned away, not answered with an empty payload.

        A route that quietly returns an empty list to a stranger looks identical to a route that
        is working, and this product's catalog is a record of what the user has downloaded.
        """
        sign_in(client)
        paths = [
            path
            for path in parameterless_get_paths(app)
            if path.startswith("/api/v1/") and not path.startswith("/api/v1/auth/")
        ]
        client.cookies.clear()

        served: list[str] = []
        for path in paths:
            response = client.get(path)
            if response.status_code < 400:
                served.append(path)
        # Bootstrap is the one route a signed out browser is meant to reach: it is what tells the
        # front end whether to render the setup screen or the login screen.
        assert served in ([], ["/api/v1/bootstrap"]), served


class TestTheLoginCeiling:
    def test_the_sixth_attempt_is_refused_with_a_retry_after(self, client):
        sign_in(client)
        client.post("/api/v1/auth/logout", headers=csrf(client))

        statuses: list[int] = []
        last = None
        for _ in range(8):
            last = client.post(
                "/api/v1/auth/login",
                json={"username": USERNAME, "password": "wrong-passcode-entirely"},
                headers=csrf(client),
            )
            statuses.append(last.status_code)
            if last.status_code == 429:
                break

        assert 429 in statuses, statuses
        assert statuses.index(429) <= 6, statuses
        assert int(last.headers["Retry-After"]) > 0
        # A refusal must not leak which half of the pair was wrong.
        assert PASSCODE not in last.text
        assert USERNAME not in last.text


class TestTheOAuthStateIsSingleUse:
    def test_replaying_the_same_callback_url_fails_identically_the_second_time(self, app, client):
        """A state is a one shot nonce. A replay has to fail the same way, not differently.

        Failing differently is itself the leak: a second answer that says "already used" rather
        than "invalid" tells whoever replayed the URL that they had a real one.
        """
        sign_in(client)
        # No socket, in either direction. The callback path reaches for the client on its way to
        # the token exchange, and a test that let that leave the machine would be asserting the
        # developer's network rather than the product.
        offline = httpx.MockTransport(
            lambda _request: httpx.Response(
                503, json={"s": "error", "code": -99, "message": "no exchange in a test"}
            )
        )
        services = app.state.services
        services.fyers_client = FyersClient(
            tokens=services.token_broker, governor=services.governor, transport=offline
        )
        saved = client.post(
            "/api/v1/broker/fyers/credentials",
            json={
                "label": "Primary",
                "app_id": SYNTHETIC_APP_ID,
                "app_secret": SYNTHETIC_APP_SECRET,
                "redirect_uri": "http://127.0.0.1:8000/",
                "plan": "standard",
            },
            headers=csrf(client),
        )
        assert saved.status_code == 200, saved.text

        started = client.post("/api/v1/broker/fyers/connect", headers=csrf(client))
        assert started.status_code == 200, started.text
        state = parse_qs(urlsplit(started.json()["authorize_url"]).query)["state"][0]

        params = {
            "s": "ok",
            "code": "200",
            "auth_code": SYNTHETIC_AUTH_CODE,
            "state": state,
        }
        first = client.get("/fyers/callback", params=params, follow_redirects=False)
        second = client.get("/fyers/callback", params=params, follow_redirects=False)
        third = client.get("/fyers/callback", params=params, follow_redirects=False)

        # The callback always redirects, so the status alone says nothing. The reason carried on
        # the redirect is what distinguishes a state that was accepted from one that was not.
        assert first.status_code == second.status_code == third.status_code == 303
        first_reason = _callback_reason(first)
        second_reason = _callback_reason(second)
        third_reason = _callback_reason(third)

        # The state was spent on the first attempt: it failed at the token exchange, which is a
        # step past the state check. Had it been rejected here too, this test would prove nothing.
        assert first_reason == "exchange_failed", first.headers.get("location")
        # And it is gone. Every later attempt is refused for the state and refused identically, so
        # a replay cannot learn that it once held a real one.
        assert second_reason == "state_invalid"
        assert third_reason == second_reason
        assert second.headers["location"] == third.headers["location"]

        # Nothing about the attempt is echoed back to the browser.
        for response in (first, second, third):
            assert SYNTHETIC_AUTH_CODE not in response.headers["location"]
            assert SYNTHETIC_AUTH_CODE not in response.text
            assert SYNTHETIC_APP_SECRET not in response.text
