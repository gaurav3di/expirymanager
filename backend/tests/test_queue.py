"""The lease queue, judged by what a running pool actually did rather than by one statement.

`test_pipeline_queue.py` asserts each transition in isolation and does it well. This file asks the
question that only shows up when several workers, a real SQLite file and a real DuckDB store are in
the same room: did every task run exactly once, and did the ones that were not supposed to run cost
nothing.

Three failures are the reason it exists, and all three look like success from inside the queue:

- Two workers lease the same row. Each commits its own copy, the delete-then-insert write hides the
  duplicate rows, and the only evidence left is a request count that is one higher than the plan.
- A lease expires while its worker is still holding it. The task is reclaimed and run again, and
  then the first worker finishes and acks. If that stale ack lands, the row records an outcome from
  a run whose data was already replaced.
- A worker sleeps out a retry backoff while holding its lease. The queue looks fine and the row
  looks fine, and the pool quietly loses a worker for up to twenty four seconds per failure.
"""

from __future__ import annotations

import asyncio
import time
from datetime import date, timedelta

from sqlalchemy import text

from expirymanager.api.schemas.downloads import DownloadRequest
from expirymanager.pipeline.queue import TaskOutcome, iso_at, utc_now

from tests.conftest import (
    RES_CODE,
    TERMINAL_JOB_STATES,
    candle_count,
    coverage_windows,
    job_row,
    make_chunk_task,
    make_job,
    register_underlying,
    seed_catalog,
    task_rows,
    wait_until,
)
from tests.fake_fyers import FakeFyers, always, server_error

EXPIRY = date(2025, 3, 27)
PLANNING_DAY = date(2025, 6, 1)
# Short enough that the whole life sits inside one chunk, so one contract is exactly one request
# and the backward walk adds nothing to the count.
LIFE_START = date(2025, 3, 3)


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


def _populated(engine, store, *, strikes=(22800, 22900, 23000, 23100, 23200, 23300)):
    register_underlying(engine, option_life_days=60)
    contracts = seed_catalog(store, expiry=EXPIRY, strikes=strikes, rights=("CE", "PE"))
    fake = FakeFyers()
    for _cid, symbol in contracts:
        fake.give_life(symbol, first_day=LIFE_START, last_day=EXPIRY)
    return contracts, fake


class TestEveryTaskRunsExactlyOnce:
    async def test_a_pool_of_workers_spends_one_request_for_each_task_and_no_more(
        self, registry_engine, catalog_store, make_world
    ):
        """Twelve tasks, four workers, twelve requests.

        A double lease is invisible in the data, because the second write replaces the first over
        the same window. It is visible here, as a thirteenth request.
        """
        contracts, fake = _populated(registry_engine, catalog_store)

        async with make_world(fake, clock=PLANNING_DAY, worker_count=4) as world:
            accepted = await world.jobs.create(_order())
            job_id = accepted.job_id
            await wait_until(
                lambda: job_row(registry_engine, job_id)["status"] in TERMINAL_JOB_STATES
            )

        assert accepted.total_tasks == len(contracts)
        assert fake.request_count == len(contracts), fake.windows
        assert job_row(registry_engine, job_id)["status"] == "completed"

        states = [task["state"] for task in task_rows(registry_engine, job_id)]
        assert states == ["done"] * len(contracts)

        # One window per contract, and every contract holds the bars it was given.
        for contract_id, symbol in contracts:
            ledger = coverage_windows(catalog_store, contract_id)
            assert len(ledger) == 1, ledger
            assert ledger[0][3] == candle_count(catalog_store, contract_id)
            assert ledger[0][3] == len(
                fake.expected_bars(symbol, ledger[0][0], ledger[0][1])
            )

    async def test_the_job_counters_agree_with_the_task_rows(
        self, registry_engine, catalog_store, make_world
    ):
        """The progress bar's denominator and numerator come from two places, so they can drift."""
        contracts, fake = _populated(registry_engine, catalog_store, strikes=(22900, 23000))

        async with make_world(fake, clock=PLANNING_DAY, worker_count=2) as world:
            accepted = await world.jobs.create(_order())
            job_id = accepted.job_id
            await wait_until(
                lambda: job_row(registry_engine, job_id)["status"] in TERMINAL_JOB_STATES
            )

        rows = task_rows(registry_engine, job_id)
        job = job_row(registry_engine, job_id)
        assert job["total_tasks"] == len(rows)
        assert job["done_tasks"] == sum(1 for row in rows if row["state"] == "done")
        assert job["failed_tasks"] == 0
        # One governed request per task, counted on the job ledger as well as by the transport.
        assert job["requests_used"] == fake.request_count == len(rows)
        assert job["rows_written"] == candle_count(catalog_store)


class TestALostLease:
    async def test_a_reclaimed_task_runs_again_and_the_stale_ack_changes_nothing(
        self, registry_engine, catalog_store, make_task_harness
    ):
        """The classic double owner: one worker stalls, its lease expires, another takes over.

        The stale worker eventually finishes and acks. That ack has to do nothing, because the
        row it would describe has already been replaced by the second run's.
        """
        register_underlying(registry_engine, option_life_days=60)
        contracts = seed_catalog(catalog_store, expiry=EXPIRY)
        contract_id, symbol = contracts[0]
        fake = FakeFyers()
        fake.give_life(symbol, first_day=LIFE_START, last_day=EXPIRY)
        job = make_job(registry_engine)

        async with make_task_harness(fake) as harness:
            task_id = make_chunk_task(
                registry_engine,
                job,
                contract_id=contract_id,
                symbol=symbol,
                expiry_date=EXPIRY,
                range_from=date(2025, 2, 1),
                range_to=EXPIRY,
            )
            stalled = harness.queue.lease(owner="worker-stalled", limit=1)[0]
            # The stalled worker gets as far as committing its chunk, which is the state a stall
            # after the write and before the ack actually leaves behind.
            await harness.commit_only(stalled)
            rows_after_stall = candle_count(catalog_store, contract_id)
            assert rows_after_stall > 0

            # The lease ages out while the stalled worker is still holding it.
            with registry_engine.begin() as connection:
                connection.execute(
                    text("UPDATE task SET lease_expires_at = :past WHERE task_id = :t"),
                    {"past": iso_at(utc_now() - timedelta(minutes=5)), "t": task_id},
                )
            assert harness.queue.reclaim_expired_leases() == 1

            # A second worker picks it up and finishes it properly.
            await harness.run_one(
                harness.lease_specific(task_id, owner="worker-fresh"), owner="worker-fresh"
            )
            rows_after = candle_count(catalog_store, contract_id)
            assert rows_after == rows_after_stall, "the rerun did not converge on the same rows"

            # And now the stalled worker comes back with an ack nobody wants.
            applied = harness.queue.ack(
                stalled,
                TaskOutcome(state="done", row_count=999_999, rows_written=999_999),
                owner="worker-stalled",
            )

        assert applied is False, "a stale owner acked a task it no longer held"
        row = task_rows(registry_engine, job)[0]
        assert row["state"] == "done"
        assert row["row_count"] == rows_after != 999_999
        assert candle_count(catalog_store, contract_id) == rows_after
        assert len(coverage_windows(catalog_store, contract_id)) == 1
        # One request for the attempt that was lost and one for the run that finished. A lost
        # lease costs exactly one repeated request and nothing else.
        assert fake.request_count == 2


class TestARetryWaitsInTheTableNotInTheWorker:
    async def test_a_transient_failure_returns_the_worker_immediately(
        self, registry_engine, catalog_store, make_task_harness
    ):
        """A worker that sleeps out a backoff is a worker the pool has lost for that long.

        The wait belongs in `not_before`, which is a column, so the lease is free the instant the
        failure is recorded. The wall clock assertion is crude on purpose: the documented backoff
        for the first retry is one to three seconds, so anything under half a second proves the
        worker did not serve it.
        """
        register_underlying(registry_engine, option_life_days=60)
        contracts = seed_catalog(catalog_store, expiry=EXPIRY)
        contract_id, symbol = contracts[0]
        fake = FakeFyers()
        fake.give_life(symbol, first_day=LIFE_START, last_day=EXPIRY)
        fake.inject = always(server_error)
        job = make_job(registry_engine)

        async with make_task_harness(fake) as harness:
            task_id = make_chunk_task(
                registry_engine,
                job,
                contract_id=contract_id,
                symbol=symbol,
                expiry_date=EXPIRY,
                range_from=date(2025, 2, 1),
                range_to=EXPIRY,
                max_attempts=3,
            )
            started = time.monotonic()
            await harness.run_one()
            elapsed = time.monotonic() - started

        assert elapsed < 0.5, f"the worker held its lease for {elapsed:.2f} seconds"
        assert fake.request_count == 1

        row = task_rows(registry_engine, job)[0]
        assert row["state"] == "pending"
        assert row["attempt"] == 1
        assert row["lease_owner"] is None
        # The wait is in the table, and it is a real wait rather than a zero.
        assert row["not_before"] > iso_at(utc_now())

        # And until it elapses the task is not leasable, so nothing spins on it.
        assert harness.queue.lease(owner="worker-w28", limit=1) == []
        assert candle_count(catalog_store, contract_id) == 0

    async def test_the_last_attempt_fails_the_task_and_settles_the_job(
        self, registry_engine, catalog_store, make_world
    ):
        """A job whose tasks exhaust their attempts finishes, rather than hanging as running."""
        register_underlying(registry_engine, option_life_days=60)
        contracts = seed_catalog(catalog_store, expiry=EXPIRY)
        _contract_id, symbol = contracts[0]
        fake = FakeFyers()
        fake.give_life(symbol, first_day=LIFE_START, last_day=EXPIRY)
        fake.inject = always(server_error)

        async with make_world(fake, clock=PLANNING_DAY, worker_count=1) as world:
            accepted = await world.jobs.create(_order(option_types=["CE"]))
            job_id = accepted.job_id
            # One attempt only, so the failure settles without waiting out a backoff.
            with registry_engine.begin() as connection:
                connection.execute(
                    text("UPDATE task SET max_attempts = 1 WHERE job_id = :j"), {"j": job_id}
                )
            world.supervisor.notify(job_id)
            await wait_until(
                lambda: job_row(registry_engine, job_id)["status"] in TERMINAL_JOB_STATES
            )

        assert job_row(registry_engine, job_id)["status"] == "completed_with_errors"
        rows = task_rows(registry_engine, job_id)
        assert [row["state"] for row in rows] == ["failed"]
        assert rows[0]["attempt"] == 1
        # One attempt, one request. A failing task must not be able to spin.
        assert fake.request_count == 1
        assert candle_count(catalog_store) == 0


class TestACancelledJobSpendsNothing:
    async def test_cancelling_a_queued_job_leaves_every_task_cancelled_and_nothing_sent(
        self, registry_engine, catalog_store, make_world
    ):
        """The pipeline is held shut while the job is created and cancelled, so the race is gone.

        Pausing the governor rather than racing the pool is what makes this deterministic: the
        property under test is that a cancelled row is never leased, not how fast a cancel travels.
        """
        contracts, fake = _populated(registry_engine, catalog_store)

        async with make_world(fake, clock=PLANNING_DAY, worker_count=2) as world:
            await world.governor.pause_user()
            # Let the supervisor's mode poll notice and shut the run gate.
            await wait_until(lambda: not world.supervisor.run_gate.is_set(), timeout=5)

            accepted = await world.jobs.create(_order())
            job_id = accepted.job_id
            assert accepted.total_tasks == len(contracts)
            assert fake.request_count == 0

            await world.jobs.cancel(job_id)
            await world.governor.resume(by="test")
            await wait_until(
                lambda: job_row(registry_engine, job_id)["status"] in TERMINAL_JOB_STATES,
                timeout=10,
            )
            # Give an ungated pool a generous window to spend the cancelled work.
            await asyncio.sleep(0.2)

        assert job_row(registry_engine, job_id)["status"] == "cancelled"
        assert fake.request_count == 0, fake.windows
        assert {task["state"] for task in task_rows(registry_engine, job_id)} == {"cancelled"}
        assert candle_count(catalog_store) == 0
