"""The outbound governor, under the concurrency it actually runs under.

The documented penalty for exceeding the per minute limit more than three times in one day is that
the account is blocked for the rest of that day. That is not a performance budget, it is a whole
trading day of downloads, so the two guarantees this module makes are asserted here against
measured send times rather than against the limiter's own bookkeeping.

What a limiter can get wrong while looking correct:

- A token bucket of capacity 8 refilling at 8 per second permits 16 grants inside one second at the
  refill boundary. Its internal counters stay self consistent the whole time. Only the rate at
  which requests actually leave the process shows the overshoot, so that is what is measured.
- A daily counter kept only in memory resets on a crash loop, and a crash loop is exactly when the
  counter matters. So it is spent, dropped and reloaded from a real SQLite file here.
- A rate limit response that is retried spends the second strike immediately. Three strikes are
  survivable and the fourth is not, so the first one has to stop the pipeline rather than back off.

The window lengths are parameters on FyersGovernor precisely so this can be asserted in
milliseconds instead of a minute of wall clock. The ratios and the limits are the production ones.
"""

from __future__ import annotations

import asyncio
import time
from datetime import date, datetime, timedelta

import pytest
from sqlalchemy import text

from expirymanager.brokers.fyers.throttle import (
    IST,
    MAX_MINUTE_VIOLATIONS,
    AccountBlocked,
    FyersGovernor,
    GovernorMode,
    InMemoryBudgetStore,
    SqliteBudgetStore,
)

from tests.conftest import make_chunk_task, make_job, register_underlying, seed_catalog
from tests.fake_fyers import FakeFyers, always, rate_limited

PER_SECOND = 8
PER_MINUTE = 170

# The production ratio, 60 to 1, scaled so a 200 caller run finishes in under a second. Scaling the
# clock rather than the limits is what keeps this a test of the guarantee instead of a test of a
# smaller limiter.
SHORT_SECOND = 0.05
SHORT_MINUTE = 3.0

# A grant and the send it authorises are not the same instant, and the measurement below is taken
# at the send. The second window is the last thing `acquire` waits on, so a send follows its own
# second-window grant with nothing awaited in between and the gap is microseconds. A minute-window
# grant is further back: the caller still has to pass the mode check, possibly a budget flush and
# the second window, which is bounded by SHORT_SECOND. Both measurement windows are therefore
# shrunk by the delay that can separate the grant from the send, which is what makes the assertion
# sound rather than merely usually true. Shrinking cannot hide an overshoot: extra sends land
# inside the window, not at its edge.
CLOCK_SLACK = 0.01
SECOND_MEASURE = SHORT_SECOND - CLOCK_SLACK
MINUTE_MEASURE = SHORT_MINUTE - SHORT_SECOND - CLOCK_SLACK

EXPIRY = date(2025, 3, 27)


def _max_in_any_window(stamps: list[float], window: float) -> int:
    """The largest number of grants inside any window of that length.

    The guarantee the limiter is written to keep is half open: grants strictly newer than
    `now - window` count, so the comparison here is strict too.
    """
    stamps = sorted(stamps)
    worst = 0
    for index, start in enumerate(stamps):
        count = 0
        for later in stamps[index:]:
            if later < start + window:
                count += 1
            else:
                break
        worst = max(worst, count)
    return worst


def _governor(**overrides):
    kwargs = {
        "budget_store": InMemoryBudgetStore(),
        "per_second": PER_SECOND,
        "per_minute": PER_MINUTE,
        "in_flight": 6,
        "daily_budget": 100_000,
        "second_window": SHORT_SECOND,
        "minute_window": SHORT_MINUTE,
    }
    kwargs.update(overrides)
    return FyersGovernor(**kwargs)


# ---------------------------------------------------------------------------
# Both windows, under concurrency
# ---------------------------------------------------------------------------


class TestBothWindowsHoldUnderConcurrency:
    async def test_two_hundred_concurrent_callers_never_exceed_either_limit(self):
        """The assertion is on when requests were allowed out, not on what the limiter thinks.

        Two hundred callers is more than the minute limit, so the run genuinely crosses a minute
        boundary and the second window has to keep holding on the far side of it. A bucket that
        refills at the boundary passes every internal check and fails here.
        """
        governor = _governor()
        sent: list[float] = []

        async def caller() -> None:
            async with governor.slot("expired-historical-data"):
                # Recorded inside the slot, so this is the moment the request would leave.
                sent.append(time.monotonic())

        await asyncio.gather(*(caller() for _ in range(200)))

        assert len(sent) == 200
        assert _max_in_any_window(sent, SECOND_MEASURE) <= PER_SECOND
        assert _max_in_any_window(sent, MINUTE_MEASURE) <= PER_MINUTE
        # The daily counter counted every one of them exactly once.
        assert governor.requests_used == 200

    async def test_the_in_flight_ceiling_is_never_exceeded(self):
        """The semaphore bounds concurrent sockets, which is a different thing from the rate."""
        governor = _governor(in_flight=4, per_second=1000, per_minute=100_000)
        concurrent = 0
        high_water = 0

        async def caller() -> None:
            nonlocal concurrent, high_water
            async with governor.slot("expired-historical-data"):
                concurrent += 1
                high_water = max(high_water, concurrent)
                await asyncio.sleep(0.005)
                concurrent -= 1

        await asyncio.gather(*(caller() for _ in range(40)))
        assert high_water <= 4, high_water
        assert governor.requests_used == 40

    async def test_a_pause_stops_the_pipeline_within_the_in_flight_ceiling(self):
        """A pause has to land while callers are already queued, not only before they arrive.

        The guarantee is bounded rather than instant, and the bound is the in flight ceiling. A
        caller that has already taken an in flight slot and passed the mode check is committed:
        its request is about to be sent and abandoning it there would leave the slot held. Nothing
        beyond that ceiling may get out, which is what makes a pause worth having.
        """
        in_flight = 2
        governor = _governor(per_second=4, per_minute=1000, in_flight=in_flight)
        sent: list[int] = []

        async def caller(index: int) -> None:
            async with governor.slot("expired-historical-data"):
                sent.append(index)

        runners = [asyncio.create_task(caller(index)) for index in range(40)]
        await asyncio.sleep(SHORT_SECOND * 1.5)
        await governor.pause_user()
        spent_at_pause = len(sent)
        await asyncio.sleep(SHORT_SECOND * 6)

        assert len(sent) <= spent_at_pause + in_flight, (
            f"{len(sent) - spent_at_pause} requests were granted after the pause, "
            f"which is past the in flight ceiling of {in_flight}"
        )
        assert governor.mode is GovernorMode.PAUSED_USER
        assert len(sent) < 40, "the pause stopped nothing at all"

        await governor.resume(by="test")
        await asyncio.wait_for(asyncio.gather(*runners), timeout=10)
        assert len(sent) == 40
        assert governor.requests_used == 40


# ---------------------------------------------------------------------------
# The durable counters
# ---------------------------------------------------------------------------


class TestTheDailyCounterSurvivesARestart:
    async def test_a_restart_reloads_the_spend_instead_of_starting_the_day_again(
        self, registry_engine
    ):
        """A counter a crash loop can reset is a counter that cannot enforce a daily budget."""
        today = date(2026, 9, 15)

        def now_ist() -> datetime:
            # Aware, and in IST. The governor keys its budget row on the IST date and compares
            # against a stored block expiry that carries a timezone.
            return datetime(2026, 9, 15, 11, 0, 0, tzinfo=IST)

        first = FyersGovernor(
            budget_store=SqliteBudgetStore(registry_engine),
            per_second=1000,
            per_minute=100_000,
            in_flight=8,
            daily_budget=100_000,
            now_ist=now_ist,
        )
        for _ in range(60):
            async with first.slot("expired-historical-data"):
                pass
        assert first.requests_used == 60
        # What the lifespan does on shutdown. Without it the unflushed tail is lost, which is the
        # point of flushing on close rather than only every 25 grants.
        await first.aclose()

        # A brand new process: a new governor, a new store, the same database file.
        second = FyersGovernor(
            budget_store=SqliteBudgetStore(registry_engine),
            per_second=1000,
            per_minute=100_000,
            in_flight=8,
            daily_budget=100_000,
            now_ist=now_ist,
        )
        assert second.requests_used == 60
        assert second.requests_remaining == 100_000 - 60

        async with second.slot("expired-historical-data"):
            pass
        assert second.requests_used == 61

        with registry_engine.connect() as connection:
            stored = connection.execute(
                text("SELECT requests_used FROM api_budget WHERE ist_date = :d"),
                {"d": today.isoformat()},
            ).scalar_one()
        assert stored >= 60

    async def test_a_strike_survives_a_restart_too(self, registry_engine):
        """A strike a crash forgets is a strike spent twice, and the fourth costs the day."""

        def now_ist() -> datetime:
            # Aware, and in IST. The governor keys its budget row on the IST date and compares
            # against a stored block expiry that carries a timezone.
            return datetime(2026, 9, 15, 11, 0, 0, tzinfo=IST)

        first = FyersGovernor(
            budget_store=SqliteBudgetStore(registry_engine),
            per_second=1000,
            per_minute=100_000,
            daily_budget=100_000,
            now_ist=now_ist,
        )
        await first.note_rate_limited(endpoint="expired-historical-data", http_status=429)
        assert first.minute_violations == 1
        assert first.strikes_remaining == MAX_MINUTE_VIOLATIONS - 1

        second = FyersGovernor(
            budget_store=SqliteBudgetStore(registry_engine),
            per_second=1000,
            per_minute=100_000,
            daily_budget=100_000,
            now_ist=now_ist,
        )
        assert second.minute_violations == 1
        assert second.strikes_remaining == MAX_MINUTE_VIOLATIONS - 1

    async def test_the_spent_budget_stops_the_pipeline_rather_than_overspending(self):
        governor = _governor(per_second=1000, per_minute=100_000, daily_budget=5)
        for _ in range(5):
            async with governor.slot("expired-historical-data"):
                pass
        assert governor.requests_used == 5
        assert governor.requests_remaining == 0

        blocked = asyncio.create_task(governor.acquire("expired-historical-data"))
        await asyncio.sleep(0.05)
        assert not blocked.done(), "the governor granted a request past the daily budget"
        assert governor.mode is GovernorMode.STOPPED_BUDGET
        blocked.cancel()
        with pytest.raises(asyncio.CancelledError):
            await blocked
        assert governor.requests_used == 5


# ---------------------------------------------------------------------------
# The first strike
# ---------------------------------------------------------------------------


class TestTheFirstRateLimitStops:
    async def test_one_rate_limited_response_pauses_and_does_not_retry_into_a_second_strike(
        self, registry_engine, catalog_store, make_task_harness
    ):
        """A real task, a real worker, a real governor and a transport that answers 429.

        The assertion is the transport's request count. A retry policy that treats a rate limit
        as transient would spend three requests here and therefore up to three strikes, and the
        task row would show the attempts being consumed. Both are checked.
        """
        register_underlying(registry_engine)
        contracts = seed_catalog(catalog_store, expiry=EXPIRY)
        contract_id, symbol = contracts[0]

        fake = FakeFyers()
        fake.give_life(symbol, first_day=date(2025, 1, 2), last_day=EXPIRY)
        fake.inject = always(rate_limited)

        job = make_job(registry_engine)
        async with make_task_harness(fake) as harness:
            task_id = make_chunk_task(
                registry_engine,
                job,
                contract_id=contract_id,
                symbol=symbol,
                expiry_date=EXPIRY,
                range_from=date(2025, 1, 1),
                range_to=EXPIRY,
                max_attempts=3,
            )
            await harness.run_one()

            assert fake.request_count == 1, "the rate limit was retried into another strike"
            assert harness.governor.minute_violations == 1
            assert harness.governor.strikes_remaining == MAX_MINUTE_VIOLATIONS - 1
            assert harness.governor.mode is GovernorMode.PAUSED_RATE
            assert harness.supervisor.rate_limits, "the supervisor was never told"

        with registry_engine.connect() as connection:
            row = connection.execute(
                text("SELECT state, attempt FROM task WHERE task_id = :t"), {"t": task_id}
            ).one()
        # Parked, not failed, and no attempt consumed: the task is owed, not spent.
        assert row[0] == "pending"
        assert row[1] == 0

    async def test_the_fourth_violation_blocks_the_account_for_the_rest_of_the_day(self):
        """Three strikes are survivable. The fourth is the one that costs the day."""
        moment = datetime(2026, 9, 15, 14, 0, 0, tzinfo=IST)
        governor = _governor(now_ist=lambda: moment)

        for strike in range(1, MAX_MINUTE_VIOLATIONS + 1):
            await governor.note_rate_limited(endpoint="expired-historical-data", http_status=429)
            assert governor.minute_violations == strike
            assert governor.mode is GovernorMode.PAUSED_RATE
            # Survivable, so a user resume is allowed to lift it.
            await governor.resume(by="test")
            assert governor.mode is GovernorMode.RUNNING

        await governor.note_rate_limited(endpoint="expired-historical-data", http_status=429)
        assert governor.minute_violations == MAX_MINUTE_VIOLATIONS + 1
        assert governor.strikes_remaining == 0
        assert governor.mode is GovernorMode.STOPPED_FATAL

        snapshot = governor.snapshot()
        assert snapshot.blocked_until is not None
        # Until the IST date rolls, and not one minute less.
        assert datetime.fromisoformat(snapshot.blocked_until).date() == moment.date() + timedelta(
            days=1
        )

        # And a resume cannot talk the broker out of it.
        with pytest.raises(AccountBlocked):
            await governor.resume(by="test")
        assert governor.mode is GovernorMode.STOPPED_FATAL

    async def test_a_blocked_account_grants_nothing(self):
        moment = datetime(2026, 9, 15, 14, 0, 0, tzinfo=IST)
        governor = _governor(now_ist=lambda: moment)
        for _ in range(MAX_MINUTE_VIOLATIONS + 1):
            await governor.note_rate_limited(endpoint="expired-historical-data", http_status=429)

        blocked = asyncio.create_task(governor.acquire("expired-historical-data"))
        await asyncio.sleep(0.05)
        assert not blocked.done(), "a blocked account was granted a request"
        blocked.cancel()
        with pytest.raises(asyncio.CancelledError):
            await blocked
        assert governor.requests_used == 0
