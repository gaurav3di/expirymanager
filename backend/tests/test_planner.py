"""What a plan promises, and what the pipeline actually spends against that promise.

Two properties live here and both of them are about money.

The first is the chunk seam. A download is decomposed into windows of at most 100 calendar days
and the windows are produced in two different places: the planner emits the newest one and the
handler's backward walk emits every older one. Nothing forces those two to agree. If they overlap,
the same day is downloaded twice and the delete-then-insert write silently discards the duplicate,
so the waste is invisible. If they leave a gap, a day of history is missing and a coverage ledger
that claims to hold the range says otherwise. Neither shows up as an error, so both are asserted
here against the modelled transport, which knows exactly which bars belong to which day.

The second is the confirm gate, and it is the most expensive defect this project has had. A sheet
priced at 4 requests, committed with `confirm_requests=4`, spent 107 real Fyers requests and
downloaded 109,412 rows, because the planner clamped `range_from` by the sheet and the handler's
backward walk did not. It was invisible to 3,786 passing tests and was found by driving the product
with curl. The assertion that catches it is not on a mock, a log line or a return code: it is the
transport's own request count, after a real plan, a real commit and a real worker pool have run.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from expirymanager.api.schemas.downloads import DownloadRequest, PlanRequest
from expirymanager.brokers.fyers.calendar import EXCHANGE_DATA_FLOOR, MAX_DAYS_PER_REQUEST
from expirymanager.pipeline.jobs import JobServiceError

from tests.conftest import (
    RES_CODE,
    candle_count,
    candle_stamps,
    coverage_windows,
    job_row,
    register_underlying,
    seed_catalog,
    task_rows,
)
from tests.fake_fyers import MAX_SPAN_DAYS, FakeFyers

# A Thursday, the NSE weekly expiry weekday for this period. Two months in the past relative to
# the planning clock, so nothing is clamped by the partial candle rule.
EXPIRY = date(2025, 3, 27)
PLANNING_DAY = date(2025, 6, 1)

# 120 days of life against a 100 day request limit is the smallest life that forces a second
# chunk, and it puts the availability floor inside the second chunk rather than beyond it.
OPTION_LIFE_DAYS = 120


def sheet(**overrides) -> DownloadRequest:
    body = {
        "underlying_id": 1,
        "expiry_dates": [EXPIRY],
        "resolutions": [RES_CODE],
        "instrument_class": "OPT",
        "option_types": ["CE"],
    }
    body.update(overrides)
    return DownloadRequest(**body)


# ---------------------------------------------------------------------------
# The chunk seam
# ---------------------------------------------------------------------------


class TestTheChunkSeam:
    async def test_adjacent_chunks_meet_exactly_across_the_hundred_day_boundary(
        self, registry_engine, catalog_store, make_world
    ):
        """Two windows, one boundary, and not one day counted twice or lost.

        The life is longer than one request may cover, so the planner emits the newest window and
        the handler's backward walk emits the older one. The seam between them is asserted three
        ways: the windows that went on the wire, the coverage ledger that was written, and the
        bars that landed. All three have to agree with the transport's own model of what traded.
        """
        register_underlying(registry_engine, option_life_days=OPTION_LIFE_DAYS)
        contracts = seed_catalog(catalog_store, expiry=EXPIRY)
        contract_id, symbol = contracts[0]

        fake = FakeFyers()
        # Life starts well before the availability floor of this plan, so the walk has a reason to
        # keep going and something has to stop it.
        fake.give_life(symbol, first_day=date(2024, 11, 1), last_day=EXPIRY)

        async with make_world(fake, clock=PLANNING_DAY) as world:
            job_id = await world.run_to_completion(sheet())

        assert job_row(registry_engine, job_id)["status"] == "completed"

        windows = sorted(fake.windows_for(symbol))
        assert len(windows) == 2, windows
        older, newer = windows

        # The seam itself. The older window ends the day before the newer one begins: no gap, and
        # no overlap. An off by one in either direction fails here.
        assert older[1] + timedelta(days=1) == newer[0]
        assert older[1] < newer[0]

        # The newest window is the full request size, which is what makes this the 100 day seam
        # rather than an arbitrary one.
        assert (newer[1] - newer[0]).days + 1 == MAX_DAYS_PER_REQUEST

        # The ledger agrees with the wire.
        ledger = coverage_windows(catalog_store, contract_id)
        assert [(row[0], row[1]) for row in ledger] == [older, newer]
        assert {row[2] for row in ledger} == {"ok"}

        # And the bars agree with both. Every bar the contract traded inside the covered span is
        # present exactly once, and nothing outside it leaked in.
        expected = fake.expected_bars(symbol, older[0], newer[1])
        stamps = candle_stamps(catalog_store, contract_id)
        assert stamps == sorted(expected)
        assert len(stamps) == len(set(stamps)), "a day at the seam was downloaded twice"
        assert sum(row[3] for row in ledger) == len(stamps)

    async def test_the_backward_walk_stops_exactly_on_the_exchange_data_floor(
        self, registry_engine, catalog_store, make_world
    ):
        """BSE serves nothing before 2023-08-07, so nothing is ever asked for before it.

        The contract is given a life that starts before the floor, so a walk that honoured only
        the contract's own history would step past it. Each of those steps is a governed request
        that can only ever answer empty.
        """
        floor = EXCHANGE_DATA_FLOOR["BSE"]
        expiry = date(2024, 1, 25)
        register_underlying(
            registry_engine,
            exchange="BSE",
            root="SENSEX",
            fyers_symbol="BSE:SENSEX-INDEX",
            option_life_days=400,
        )
        contracts = seed_catalog(
            catalog_store, expiry=expiry, exchange="BSE", root="SENSEX", strikes=(70000,)
        )
        contract_id, symbol = contracts[0]

        fake = FakeFyers()
        fake.give_life(symbol, first_day=date(2023, 6, 1), last_day=expiry)

        async with make_world(fake, clock=date(2024, 4, 1)) as world:
            await world.run_to_completion(sheet(expiry_dates=[expiry]))

        windows = sorted(fake.windows_for(symbol))
        assert windows, "nothing was requested at all"
        assert min(start for start, _end in windows) == floor
        assert all(start >= floor for start, _end in windows)

        # The seam holds here too, all the way down to the floor.
        for older, newer in zip(windows, windows[1:]):
            assert older[1] + timedelta(days=1) == newer[0]

        # Nothing below the floor landed, and nothing above it was skipped.
        stamps = candle_stamps(catalog_store, contract_id)
        assert stamps == sorted(fake.expected_bars(symbol, floor, expiry))
        assert min(stamps).date() >= floor

    async def test_no_request_is_ever_wider_than_the_measured_limit(
        self, registry_engine, catalog_store, make_world
    ):
        """101 calendar days is a hard 422, not a truncation, so a wide window loses everything.

        The fake reproduces that rather than quietly serving the first 100 days, which is what
        makes this assertion about the product rather than about the fake being generous.
        """
        register_underlying(registry_engine, option_life_days=365)
        contracts = seed_catalog(catalog_store, expiry=EXPIRY)
        contract_id, symbol = contracts[0]

        fake = FakeFyers()
        fake.give_life(symbol, first_day=date(2024, 4, 1), last_day=EXPIRY)

        async with make_world(fake, clock=PLANNING_DAY) as world:
            job_id = await world.run_to_completion(sheet())

        assert job_row(registry_engine, job_id)["status"] == "completed"
        assert fake.request_count >= 3, "the life was not long enough to need several chunks"
        for start, end in fake.windows_for(symbol):
            assert (end - start).days <= MAX_SPAN_DAYS, (start, end)
        assert all(call.status_code == 200 for call in fake.calls), "a window was refused as 422"
        assert candle_count(catalog_store, contract_id) > 0


# ---------------------------------------------------------------------------
# The confirm gate
# ---------------------------------------------------------------------------


class TestTheConfirmGateIsAPromiseAboutSpend:
    async def test_a_job_committed_at_its_confirmed_count_spends_no_more_than_that(
        self, registry_engine, catalog_store, make_world
    ):
        """The assertion that would have caught 107 requests against a confirmed 4.

        A real plan, a real commit through the confirm gate, a real worker pool, real databases,
        and the transport's own count of what left the process. The sheet names a four day window;
        every contract traded for months before it, so a backward walk that honours the contract's
        life instead of the sheet's left edge will happily spend fifty requests per contract
        discovering history nobody asked for.
        """
        register_underlying(registry_engine, option_life_days=200)
        contracts = seed_catalog(
            catalog_store, expiry=EXPIRY, strikes=(22900, 23000), rights=("CE", "PE")
        )
        window_start = EXPIRY - timedelta(days=3)

        fake = FakeFyers()
        for _cid, symbol in contracts:
            fake.give_life(symbol, first_day=date(2024, 11, 1), last_day=EXPIRY)

        async with make_world(fake, clock=PLANNING_DAY) as world:
            priced = await world.jobs.estimate(
                PlanRequest(
                    underlying_id=1,
                    expiry_dates=[EXPIRY],
                    resolutions=[RES_CODE],
                    instrument_class="OPT",
                    option_types=["CE", "PE"],
                    range_from=window_start,
                )
            )
            # Pricing is answered from local state. Nothing left the process to produce it.
            assert fake.request_count == 0
            confirmed = priced.requests_estimated
            assert confirmed == len(contracts)

            job_id = await world.run_to_completion(
                sheet(
                    option_types=["CE", "PE"],
                    range_from=window_start,
                    confirm_requests=confirmed,
                )
            )

            # The whole item, in one line. What was shown is what was spent.
            assert fake.request_count == confirmed, (
                f"a job confirmed at {confirmed} requests spent {fake.request_count}: "
                f"{fake.windows}"
            )
            # And every one of them went through the governor, so the budget the user is shown
            # cannot drift from the requests that actually left.
            assert world.governor.requests_used == fake.request_count

        assert job_row(registry_engine, job_id)["status"] == "completed"

        # No window reached to the left of the sheet, at any point in the walk.
        assert all(start >= window_start for _sym, start, _end in fake.windows)
        # And no follow-up task was ever written, which is the row level form of the same fact.
        assert len(task_rows(registry_engine, job_id)) == confirmed

    async def test_a_sheet_with_no_left_edge_is_still_bounded_by_the_life_window(
        self, registry_engine, catalog_store, make_world
    ):
        """The other half of the same clamp: no sheet edge means the life window bounds the walk.

        Worth asserting alongside the one above, because the cheap way to fix the confirm gate is
        to stop the backward walk entirely, and that would silently turn every scheduled backfill
        into a one chunk job that never reaches older history.
        """
        register_underlying(registry_engine, option_life_days=OPTION_LIFE_DAYS)
        contracts = seed_catalog(catalog_store, expiry=EXPIRY)
        _contract_id, symbol = contracts[0]

        fake = FakeFyers()
        fake.give_life(symbol, first_day=date(2024, 11, 1), last_day=EXPIRY)

        async with make_world(fake, clock=PLANNING_DAY) as world:
            await world.run_to_completion(sheet())

        starts = [start for start, _end in fake.windows_for(symbol)]
        assert len(starts) > 1, "the backward walk did not run at all"
        assert min(starts) == EXPIRY - timedelta(days=OPTION_LIFE_DAYS)

    async def test_a_stale_estimate_is_refused_rather_than_committed(
        self, registry_engine, catalog_store, make_world
    ):
        """A count the user was shown for a different plan buys nothing."""
        register_underlying(registry_engine, option_life_days=OPTION_LIFE_DAYS)
        contracts = seed_catalog(catalog_store, expiry=EXPIRY, strikes=(22900, 23000))
        fake = FakeFyers()
        for _cid, symbol in contracts:
            fake.give_life(symbol, first_day=date(2025, 1, 2), last_day=EXPIRY)

        async with make_world(fake, clock=PLANNING_DAY) as world:
            with pytest.raises(JobServiceError) as refusal:
                await world.jobs.create(sheet(confirm_requests=1))

        assert refusal.value.code == "plan_changed"
        assert refusal.value.status_code == 409
        # Refused before anything was spent, and refused before anything was written.
        assert fake.request_count == 0
        with_jobs = registry_engine.connect()
        try:
            from sqlalchemy import text

            assert with_jobs.execute(text("SELECT count(*) FROM job")).scalar_one() == 0
            assert with_jobs.execute(text("SELECT count(*) FROM task")).scalar_one() == 0
        finally:
            with_jobs.close()
