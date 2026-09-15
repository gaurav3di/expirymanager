"""The auth gate: one reaction to N rejections, and a pipeline that resumes where it stopped.

There is no refresh flow in this product. A rejected token means the user logs in again, and
everything in flight has to survive that without losing work and without spending budget on a token
the broker has already refused.

Three properties, and each of them fails silently if it is wrong:

1. Eight workers see the same rejection within milliseconds of each other. Exactly one of them may
   react: one gate closure, one mode change, one banner, one login prompt. Eight reactions would be
   eight prompts and, in a product that had a refresh flow, eight refreshes racing each other.
2. A parked job is parked, not failed. Failing it would throw away a backfill that is most of the
   way done and make the user re-price and re-spend the whole thing.
3. Nothing consumes a retry attempt. An auth rejection is not the task's fault, and a task that
   burned its three attempts against a dead token is a task that fails permanently the moment the
   user logs back in.

Every credential here is synthetic.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime, timedelta

import httpx
from sqlalchemy import text

from expirymanager.brokers.fyers.tokens import (
    FyersCredentials,
    InMemoryTokenStore,
    TokenBroker,
    TokenState,
)

from tests.conftest import (
    RES_CODE,
    TERMINAL_JOB_STATES,
    candle_count,
    job_row,
    register_underlying,
    seed_catalog,
    task_rows,
    wait_until,
)
from tests.fake_fyers import FakeFyers, auth_error
from expirymanager.api.schemas.downloads import DownloadRequest

EXPIRY = date(2025, 3, 27)
PLANNING_DAY = date(2025, 6, 1)

SYNTHETIC_CREDENTIAL = FyersCredentials(
    credential_id="cred-synthetic-1",
    app_id="TESTAPP01-100",
    app_secret="synthetic-secret-not-a-real-value",
    redirect_uri="http://127.0.0.1:8000/",
    plan="standard",
    label="Fyers",
)


class StubCredentialStore:
    def __init__(self, credential: FyersCredentials | None = SYNTHETIC_CREDENTIAL) -> None:
        self._credential = credential

    def load_active(self) -> FyersCredentials | None:
        return self._credential


class CountingGovernor:
    """Only the surface the token broker touches, with the reactions counted."""

    def __init__(self) -> None:
        self.pauses: list[str] = []

    async def pause_auth(self, *, reason: str = "") -> None:
        self.pauses.append(reason)


def _broker() -> tuple[TokenBroker, CountingGovernor, InMemoryTokenStore]:
    tokens = InMemoryTokenStore()
    governor = CountingGovernor()
    broker = TokenBroker(
        credentials=StubCredentialStore(), tokens=tokens, governor=governor
    )
    return broker, governor, tokens


def _stored_state(tokens: InMemoryTokenStore) -> str:
    """The state the token row itself carries, read past the active-only load filter."""
    record, _secret = tokens._tokens[SYNTHETIC_CREDENTIAL.credential_id]
    return record.state


def _store_a_login(tokens: InMemoryTokenStore, *, generation: int = 1) -> None:
    tokens.save(
        credential_id=SYNTHETIC_CREDENTIAL.credential_id,
        # A JWT shaped synthetic string. Nothing here is or resembles a real credential.
        access_token="header.payload.signature",
        refresh_token=None,
        generation=generation,
        access_expires_at=(datetime.now(UTC) + timedelta(hours=12)).isoformat(),
        refresh_expires_at=None,
    )


# ---------------------------------------------------------------------------
# Single flight
# ---------------------------------------------------------------------------


class TestOneReactionToManyRejections:
    async def test_eight_simultaneous_auth_errors_park_exactly_once(self):
        """Eight workers, one rejection each, one reaction between them."""
        broker, governor, tokens = _broker()
        _store_a_login(tokens)
        assert broker.has_valid_token()
        generation = broker.generation

        results = await asyncio.gather(
            *(broker.on_auth_error(generation, reason="invalid token") for _ in range(8))
        )

        assert sum(1 for parked in results if parked) == 1, results
        assert not broker.auth_gate.is_set()
        # One mode change, not eight. A second pause_auth would mean a second banner and, in any
        # product with a refresh flow, a second refresh racing the first.
        assert len(governor.pauses) == 1
        assert _stored_state(tokens) == TokenState.NEEDS_REAUTH
        # And the broker no longer offers a token at all, which is what stops the next request.
        assert broker.token_state() == "none"

    async def test_a_second_wave_on_the_same_generation_still_does_nothing(self):
        """A worker that was slow to report must not reopen the reaction."""
        broker, governor, tokens = _broker()
        _store_a_login(tokens)
        # What the supervisor does at startup before any worker reads the generation: the broker
        # loads lazily, so the generation is only meaningful once something has asked for it.
        assert broker.has_valid_token()
        generation = broker.generation

        assert await broker.on_auth_error(generation) is True
        later = await asyncio.gather(
            *(broker.on_auth_error(generation) for _ in range(4))
        )
        assert not any(later)
        assert len(governor.pauses) == 1

    async def test_a_rejection_from_before_the_new_login_cannot_undo_it(self):
        """The late report is the dangerous one: it would park a token that is good.

        A worker holding a request issued under the old token reports its rejection after the user
        has already logged in again. Acting on it would close the gate on a working token and
        would look exactly like the new login having failed.
        """
        broker, governor, tokens = _broker()
        _store_a_login(tokens, generation=1)
        assert broker.has_valid_token()
        stale_generation = broker.generation
        assert await broker.on_auth_error(stale_generation) is True

        _store_a_login(tokens, generation=2)
        broker.reload()
        assert broker.generation == 2
        assert broker.auth_gate.is_set(), "the new login did not reopen the gate"

        assert await broker.on_auth_error(stale_generation) is False
        assert broker.auth_gate.is_set(), "a stale rejection closed the gate on a good token"
        assert len(governor.pauses) == 1


# ---------------------------------------------------------------------------
# The gate, through the real pool
# ---------------------------------------------------------------------------


def _order(**overrides) -> DownloadRequest:
    body = {
        "underlying_id": 1,
        "expiry_dates": [EXPIRY],
        "resolutions": [RES_CODE],
        "instrument_class": "OPT",
        "option_types": ["CE", "PE"],
    }
    body.update(overrides)
    return DownloadRequest(**body)


class TestTheAuthGateParksTheJob:
    async def test_a_rejection_parks_the_job_keeps_every_attempt_and_resumes_after_login(
        self, registry_engine, catalog_store, make_world
    ):
        """The whole recovery path, driven end to end against a transport that goes dead.

        Two requests succeed, then the token is rejected for everything that follows. The pool has
        to stop, the job has to be parked rather than failed, no task may lose an attempt, and the
        morning login has to resume at the exact task without re-running the ones that finished.
        """
        register_underlying(registry_engine, option_life_days=60)
        contracts = seed_catalog(
            catalog_store, expiry=EXPIRY, strikes=(22900, 23000, 23100), rights=("CE", "PE")
        )
        fake = FakeFyers()
        for _cid, symbol in contracts:
            # The life sits inside the planned window, so each contract is one request and the
            # backward walk adds nothing to count.
            fake.give_life(symbol, first_day=date(2025, 3, 3), last_day=EXPIRY)

        token_is_dead = {"yes": False}
        succeeded_before_park: set[str] = set()

        def hook(request: httpx.Request, index: int) -> httpx.Response | None:
            symbol = dict(request.url.params).get("symbol", "")
            if token_is_dead["yes"]:
                return auth_error()
            if index >= 2:
                # Everything from the third request on is rejected. The two that already answered
                # are what the resume must not repeat.
                token_is_dead["yes"] = True
                return auth_error()
            succeeded_before_park.add(symbol)
            return None

        fake.inject = hook

        async with make_world(fake, clock=PLANNING_DAY, worker_count=2) as world:
            accepted = await world.jobs.create(_order())
            job_id = accepted.job_id
            assert accepted.total_tasks == len(contracts)

            await world.wait_for_status(job_id, "blocked_auth")

            # Parked, not failed. A failed job is a backfill the user has to re-price.
            row = job_row(registry_engine, job_id)
            assert row["status"] == "blocked_auth"
            assert row["status"] not in TERMINAL_JOB_STATES

            # The gate closed exactly once, whatever the workers saw.
            assert world.broker.parked_count == 1
            assert not world.broker.auth_gate.is_set()

            # And it stays shut. An ungated pool would spend the rest of the job in this window.
            spent = fake.request_count
            await asyncio.sleep(0.2)
            assert fake.request_count == spent, "the pool kept spending after the gate closed"
            assert spent < len(contracts), f"the gate let {spent} of {len(contracts)} through"

            # Nothing burned an attempt on a dead token.
            parked_tasks = task_rows(registry_engine, job_id)
            assert {task["attempt"] for task in parked_tasks} == {0}
            assert {task["state"] for task in parked_tasks} <= {"pending", "done"}
            assert sum(1 for task in parked_tasks if task["state"] == "done") == len(
                succeeded_before_park
            )

            # The morning login.
            token_is_dead["yes"] = False
            fake.inject = None
            world.broker.log_in_again()
            await world.supervisor.on_login()
            await wait_until(
                lambda: job_row(registry_engine, job_id)["status"] in TERMINAL_JOB_STATES
            )

            resumed = task_rows(registry_engine, job_id)
            assert job_row(registry_engine, job_id)["status"] == "completed"
            assert {task["state"] for task in resumed} == {"done"}
            # Still zero. The park cost nothing and the resume cost nothing.
            assert {task["attempt"] for task in resumed} == {0}

        # Resumed at the exact task: what finished before the park was never requested again.
        for symbol in succeeded_before_park:
            assert len(fake.windows_for(symbol)) == 1, (
                f"{symbol} was downloaded again after the login"
            )
        # Every contract landed, exactly once.
        for contract_id, _symbol in contracts:
            assert candle_count(catalog_store, contract_id) > 0
        stamps_total = sum(
            candle_count(catalog_store, contract_id) for contract_id, _s in contracts
        )
        assert stamps_total == candle_count(catalog_store)

    async def test_an_auth_rejection_never_writes_a_coverage_row(
        self, registry_engine, catalog_store, make_world
    ):
        """A failed window must not look downloaded.

        A coverage row is a promise that the window is held, and the planner subtracts it from the
        next plan. One written on an auth rejection would make the missing data permanently
        invisible: the plan would skip it and no sweep would ever come back for it.
        """
        register_underlying(registry_engine, option_life_days=60)
        contracts = seed_catalog(catalog_store, expiry=EXPIRY)
        fake = FakeFyers()
        for _cid, symbol in contracts:
            fake.give_life(symbol, first_day=date(2025, 3, 3), last_day=EXPIRY)
        fake.inject = lambda _request, _index: auth_error(code=-15, message="Invalid token")

        async with make_world(fake, clock=PLANNING_DAY, worker_count=1) as world:
            accepted = await world.jobs.create(_order(option_types=["CE"]))
            await world.wait_for_status(accepted.job_id, "blocked_auth")

        cur = catalog_store.cursor()
        try:
            assert cur.execute("SELECT count(*) FROM candle_coverage").fetchone()[0] == 0
        finally:
            cur.close()
        assert candle_count(catalog_store) == 0
        with registry_engine.connect() as connection:
            states = [
                row[0]
                for row in connection.execute(
                    text("SELECT state FROM task WHERE job_id = :j"),
                    {"j": accepted.job_id},
                )
            ]
        assert set(states) == {"pending"}
