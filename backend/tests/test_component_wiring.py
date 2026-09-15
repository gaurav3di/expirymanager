"""The four registrations that turn a running application into a working one.

Every one of these components exposes an explicit install() rather than registering as an import
side effect, which is the right call: registering on import makes the installed set depend on
import order and changes the behaviour of any test that merely imports the module. The cost is
that something has to call them, and for a while nothing did.

That omission is invisible from outside. The application starts, every route answers, the health
check is green. What actually happens is that the three lifespan slots stay empty, the worker
handler registry stays empty, api.deps.get_supervisor raises its documented 503, no schedule fires,
and any leased task dies with "no handler is registered for task kind". A download appears to start
and then silently does nothing.

These tests exist so that failure is loud and immediate instead.
"""

from __future__ import annotations

import pathlib
import tempfile

import pytest

from expirymanager import lifespan
from expirymanager.app import create_app
from expirymanager.pipeline import worker

# Every kind the planner and the scheduler can write onto a task row. A handler missing here means
# jobs of that kind fail at lease time, in production, with no test having noticed.
EXPECTED_TASK_KINDS = {
    "candle_chunk",
    "chain_snapshot",
    "expiry_dates",
    "spot_chunk",
    "symbol_master",
    "underlying_symbols",
}


@pytest.fixture
def built_app():
    with tempfile.TemporaryDirectory() as directory:
        yield create_app(root=pathlib.Path(directory), serve_static=False)


def test_every_lifespan_slot_has_a_factory(built_app) -> None:
    registered = set(lifespan.registered_components())
    missing = set(lifespan.COMPONENT_SLOTS) - registered
    assert not missing, f"lifespan slots with no factory: {sorted(missing)}"


def test_the_worker_registry_holds_a_handler_for_every_task_kind(built_app) -> None:
    registry = worker.handler_registry()
    kinds = set(registry.kinds()) if hasattr(registry, "kinds") else set(registry)
    missing = EXPECTED_TASK_KINDS - kinds
    assert not missing, f"task kinds with no handler: {sorted(missing)}"


def test_building_the_app_twice_does_not_double_register(built_app) -> None:
    # create_app runs per process in production but repeatedly across a test session, and an
    # installer that appends rather than replaces would grow the registry every time.
    before = len(lifespan.registered_components())
    with tempfile.TemporaryDirectory() as directory:
        create_app(root=pathlib.Path(directory), serve_static=False)
    assert len(lifespan.registered_components()) == before


# ---------------------------------------------------------------------------
# Derived tables need a caller, or they are empty forever
# ---------------------------------------------------------------------------


def test_every_derived_table_rebuild_has_a_caller() -> None:
    """A derived table nobody refreshes is worse than no table at all.

    This has already happened twice. dim_underlying had an upsert that nothing outside tests
    called, and the omission survived every unit test because the fixtures inserted the rows by
    hand; it surfaced only when a live download silently failed to seal a contract. dim_trading_day
    then repeated it: refresh_trading_days was written, exported and never called, so the planner's
    observed trading days would have been permanently empty and it would have fallen back to the
    weekday rule while appearing to consult real data.

    Both failures look identical from outside: correct code, passing tests, an empty table, and a
    silent downgrade to a guess.
    """
    import pathlib

    import expirymanager

    root = pathlib.Path(expirymanager.__file__).parent
    sources = [
        path.read_text(encoding="utf-8")
        for path in root.rglob("*.py")
        if "maintenance.py" not in path.name
    ]
    body = "\n".join(sources)

    for function in ("refresh_trading_days", "upsert_underlying"):
        assert f"{function}(" in body, (
            f"{function} is defined and exported but nothing outside maintenance calls it, so the "
            "table it maintains stays empty and whatever reads it silently falls back"
        )


def test_a_fresh_install_mirrors_its_underlyings_before_anything_reads_them() -> None:
    """dim_underlying must be populated by starting the app, not by the first download.

    Migration 0004 seeds four builtin underlyings into SQLite, and a SQL migration cannot reach
    DuckDB, so the mirror starts empty. Nine analytic queries join it, and an empty mirror makes
    all nine answer empty while the install looks configured. Contract discovery repaired one
    underlying lazily, which hid the problem behind the first download.

    Asserts the ROW COUNT in DuckDB after a real startup, not that a function was called.
    """
    import pathlib
    import tempfile

    from fastapi.testclient import TestClient

    with tempfile.TemporaryDirectory() as directory:
        root = pathlib.Path(directory)
        application = create_app(root=root, serve_static=False)
        with TestClient(application, base_url="http://127.0.0.1:8000"):
            services = application.state.services
            registry = services.engine.connect()
            try:
                expected = registry.execute(
                    __import__("sqlalchemy").text("SELECT count(*) FROM underlying_registry")
                ).scalar_one()
            finally:
                registry.close()

            mirrored = services.duck.connection.execute(
                "SELECT count(*) FROM dim_underlying"
            ).fetchone()[0]

        assert expected > 0, "the migration seeds builtin underlyings, so this should not be zero"
        assert mirrored == expected, (
            f"{expected} underlyings in the SQLite registry but {mirrored} in the DuckDB mirror, "
            "so every query that joins dim_underlying answers empty on a fresh install"
        )
