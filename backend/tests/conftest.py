"""Shared machinery for the W28 suite: real databases, a real pipeline, a modelled transport.

Nothing in this file is a mock. The registry is a real migrated SQLite file, the catalog is a real
DuckDB file in a temporary directory, the planner, the job service, the lease queue, the supervisor,
the worker pool, the handlers and the single writer are all the production classes, and the only
thing replaced is the socket, by `tests/fake_fyers.FakeFyers`.

That choice is the point of the item. Four of the five defects this project shipped with a green
suite over them would have passed a test built on mocks: an upsert nobody called, a rebuild nobody
called, a getattr with a None default that skipped silently, and a symbol master field that was
never populated. Every one of them is visible in a table and invisible in a call log, so this
harness gives tests tables to look at.

Every fixture here is additive and none is autouse, so no test outside this item changes behaviour
because this file exists. The database fixtures are deliberately named for what they hold rather
than `engine` and `store`, so that a module defining its own fixture of either name is unambiguous
rather than merely shadowing.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterable, Sequence
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text

from expirymanager.brokers.fyers.client import AuthContext, FyersClient
from expirymanager.brokers.fyers.throttle import FyersGovernor, InMemoryBudgetStore
from expirymanager.db import migrate as migrate_module
from expirymanager.db import sqlite as sqlite_module
from expirymanager.db.duck import DuckStore
from expirymanager.db.reader import DuckReader
from expirymanager.pipeline import worker as worker_module
from expirymanager.pipeline.events import EventBus
from expirymanager.pipeline.handlers import candle_chunk as candle_handlers
from expirymanager.pipeline.jobs import JobService
from expirymanager.pipeline.planner import Planner
from expirymanager.pipeline.queue import LeaseQueue
from expirymanager.pipeline.supervisor import PipelineSupervisor

# Synthetic throughout. Nothing here is or resembles a real credential.
SYNTHETIC_APP_ID = "TESTAPP01-100"
SYNTHETIC_ACCESS_TOKEN = "header.payload.signature"
SYNTHETIC_FINGERPRINT = "0f0f0f0f0f0f0f0f"

# The vocabulary LeaseQueue.terminal_status() writes. A job never reaches a word outside it.
TERMINAL_JOB_STATES = ("completed", "completed_with_errors", "cancelled", "failed")

RES_CODE = "1"
RES_ID = 2


# ---------------------------------------------------------------------------
# Databases
# ---------------------------------------------------------------------------


@pytest.fixture
def registry_engine(tmp_path: Path):
    """A real migrated SQLite registry in a temporary directory."""
    engine = sqlite_module.create_engine(tmp_path / "data" / "config.sqlite3")
    migrate_module.migrate(engine)
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture
def catalog_store(tmp_path: Path):
    """A real DuckDB catalog in a temporary directory.

    Never the developer's own market.duckdb: a second opener of that file fails, and a test that
    wrote into it would corrupt real downloaded history.
    """
    store = DuckStore(tmp_path / "market.duckdb", app_version="0.0.0-test")
    store.open()
    try:
        yield store
    finally:
        store.close()


@pytest.fixture
def catalog_reader(catalog_store) -> DuckReader:
    return DuckReader(catalog_store)


@pytest.fixture
def installed_handlers():
    """The real handler registry, installed and torn down around one test.

    The registry is process wide, so a test that relied on some other module having imported the
    handlers first would pass or fail on collection order.
    """
    worker_module.clear_registry()
    candle_handlers.clear_caches()
    candle_handlers.install_all()
    yield
    worker_module.clear_registry()
    candle_handlers.clear_caches()


# ---------------------------------------------------------------------------
# Seeding: the local state a plan is priced from
# ---------------------------------------------------------------------------


def register_underlying(
    engine,
    *,
    underlying_id: int = 1,
    fyers_symbol: str = "NSE:NIFTY50-INDEX",
    root: str = "NIFTY",
    exchange: str = "NSE",
    data_from: date = date(2022, 1, 3),
    option_life_days: int = 200,
    future_life_days: int = 400,
    spot_contract_id: int = 1,
    is_active: bool = True,
) -> int:
    """Write the SQLite registry row the planner and the backward walk both read.

    INSERT OR REPLACE rather than INSERT, because migration 0004 seeds four builtin underlyings
    and a test that silently ran against one of those would be asserting somebody else's life
    window rather than its own.
    """
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT OR REPLACE INTO underlying_registry (underlying_id, fyers_symbol, root,"
                " exchange, segment, instrument_kind, display_name, data_from,"
                " default_resolutions, include_oi, option_life_days, future_life_days,"
                " spot_contract_id, is_active, created_at, updated_at)"
                " VALUES (:id, :symbol, :root, :exchange, 'CM', 'INDEX', :display, :data_from,"
                " '[\"1\"]', 1, :option_life, :future_life, :spot, :active, :now, :now)"
            ),
            {
                "id": underlying_id,
                "symbol": fyers_symbol,
                "root": root,
                "exchange": exchange,
                "display": root.title(),
                "data_from": data_from.isoformat(),
                "option_life": option_life_days,
                "future_life": future_life_days,
                "spot": spot_contract_id,
                "active": 1 if is_active else 0,
                "now": datetime.now(UTC).isoformat(),
            },
        )
    return underlying_id


def clear_holidays(engine, exchange: str = "NSE") -> None:
    with engine.begin() as connection:
        connection.execute(
            text("DELETE FROM market_holiday WHERE exchange = :exchange"),
            {"exchange": exchange},
        )


def add_holidays(engine, days: Iterable[date], exchange: str = "NSE") -> None:
    with engine.begin() as connection:
        for day in days:
            connection.execute(
                text(
                    "INSERT OR REPLACE INTO market_holiday (exchange, holiday_date, source)"
                    " VALUES (:exchange, :day, 'test')"
                ),
                {"exchange": exchange, "day": day.isoformat()},
            )


def seed_catalog(
    store,
    *,
    underlying_id: int = 1,
    root: str = "NIFTY",
    exchange: str = "NSE",
    expiry: date,
    strikes: Sequence[int] = (23000,),
    rights: Sequence[str] = ("CE",),
    data_from: date = date(2022, 1, 3),
    spot_contract_id: int = 1,
    first_contract_id: int = 5000,
) -> list[tuple[int, str]]:
    """Mirror the underlying and write the expiry and contract rows a plan reads.

    Returns (contract_id, fyers_symbol) pairs, because a test then gives each of those symbols a
    life on the fake transport and asserts against the ids in DuckDB.
    """
    exchange_code = 10 if exchange == "NSE" else 12
    tag = expiry.strftime("%y%b").upper()
    expiry_id = underlying_id * 1000 + expiry.month * 32 + expiry.day
    contracts: list[tuple[int, str, float, str]] = []
    contract_id = first_contract_id
    for strike in strikes:
        for right in rights:
            symbol = f"{exchange}:{root}{tag}{strike}{right}"
            contracts.append((contract_id, symbol, float(strike), right))
            contract_id += 1

    cur = store.cursor()
    try:
        cur.execute(
            "INSERT INTO dim_underlying (underlying_id, fyers_symbol, root, exchange,"
            " exchange_code, segment, segment_code, instrument_kind, display_name,"
            " spot_contract_id, data_from, is_active, synced_at)"
            " VALUES (?, ?, ?, ?, ?, 'CM', 10, 'INDEX', ?, ?, ?, TRUE, now())"
            " ON CONFLICT (underlying_id) DO NOTHING",
            [
                underlying_id,
                f"{exchange}:{root}50-INDEX",
                root,
                exchange,
                exchange_code,
                root.title(),
                spot_contract_id,
                data_from,
            ],
        )
        cur.execute(
            "INSERT INTO dim_expiry (expiry_id, underlying_id, expiry_date, has_options,"
            " contract_count, options_count, contract_id_lo, contract_id_hi, discovered_at,"
            " contracts_discovered_at)"
            " VALUES (?, ?, ?, TRUE, ?, ?, ?, ?, now(), now())"
            " ON CONFLICT (expiry_id) DO NOTHING",
            [
                expiry_id,
                underlying_id,
                expiry,
                len(contracts),
                len(contracts),
                contracts[0][0],
                contracts[-1][0],
            ],
        )
        for cid, symbol, strike, right in contracts:
            cur.execute(
                "INSERT INTO dim_contract (contract_id, underlying_id, expiry_id, fyers_symbol,"
                " kind, instrument_class, exchange, exchange_code, segment, segment_code, root,"
                " expiry_date, strike, option_type, source_endpoint, parse_method,"
                " parse_confidence, first_seen_at, last_seen_at)"
                " VALUES (?, ?, ?, ?, 'OPT', 'OPTIDX', ?, ?, 'FO', 11, ?, ?, ?, ?,"
                " 'expired-symbols', 'regex', 'high', now(), now())"
                " ON CONFLICT (contract_id) DO NOTHING",
                [
                    cid,
                    underlying_id,
                    expiry_id,
                    symbol,
                    exchange,
                    exchange_code,
                    root,
                    expiry,
                    strike,
                    right,
                ],
            )
    finally:
        cur.close()
    return [(cid, symbol) for cid, symbol, _strike, _right in contracts]


# ---------------------------------------------------------------------------
# Reading the result back
# ---------------------------------------------------------------------------


def candle_count(store, contract_id: int | None = None, res_id: int = RES_ID) -> int:
    cur = store.cursor()
    try:
        if contract_id is None:
            return int(cur.execute("SELECT count(*) FROM candles").fetchone()[0])
        return int(
            cur.execute(
                "SELECT count(*) FROM candles WHERE contract_id = ? AND res_id = ?",
                [contract_id, res_id],
            ).fetchone()[0]
        )
    finally:
        cur.close()


def candle_stamps(store, contract_id: int, res_id: int = RES_ID) -> list[datetime]:
    cur = store.cursor()
    try:
        return [
            row[0]
            for row in cur.execute(
                "SELECT ts FROM candles WHERE contract_id = ? AND res_id = ? ORDER BY ts",
                [contract_id, res_id],
            ).fetchall()
        ]
    finally:
        cur.close()


def coverage_windows(store, contract_id: int, res_id: int = RES_ID) -> list[tuple[date, date, str, int]]:
    cur = store.cursor()
    try:
        return [
            (row[0], row[1], row[2], int(row[3]))
            for row in cur.execute(
                "SELECT range_from, range_to, status, row_count FROM candle_coverage"
                " WHERE contract_id = ? AND res_id = ? ORDER BY range_from",
                [contract_id, res_id],
            ).fetchall()
        ]
    finally:
        cur.close()


def sealed_at(store, contract_id: int):
    cur = store.cursor()
    try:
        return cur.execute(
            "SELECT sealed_at FROM dim_contract WHERE contract_id = ?", [contract_id]
        ).fetchone()[0]
    finally:
        cur.close()


def task_rows(engine, job_id: str) -> list[dict[str, Any]]:
    with engine.connect() as connection:
        return [
            dict(row)
            for row in connection.execute(
                text("SELECT * FROM task WHERE job_id = :job ORDER BY task_id"), {"job": job_id}
            ).mappings()
        ]


def job_row(engine, job_id: str) -> dict[str, Any]:
    with engine.connect() as connection:
        return dict(
            connection.execute(
                text("SELECT * FROM job WHERE job_id = :job"), {"job": job_id}
            ).mappings().one()
        )


async def wait_until(predicate, *, timeout: float = 15.0, interval: float = 0.01) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return
        await asyncio.sleep(interval)
    raise AssertionError("the condition did not hold within the timeout")


# ---------------------------------------------------------------------------
# The collaborators that are not under test here
# ---------------------------------------------------------------------------


class StubTokens:
    """The auth context supplier a FyersClient needs. Synthetic token, one generation."""

    def __init__(self) -> None:
        self.generation = 1

    async def auth_context(self) -> AuthContext:
        return AuthContext(
            app_id=SYNTHETIC_APP_ID,
            access_token=SYNTHETIC_ACCESS_TOKEN,
            generation=self.generation,
        )


class StubBroker:
    """The token broker surface the supervisor, the worker and the handlers reach for.

    The real TokenBroker's single flight guard is under test in `test_tokens.py` against the real
    class. What this one contributes to a pipeline test is a gate that can be watched and a count
    of how many times it was closed.
    """

    class _Record:
        fingerprint = SYNTHETIC_FINGERPRINT

    def __init__(self) -> None:
        self.auth_gate = asyncio.Event()
        self.auth_gate.set()
        self.generation = 1
        self._parked_generation: int | None = None
        self.record = StubBroker._Record()
        self.parked_count = 0

    def has_valid_token(self) -> bool:
        return self.auth_gate.is_set()

    async def on_auth_error(self, generation: int, *, reason: str = "") -> bool:
        if generation < self.generation:
            return False
        if self._parked_generation is not None and generation <= self._parked_generation:
            return False
        self._parked_generation = generation
        self.auth_gate.clear()
        self.parked_count += 1
        return True

    def log_in_again(self) -> None:
        """What a successful morning login does to the gate."""
        self.generation += 1
        self._parked_generation = None
        self.auth_gate.set()


class StubSettings:
    def __init__(self, **values: Any) -> None:
        self._values: dict[str, Any] = {
            "budget_reserve_fraction": 0.70,
            "throttle_per_minute": 170,
        }
        self._values.update(values)

    def get_int(self, key: str) -> int:
        return int(self._values[key])

    def get_float(self, key: str) -> float:
        return float(self._values[key])

    def get_str(self, key: str) -> str:
        return str(self._values[key])


class Services:
    """The AppState shaped bag a handler resolves its five dependencies from."""

    def __init__(self, *, client, store, engine, governor, settings=None) -> None:
        self.fyers_client = client
        self.duck_writer = store.writer
        self.duck_reader = DuckReader(store)
        self.engine = engine
        self.settings = settings or StubSettings()
        self.paths = None
        self.token_broker = StubBroker()
        self.governor = governor


async def run_inline(fn, /, *args: Any, **kwargs: Any) -> Any:
    """Run the queue's blocking statements on this thread, so a test is deterministic."""
    return fn(*args, **kwargs)


# ---------------------------------------------------------------------------
# The whole pipeline, wired the way the lifespan wires it
# ---------------------------------------------------------------------------


class PipelineWorld:
    """Planner, job service, queue, supervisor, workers, handlers, writer and a fake socket.

    The governor is the real FyersGovernor with the rate windows opened up, because a test that
    waited on the production 8 per second would spend a minute of wall clock proving something
    `test_throttle.py` proves in milliseconds. The daily counting is still real, so a test can
    cross-check the transport's request count against the budget the governor thinks it spent,
    which is the assertion that catches a request leaving outside the governed path.
    """

    def __init__(
        self,
        engine,
        store,
        fake,
        *,
        worker_count: int = 2,
        clock: date | None = None,
        daily_budget: int = 100_000,
    ) -> None:
        self.engine = engine
        self.store = store
        self.fake = fake
        self.governor = FyersGovernor(
            budget_store=InMemoryBudgetStore(),
            per_second=10_000,
            per_minute=100_000,
            in_flight=4,
            daily_budget=daily_budget,
            plan="standard",
        )
        self.client = FyersClient(
            tokens=StubTokens(), governor=self.governor, transport=fake.transport
        )
        self.services = Services(
            client=self.client, store=store, engine=engine, governor=self.governor
        )
        self.broker = self.services.token_broker
        self.reader = self.services.duck_reader
        self.bus = EventBus()
        self.queue = LeaseQueue(engine)
        self.supervisor = PipelineSupervisor(
            engine=engine,
            settings=self.services.settings,
            governor=self.governor,
            token_broker=self.broker,
            bus=self.bus,
            services=self.services,
            queue=self.queue,
            worker_count=worker_count,
            poll_interval=0.01,
            progress_interval=0.01,
            reclaim_interval=0.05,
            mode_poll_interval=0.01,
            to_thread=run_inline,
            recover_on_start=False,
            auto_reclaim=False,
        )
        self._job_seq = 0
        self.planner = Planner(
            reader=self.reader,
            engine=engine,
            settings=self.services.settings,
            governor=self.governor,
            clock=lambda: datetime.combine(clock, datetime.min.time())
            if clock is not None
            else datetime.now(),
        )
        self.jobs = JobService(
            engine=engine,
            planner=self.planner,
            supervisor=self.supervisor,
            governor=self.governor,
            id_factory=self._next_job_id,
        )

    def _next_job_id(self) -> str:
        self._job_seq += 1
        return f"job-{self._job_seq}"

    async def __aenter__(self) -> "PipelineWorld":
        # The single writer is an asyncio task, so it can only start inside the loop the test
        # runs on. The fixture opened the file; this starts the queue that drains it.
        await self.store.writer.start()
        await self.supervisor.start()
        return self

    async def __aexit__(self, *_exc: Any) -> bool:
        await self.supervisor.stop()
        await self.store.writer.stop()
        await self.client.aclose()
        return False

    async def run_to_completion(self, request, *, timeout: float = 20.0) -> str:
        """Commit one download and wait for the job to reach a terminal state."""
        accepted = await self.jobs.create(request)
        await wait_until(
            lambda: job_row(self.engine, accepted.job_id)["status"] in TERMINAL_JOB_STATES,
            timeout=timeout,
        )
        return accepted.job_id

    async def wait_for_status(self, job_id: str, status: str, *, timeout: float = 15.0) -> None:
        await wait_until(
            lambda: job_row(self.engine, job_id)["status"] == status, timeout=timeout
        )


@pytest.fixture
def make_world(registry_engine, catalog_store, installed_handlers):
    """A factory, so a test can choose the worker count and the planning date."""

    def factory(fake, **kwargs: Any) -> PipelineWorld:
        return PipelineWorld(registry_engine, catalog_store, fake, **kwargs)

    return factory


# ---------------------------------------------------------------------------
# One task at a time, with the pool taken out of the picture
# ---------------------------------------------------------------------------


class StubSupervisor:
    """The four calls a worker makes on its supervisor, recorded rather than acted on.

    Used where the property under test is about one task's effect on the tables, so a running pool
    would only add a race to the assertion.
    """

    def __init__(self) -> None:
        self.auth_failures: list[tuple[int, str, bool]] = []
        self.rate_limits: list[str] = []
        self.settled: list[str] = []
        self.gate_open = True
        self.generation = 1

    def gates_open(self) -> bool:
        return self.gate_open

    def token_generation(self) -> int:
        return self.generation

    async def on_auth_failure(self, generation: int, *, reason: str = "", fatal: bool = False):
        self.auth_failures.append((generation, reason, fatal))

    async def on_rate_limited(self, *, reason: str = "") -> None:
        self.rate_limits.append(reason)

    async def on_task_settled(self, job_id: str) -> None:
        self.settled.append(job_id)


class _StubDispatcher:
    def __init__(self, owner: str = "worker-w28") -> None:
        self.owner = owner

    def task_done(self) -> None:
        return None


class TaskHarness:
    """The handler stack with no pool: a real client, real services, a real queue, one worker.

    `run_one` drives a task exactly the way production does, through the worker, the registry and
    the queue transition. `commit_only` stops one step earlier, after the handler has made its
    write durable and before the queue is told, which is the state a crash leaves behind.
    """

    def __init__(self, engine, store, fake) -> None:
        from expirymanager.pipeline.worker import HandlerContext, Worker

        self.engine = engine
        self.store = store
        self.fake = fake
        self.governor = FyersGovernor(
            budget_store=InMemoryBudgetStore(),
            per_second=10_000,
            per_minute=100_000,
            in_flight=4,
            daily_budget=100_000,
        )
        self.client = FyersClient(
            tokens=StubTokens(), governor=self.governor, transport=fake.transport
        )
        self.services = Services(
            client=self.client, store=store, engine=engine, governor=self.governor
        )
        self.queue = LeaseQueue(engine)
        self.supervisor = StubSupervisor()
        self._HandlerContext = HandlerContext
        self._Worker = Worker

    async def __aenter__(self) -> "TaskHarness":
        await self.store.writer.start()
        return self

    async def __aexit__(self, *_exc: Any) -> bool:
        await self.store.writer.stop()
        await self.client.aclose()
        return False

    def lease_one(self, owner: str = "worker-w28"):
        leased = self.queue.lease(owner=owner, limit=1)
        assert len(leased) == 1, f"expected one leasable task, got {len(leased)}"
        return leased[0]

    def lease_specific(self, task_id: int, *, owner: str = "worker-w28", limit: int = 20):
        """Claim one named task, giving every other lease straight back.

        The queue hands out work in (priority, job_id, seq) order and a backward walk inserts its
        follow-up into the same job, so a test that wants to replay one particular window has to
        say which one rather than take whatever is next.
        """
        skipped: list[int] = []
        try:
            for _ in range(limit):
                batch = self.queue.lease(owner=owner, limit=1)
                if not batch:
                    break
                if batch[0].task_id == task_id:
                    return batch[0]
                skipped.append(batch[0].task_id)
        finally:
            if skipped:
                self.queue.release(skipped, owner=owner)
        raise AssertionError(f"task {task_id} was not leasable")

    async def run_one(self, task=None, *, owner: str = "worker-w28"):
        """Lease if needed, then run one task through the worker and let the queue settle it."""
        task = task if task is not None else self.lease_one(owner)
        worker = self._Worker(
            "solo",
            supervisor=self.supervisor,
            dispatcher=_StubDispatcher(owner),
            queue=self.queue,
            services=self.services,
            to_thread=run_inline,
        )
        await worker._run_one(task)
        return task

    async def commit_only(self, task=None, *, owner: str = "worker-w28"):
        """Run the handler and stop before the ack, which is where a crash lands.

        The handler is the production one and the write is a real DuckDB transaction, so what is
        on disk afterwards is exactly what a killed process would have left behind.
        """
        from expirymanager.pipeline.worker import get_handler

        task = task if task is not None else self.lease_one(owner)
        handler = get_handler(task.kind)
        assert handler is not None, f"no handler is registered for {task.kind}"
        outcome = await handler(
            self._HandlerContext(
                task=task, services=self.services, supervisor=self.supervisor, generation=1
            )
        )
        return task, outcome


@pytest.fixture
def make_task_harness(registry_engine, catalog_store, installed_handlers):
    def factory(fake) -> TaskHarness:
        return TaskHarness(registry_engine, catalog_store, fake)

    return factory


def make_job(engine, job_id: str = "job-1", *, params: dict[str, Any] | None = None) -> str:
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO job (job_id, kind, status, params_json, priority, total_tasks,"
                " est_requests, created_at)"
                " VALUES (:job_id, 'candle_backfill', 'running', :params, 100, 0, 0, :now)"
            ),
            {
                "job_id": job_id,
                "params": json.dumps(params or {}, separators=(",", ":"), sort_keys=True),
                "now": datetime.now(UTC).isoformat(),
            },
        )
    return job_id


def make_chunk_task(
    engine,
    job_id: str,
    *,
    seq: int = 0,
    contract_id: int,
    symbol: str,
    expiry_date: date,
    range_from: date,
    range_to: date,
    resolution: str = RES_CODE,
    include_oi: bool = True,
    max_attempts: int = 3,
    params: dict[str, Any] | None = None,
) -> int:
    with engine.begin() as connection:
        return int(
            connection.execute(
                text(
                    "INSERT INTO task (job_id, seq, kind, state, priority, underlying_id,"
                    " contract_id, fyers_symbol, expiry_date, resolution, range_from, range_to,"
                    " include_oi, request_params_json, attempt, max_attempts, not_before,"
                    " created_at)"
                    " VALUES (:job_id, :seq, 'candle_chunk', 'pending', 100, 1, :contract_id,"
                    " :symbol, :expiry, :resolution, :range_from, :range_to, :include_oi,"
                    " :params, 0, :max_attempts, :now, :now) RETURNING task_id"
                ),
                {
                    "job_id": job_id,
                    "seq": seq,
                    "contract_id": contract_id,
                    "symbol": symbol,
                    "expiry": expiry_date.isoformat(),
                    "resolution": resolution,
                    "range_from": range_from.isoformat(),
                    "range_to": range_to.isoformat(),
                    "include_oi": 1 if include_oi else 0,
                    "params": json.dumps(params) if params else None,
                    "max_attempts": max_attempts,
                    "now": datetime.now(UTC).isoformat(),
                },
            ).scalar_one()
        )
