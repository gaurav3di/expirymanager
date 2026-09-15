"""Running the same work twice converges on the truth instead of accumulating.

The pipeline replays. A lease expires and is reclaimed, a crash lands between the DuckDB commit and
the SQLite ack, a user forces a refresh, a nightly sweep re-covers a window whose ledger row was
lost. Every one of those runs a chunk that has already run, and the product's answer to all of them
is the same: the write is a delete over the exact half-open IST window of the request followed by an
insert of what came back, so a replay costs one request and changes nothing else.

Three things can go wrong with that and none of them raises:

1. The delete window and the insert window disagree, so a replay adds rows instead of replacing
   them. The row count grows and every count, every chart and every export is wrong by a multiple.
2. A shorter, corrected response is written over a longer one and the surplus rows survive, so the
   store holds bars the vendor no longer says exist.
3. The replay re-runs the backward walk and plans the next chunk a second time, so one crash turns
   into one extra governed request per chunk for the rest of the backfill.

All three are asserted here against row counts and task rows, never against a call.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

from sqlalchemy import text

from tests.conftest import (
    RES_CODE,
    RES_ID,
    candle_count,
    candle_stamps,
    coverage_windows,
    job_row,
    make_chunk_task,
    make_job,
    register_underlying,
    seed_catalog,
    task_rows,
)
from tests.fake_fyers import FakeFyers

EXPIRY = date(2025, 3, 27)
LIFE_START = date(2024, 11, 1)
# Deliberately shorter than a full request so the window is easy to reason about, and deliberately
# starting on a day the contract already traded so the backward walk has a reason to plan more.
WINDOW_FROM = date(2025, 1, 1)


def _world(engine, store):
    register_underlying(engine, option_life_days=200)
    contracts = seed_catalog(store, expiry=EXPIRY)
    return contracts[0]


def _fake(symbol: str) -> FakeFyers:
    fake = FakeFyers()
    fake.give_life(symbol, first_day=LIFE_START, last_day=EXPIRY)
    return fake


def _task(engine, job_id: str, contract_id: int, symbol: str, *, seq: int = 0) -> int:
    return make_chunk_task(
        engine,
        job_id,
        seq=seq,
        contract_id=contract_id,
        symbol=symbol,
        expiry_date=EXPIRY,
        range_from=WINDOW_FROM,
        range_to=EXPIRY,
    )


def _followups(engine, job_id: str) -> list[dict]:
    return [row for row in task_rows(engine, job_id) if row["parent_task_id"] is not None]


class TestReplayingAChunk:
    async def test_running_the_same_window_twice_leaves_the_row_count_unchanged(
        self, registry_engine, catalog_store, make_task_harness
    ):
        contract_id, symbol = _world(registry_engine, catalog_store)
        fake = _fake(symbol)
        job = make_job(registry_engine)

        async with make_task_harness(fake) as harness:
            _task(registry_engine, job, contract_id, symbol, seq=0)
            await harness.run_one()
            first_rows = candle_count(catalog_store, contract_id)
            first_stamps = candle_stamps(catalog_store, contract_id)

            # A second identical task. Same contract, same resolution, same window. Named
            # explicitly, because the backward walk has by now queued an older window too.
            replay_id = _task(registry_engine, job, contract_id, symbol, seq=99)
            await harness.run_one(harness.lease_specific(replay_id))
            assert candle_count(catalog_store, contract_id) == first_rows

            # And once more with the payload hash short circuit turned off, which is what the
            # force refresh button does. That path deletes and re-inserts for real rather than
            # recognising an unchanged payload, so it is the one that exercises the delete window
            # itself. Without it a delete window that is a day too narrow passes this test.
            forced_id = make_chunk_task(
                registry_engine,
                job,
                seq=100,
                contract_id=contract_id,
                symbol=symbol,
                expiry_date=EXPIRY,
                range_from=WINDOW_FROM,
                range_to=EXPIRY,
                params={"force_refresh": True},
            )
            await harness.run_one(harness.lease_specific(forced_id))

        assert first_rows > 0
        assert candle_count(catalog_store, contract_id) == first_rows
        assert candle_stamps(catalog_store, contract_id) == first_stamps
        # One window, one ledger row, however many times it ran.
        ledger = [row for row in coverage_windows(catalog_store, contract_id)
                  if row[0] == WINDOW_FROM]
        assert len(ledger) == 1
        assert ledger[0][3] == first_rows

    async def test_a_shorter_correction_removes_the_surplus_rows(
        self, registry_engine, catalog_store, make_task_harness
    ):
        """A window that came back long once and short later must end up short.

        The vendor does correct itself. The write is a delete over the request's own window rather
        than an upsert per row precisely so a bar that no longer exists stops existing here too.
        An upsert would leave it behind forever, and nothing would ever report it.
        """
        contract_id, symbol = _world(registry_engine, catalog_store)
        fake = _fake(symbol)
        # Two bars the contract never really traded, inside the window, at minutes the model does
        # not produce. They are removable only by a delete over the window.
        surplus = [
            datetime(2025, 2, 10, 10, 30),
            datetime(2025, 2, 11, 14, 5),
        ]
        fake.surplus_bars[symbol] = list(surplus)
        job = make_job(registry_engine)

        async with make_task_harness(fake) as harness:
            _task(registry_engine, job, contract_id, symbol, seq=0)
            await harness.run_one()
            long_rows = candle_count(catalog_store, contract_id)
            assert set(surplus) <= set(candle_stamps(catalog_store, contract_id))

            # The correction: the same window, answered without the two extra bars.
            fake.surplus_bars[symbol] = []
            replay_id = _task(registry_engine, job, contract_id, symbol, seq=99)
            await harness.run_one(harness.lease_specific(replay_id))

        stamps = candle_stamps(catalog_store, contract_id)
        assert candle_count(catalog_store, contract_id) == long_rows - len(surplus)
        assert not set(surplus) & set(stamps), "a bar the vendor withdrew survived the correction"
        assert stamps == sorted(fake.expected_bars(symbol, WINDOW_FROM, EXPIRY))
        # The ledger's own count was corrected too, so a reconciliation would not report a gap.
        ledger = [row for row in coverage_windows(catalog_store, contract_id)
                  if row[0] == WINDOW_FROM]
        assert ledger[0][3] == len(stamps)

    async def test_a_crash_between_the_commit_and_the_ack_replays_without_duplicating(
        self, registry_engine, catalog_store, make_task_harness
    ):
        """Commit first, ack second, so a crash in between costs one request and nothing else.

        The crash is real rather than described: the handler runs to completion and the queue is
        never told, which leaves exactly the rows and exactly the task state a killed process
        would leave. The task is then reclaimed and run again, the way the supervisor's reclaim
        loop does it.
        """
        contract_id, symbol = _world(registry_engine, catalog_store)
        fake = _fake(symbol)
        job = make_job(registry_engine)

        async with make_task_harness(fake) as harness:
            task_id = _task(registry_engine, job, contract_id, symbol, seq=0)
            crashed, _outcome = await harness.commit_only()
            assert crashed.task_id == task_id

            rows_after_crash = candle_count(catalog_store, contract_id)
            stamps_after_crash = candle_stamps(catalog_store, contract_id)
            assert rows_after_crash > 0, "the commit did not happen before the ack"

            # The task is still owed: nothing acked it, so it is not done.
            with registry_engine.connect() as connection:
                state = connection.execute(
                    text("SELECT state FROM task WHERE task_id = :t"), {"t": task_id}
                ).scalar_one()
            assert state != "done"

            # What the reclaim loop does: the lease goes back and the task runs again.
            harness.queue.release([task_id], owner="worker-w28")
            await harness.run_one()

        assert candle_count(catalog_store, contract_id) == rows_after_crash
        assert candle_stamps(catalog_store, contract_id) == stamps_after_crash
        assert len([row for row in coverage_windows(catalog_store, contract_id)
                    if row[0] == WINDOW_FROM]) == 1
        with registry_engine.connect() as connection:
            assert connection.execute(
                text("SELECT state FROM task WHERE task_id = :t"), {"t": task_id}
            ).scalar_one() == "done"

    async def test_a_replayed_chunk_does_not_plan_its_successor_twice(
        self, registry_engine, catalog_store, make_task_harness
    ):
        """One crash must not cost one extra governed request for the rest of the backfill.

        The backward walk runs after the commit and before the ack, so it runs again on a replay.
        The follow-up insert is conditional on no identical row existing for the job, and this is
        the assertion that the condition is real: two task rows, not three, and a job whose own
        totals were bumped once.
        """
        contract_id, symbol = _world(registry_engine, catalog_store)
        fake = _fake(symbol)
        job = make_job(registry_engine)

        async with make_task_harness(fake) as harness:
            task_id = _task(registry_engine, job, contract_id, symbol, seq=0)
            await harness.commit_only()

            planned_once = _followups(registry_engine, job)
            assert len(planned_once) == 1, "the backward walk planned nothing to replay"

            harness.queue.release([task_id], owner="worker-w28")
            await harness.run_one()

        planned_twice = _followups(registry_engine, job)
        assert len(planned_twice) == 1, planned_twice
        assert planned_twice[0]["range_from"] == planned_once[0]["range_from"]
        assert planned_twice[0]["range_to"] == planned_once[0]["range_to"]
        # The denominator the progress bar divides by was bumped once, not twice.
        assert job_row(registry_engine, job)["total_tasks"] == 1
        assert job_row(registry_engine, job)["est_requests"] == 1
        # And the follow-up meets the chunk it came from, exactly.
        assert (
            date.fromisoformat(planned_twice[0]["range_to"]) + timedelta(days=1) == WINDOW_FROM
        )


class TestConvergenceOfTheEmptyCase:
    async def test_a_no_data_window_is_recorded_so_it_is_never_requested_again(
        self, registry_engine, catalog_store, make_task_harness
    ):
        """no_data is a success with a durable result, not a failure and not a silence.

        A window with no coverage row is a window this product will pay to request again, so an
        empty answer has to leave a row behind. It also ends the backward walk: a contract's life
        is contiguous, so a window with no trades means everything older is dead too.
        """
        contract_id, symbol = _world(registry_engine, catalog_store)
        fake = FakeFyers()
        # The contract traded, but not in the window this task asks for.
        fake.give_life(symbol, first_day=date(2025, 2, 1), last_day=EXPIRY)
        job = make_job(registry_engine)

        async with make_task_harness(fake) as harness:
            make_chunk_task(
                registry_engine,
                job,
                contract_id=contract_id,
                symbol=symbol,
                expiry_date=EXPIRY,
                range_from=date(2024, 12, 1),
                range_to=date(2025, 1, 15),
            )
            await harness.run_one()

        ledger = coverage_windows(catalog_store, contract_id)
        assert len(ledger) == 1
        assert ledger[0][:3] == (date(2024, 12, 1), date(2025, 1, 15), "empty")
        assert ledger[0][3] == 0
        assert candle_count(catalog_store, contract_id, RES_ID) == 0
        # Nothing older was planned off the back of an empty window.
        assert _followups(registry_engine, job) == []
        with registry_engine.connect() as connection:
            assert connection.execute(
                text("SELECT state FROM task WHERE job_id = :j"), {"j": job}
            ).scalar_one() == "empty"

    async def test_an_empty_window_replays_to_the_same_single_ledger_row(
        self, registry_engine, catalog_store, make_task_harness
    ):
        contract_id, symbol = _world(registry_engine, catalog_store)
        fake = FakeFyers()
        fake.give_life(symbol, first_day=date(2025, 2, 1), last_day=EXPIRY)
        job = make_job(registry_engine)

        async with make_task_harness(fake) as harness:
            for seq in (0, 1):
                make_chunk_task(
                    registry_engine,
                    job,
                    seq=seq,
                    contract_id=contract_id,
                    symbol=symbol,
                    expiry_date=EXPIRY,
                    range_from=date(2024, 12, 1),
                    range_to=date(2025, 1, 15),
                    resolution=RES_CODE,
                )
            await harness.run_one()
            await harness.run_one(harness.lease_one())

        assert len(coverage_windows(catalog_store, contract_id)) == 1
        assert candle_count(catalog_store, contract_id) == 0
        assert fake.count_for("expired-historical-data") == 2
