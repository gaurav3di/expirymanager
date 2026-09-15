"""Compaction and the maintenance assertions, driven against real files and broken rows.

Compaction is the single most destructive thing this application can do: it closes the live
database, renames it away and puts a different file in its place. So every assertion here is made
against the file system and the reopened database, never against the CompactionResult, which is
the one thing that cannot tell you whether the swap worked.

The assertions in maintenance.py are the other half. A detector that always returns an empty list
looks exactly like a clean database, so each one is run twice: once against a well formed store
where it has to stay empty, and once against a store with a specific row deliberately broken,
where it has to name that row and only that row.

Two measured findings are pinned here as tests rather than left as prose, because both of them
contradict what the code says about itself. They are named
``test_compaction_is_not_safe_to_interrupt_between_the_two_renames`` and
``test_compacting_a_well_clustered_store_grows_the_file_and_then_recommends_itself``.
"""

from __future__ import annotations

import asyncio
import json
import os
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import duckdb
import pytest

from expirymanager.db import maintenance as maintenance_m
from expirymanager.db.arrow import candles_to_arrow
from expirymanager.db.duck import DuckStore
from expirymanager.db.ids import SPOT_ID_MAX, reserve_block
from expirymanager.db.writer import CoverageRow
from expirymanager.db.writes import (
    ContractRow,
    UnderlyingRow,
    record_export,
    start_ingest_run,
    upsert_candle_chunk,
    upsert_contracts,
    upsert_underlying,
)

COLUMNS = ["timestamp", "open", "high", "low", "close", "volume", "open_interest"]
COLUMNS_JSON = json.dumps(COLUMNS, separators=(",", ":"))

EXPIRY = date(2025, 3, 27)
DAY = date(2025, 3, 26)
RES_ID = 2
STRIKES = (22900, 23000)
BARS = 4
PRICES = ("100.5000", "1234567.8901", "0.0025", "149.3075")

# 2025-03-29 is a Saturday. NSE has run special sessions on Saturdays and Sundays, so the
# observed calendar has to be able to hold one. Nothing here may assume weekends are closed.
SATURDAY = date(2025, 3, 29)


def bar_time(day: date, index: int) -> datetime:
    minute = 15 + index
    return datetime(day.year, day.month, day.day, 9 + minute // 60, minute % 60)


def epoch_payload(day: date, *, null_oi_at: int | None = None) -> list[list[object]]:
    """Fyers sends UTC seconds. candles_to_arrow adds the IST offset back on the way in."""
    from datetime import timezone

    from expirymanager.db.arrow import IST_OFFSET_SECONDS

    rows = []
    for index in range(BARS):
        moment = bar_time(day, index)
        rows.append(
            [
                int(moment.replace(tzinfo=timezone.utc).timestamp()) - IST_OFFSET_SECONDS,
                PRICES[0],
                PRICES[1],
                PRICES[2],
                PRICES[3],
                1000 + index,
                None if index == null_oi_at else 50000 + index,
            ]
        )
    return rows


def underlying_row() -> UnderlyingRow:
    return UnderlyingRow(
        underlying_id=1,
        fyers_symbol="NSE:NIFTY50-INDEX",
        root="NIFTY",
        exchange="NSE",
        exchange_code=10,
        segment="CM",
        segment_code=10,
        instrument_kind="INDEX",
        display_name="Nifty 50",
        data_from=date(2022, 1, 3),
    )


def option_row(strike: int, right: str) -> ContractRow:
    return ContractRow(
        fyers_symbol=f"NSE:NIFTY25MAR{strike}{right}",
        kind="OPT",
        instrument_class="OPTIDX",
        exchange="NSE",
        exchange_code=10,
        segment="FO",
        segment_code=11,
        root="NIFTY",
        source_endpoint="expired_contracts",
        parse_method="regex",
        parse_confidence="exact",
        strike=Decimal(strike),
        strike_raw=str(strike),
        option_type=right,
        expiry_date=EXPIRY,
        lot_size=75,
    )


def _seed_side_tables(cur) -> None:
    """Rows in the tables that have no write helper, so compaction has to carry them too.

    Every one of these is in COMPACTED_TABLES. If a compaction quietly dropped a table nobody
    reads daily, the loss would surface months later as a missing symbol master history.
    """
    cur.execute(
        "INSERT INTO dim_trading_day (exchange, trade_date, session_open, session_close, "
        " bar_count, contract_count, derived_from) VALUES "
        "('NSE', DATE '2025-03-20', TIMESTAMP '2025-03-20 09:15:00', "
        " TIMESTAMP '2025-03-20 15:30:00', 375, 4, 'seed')"
    )
    cur.execute(
        "INSERT INTO candle_greeks (contract_id, res_id, ts, iv, delta, gamma, theta, vega, "
        " rho, fp, src) VALUES (1024, 2, TIMESTAMP '2025-03-26 09:15:00', 14.2500, 0.512345, "
        " 0.00012345, -3.250000, 12.500000, 1.250000, 23050.7500, 'test')"
    )
    cur.execute(
        "INSERT INTO chain_snapshot (snapshot_ts, underlying_id, expiry_date, expiry_flag, "
        " strike, option_type, fyers_symbol, ltp, volume, oi, task_id) VALUES "
        "(TIMESTAMP '2025-03-26 10:00:00', 1, DATE '2025-03-27', 'W', 23000.0000, 'CE', "
        " 'NSE:NIFTY25MAR23000CE', 145.2500, 1234, 56789, 7)"
    )
    cur.execute(
        "INSERT INTO dim_instrument_master (fytoken, symbol_ticker, exchange_code, segment_code, "
        " min_lot_size, tick_size, strike_price, option_type, expiry_date, valid_from, row_hash) "
        "VALUES ('101000000012345', 'NSE:NIFTY25MAR23000CE', 10, 11, 75, 0.0500, 23000.0000, "
        " 'CE', DATE '2025-03-27', DATE '2025-03-01', 'abc123')"
    )
    cur.execute(
        "INSERT INTO symbol_master_snapshot (snapshot_date, file, url, sha256, row_count, "
        " byte_size, fetched_at) VALUES (DATE '2025-03-26', 'NSE_FO.csv', "
        " 'https://example.invalid/NSE_FO.csv', 'deadbeef', 120000, 9000000, "
        " TIMESTAMP '2025-03-26 08:00:00')"
    )


async def _seed(store: DuckStore) -> dict[str, int]:
    await store.writer.start()
    try:
        writer = store.writer
        await upsert_underlying(writer, underlying_row())
        result = await upsert_contracts(
            writer,
            underlying_id=1,
            expiry_date=EXPIRY,
            rows=[option_row(strike, right) for strike in STRIKES for right in ("CE", "PE")],
        )
        ids = dict(sorted(result.contract_ids.items()))
        task_id = 1
        for contract_id in ids.values():
            await upsert_candle_chunk(
                writer,
                contract_id=contract_id,
                res_id=RES_ID,
                range_from=DAY,
                range_to=DAY,
                coverage=CoverageRow(
                    status="ok",
                    include_oi=True,
                    columns_json=COLUMNS_JSON,
                    task_id=task_id,
                ),
                batch=candles_to_arrow(
                    epoch_payload(DAY, null_oi_at=1), COLUMNS, contract_id, RES_ID
                ),
            )
            task_id += 1
        run_id = await start_ingest_run(
            writer, job_id="job-1", job_kind="backfill", app_version="0.0.0-test"
        )
        await record_export(
            writer,
            export_id="exp-1",
            kind="query",
            path="/tmp/exp-1.parquet",
            filters={"underlying_id": 1},
            row_count=len(ids) * BARS,
            byte_size=4096,
            sha256="0" * 64,
        )
        assert run_id >= 1
        return ids
    finally:
        await store.writer.stop()


@pytest.fixture
def store(tmp_path: Path):
    duck = DuckStore(tmp_path / "market.duckdb", app_version="0.0.0-test")
    duck.open()
    ids = asyncio.run(_seed(duck))
    duck.connection.execute("BEGIN TRANSACTION")
    _seed_side_tables(duck.connection)
    duck.connection.execute("COMMIT")
    duck.contract_ids = ids  # type: ignore[attr-defined]
    try:
        yield duck
    finally:
        if duck.is_open:
            duck.close()


TOTAL_ROWS = len(STRIKES) * 2 * BARS


def run(awaitable):
    return asyncio.run(awaitable)


def dump(connection, table: str, order_by: str) -> list[tuple]:
    return connection.execute(f"SELECT * FROM {table} ORDER BY {order_by}").fetchall()


def populated_tables(connection) -> list[tuple[str, str]]:
    return [
        (name, order_by)
        for name, order_by in maintenance_m.COMPACTED_TABLES
        if connection.execute(f"SELECT count(*) FROM {name}").fetchone()[0] > 0
    ]


# -- what compaction is required to carry ------------------------------------------------


def test_compaction_names_every_table_the_schema_creates(store):
    """A table added to the schema and not to COMPACTED_TABLES disappears on the next rewrite.

    The source comment says this list is explicit rather than discovered so that the omission
    fails a test. This is that test.
    """
    live = {
        row[0]
        for row in store.connection.execute(
            "SELECT table_name FROM duckdb_tables() WHERE database_name = current_database()"
        ).fetchall()
    }
    compacted = {name for name, _ in maintenance_m.COMPACTED_TABLES}
    assert live - compacted == set(), "these tables would be silently dropped by a compaction"
    assert compacted - live == set(), "these names do not exist and would never be copied"


def test_the_seed_puts_rows_in_every_compacted_table(store):
    """Guard the fixture: an empty table proves nothing about whether compaction copies it."""
    counts = {
        name: store.connection.execute(f"SELECT count(*) FROM {name}").fetchone()[0]
        for name, _ in maintenance_m.COMPACTED_TABLES
    }
    assert all(value > 0 for value in counts.values()), counts


# -- compaction preserves the data --------------------------------------------------------


def test_compaction_reproduces_every_row_of_every_table_value_for_value(store):
    """Not a row count. The whole contents of each table, compared tuple for tuple."""
    tables = populated_tables(store.connection)
    before = {name: dump(store.connection, name, order_by) for name, order_by in tables}

    result = run(maintenance_m.compact(store, keep_backup=False))

    assert store.is_open
    after = {name: dump(store.connection, name, order_by) for name, order_by in tables}
    assert set(after) == set(before)
    for name in before:
        assert after[name] == before[name], f"{name} changed across the compaction"
    assert result.tables_copied == len(maintenance_m.COMPACTED_TABLES)


def test_compaction_keeps_prices_decimal_and_open_interest_null(store):
    """A null that becomes 0, or a price that becomes a float, is invisible in a row count."""
    before = store.connection.execute(
        "SELECT contract_id, ts, open, high, low, close, oi FROM candles "
        " ORDER BY contract_id, ts"
    ).fetchall()
    null_count = store.connection.execute(
        "SELECT count(*) FROM candles WHERE oi IS NULL"
    ).fetchone()[0]
    assert null_count == len(STRIKES) * 2

    run(maintenance_m.compact(store, keep_backup=False))

    after = store.connection.execute(
        "SELECT contract_id, ts, open, high, low, close, oi FROM candles "
        " ORDER BY contract_id, ts"
    ).fetchall()
    assert after == before
    assert all(isinstance(row[2], Decimal) for row in after)
    assert {str(after[0][index]) for index in (2, 3, 4, 5)} == set(PRICES)
    assert sum(1 for row in after if row[6] is None) == null_count
    assert (
        store.connection.execute("SELECT typeof(open) FROM candles LIMIT 1").fetchone()[0]
        == "DECIMAL(11,4)"
    )


def test_compaction_restores_the_physical_sort_order(store):
    """The sort key is the pruning key. Out of order rows defeat every zone map in the file."""
    lowest = store.connection.execute("SELECT min(contract_id) FROM candles").fetchone()[0]
    store.connection.execute(
        "INSERT INTO candles VALUES (?, ?, TIMESTAMP '2025-03-20 09:15:00', "
        "100.5, 101.25, 99.75, 100.0, 1, 1)",
        [lowest, RES_ID],
    )
    keys_before = store.connection.execute(
        "SELECT contract_id, res_id, ts FROM candles"
    ).fetchall()
    assert keys_before != sorted(keys_before)

    run(maintenance_m.compact(store, keep_backup=False))

    keys_after = store.connection.execute(
        "SELECT contract_id, res_id, ts FROM candles"
    ).fetchall()
    assert keys_after == sorted(keys_after)
    assert sorted(keys_after) == sorted(keys_before)


def test_compaction_keeps_every_primary_key_and_unique_constraint(store):
    """A CREATE TABLE AS SELECT copy reclaims the same space and drops all of these."""
    run(maintenance_m.compact(store, keep_backup=False))
    connection = store.connection

    with pytest.raises(duckdb.ConstraintException):
        connection.execute("INSERT INTO dim_contract SELECT * FROM dim_contract LIMIT 1")
    with pytest.raises(duckdb.ConstraintException):
        connection.execute("INSERT INTO dim_underlying SELECT * FROM dim_underlying LIMIT 1")
    with pytest.raises(duckdb.ConstraintException):
        connection.execute("INSERT INTO candle_coverage SELECT * FROM candle_coverage LIMIT 1")
    with pytest.raises(duckdb.ConstraintException):
        connection.execute("INSERT INTO contract_bounds SELECT * FROM contract_bounds LIMIT 1")
    with pytest.raises(duckdb.ConstraintException):
        connection.execute("INSERT INTO dim_expiry SELECT * FROM dim_expiry LIMIT 1")
    with pytest.raises(duckdb.ConstraintException):
        connection.execute(
            "INSERT INTO symbol_master_snapshot SELECT * FROM symbol_master_snapshot LIMIT 1"
        )
    with pytest.raises(duckdb.ConstraintException):
        connection.execute(
            "INSERT INTO dim_instrument_master SELECT * FROM dim_instrument_master LIMIT 1"
        )


def test_compaction_resumes_every_sequence_rather_than_restarting_it(store):
    """A restarted sequence hands a new expiry an id block that already carries bars.

    Every query in the system prunes on contract_id, so two expiries sharing a block means one
    of them silently returns the other's rows.
    """
    connection = store.connection
    highest_block = connection.execute("SELECT max(contract_id_hi) FROM dim_expiry").fetchone()[0]
    expiry_next = int(
        connection.execute("SELECT nextval('seq_expiry_id')").fetchone()[0]
    )
    run_next = int(connection.execute("SELECT nextval('seq_run_id')").fetchone()[0])

    run(maintenance_m.compact(store, keep_backup=False))
    connection = store.connection

    assert int(connection.execute("SELECT nextval('seq_expiry_id')").fetchone()[0]) > expiry_next
    assert int(connection.execute("SELECT nextval('seq_run_id')").fetchone()[0]) > run_next
    lo, hi = reserve_block(connection, 1)
    assert lo == highest_block + 1
    assert hi > lo
    assert (
        connection.execute(
            "SELECT count(*) FROM candles WHERE contract_id BETWEEN ? AND ?", [lo, hi]
        ).fetchone()[0]
        == 0
    )


def test_compaction_rebuilds_the_views_the_copy_does_not_carry(store):
    """The views are recreated by the schema on open, and the health screen reads them."""
    run(maintenance_m.compact(store, keep_backup=False))
    views = {
        row[0]
        for row in store.connection.execute(
            "SELECT view_name FROM duckdb_views() "
            " WHERE database_name = current_database() AND NOT internal"
        ).fetchall()
    }
    assert {"v_data_health", "v_coverage_gaps", "v_candle", "v_contract_full"} <= views
    checks = {row["check_name"]: row["offending"] for row in run(
        maintenance_m.health_checks(store.reader)
    )}
    assert checks["duplicate_keys"] == 0
    assert checks["coverage_row_mismatch"] == 0


def test_the_database_still_takes_writes_after_a_compaction(store):
    """Reopening is not enough. The writer has to be able to land a chunk on the new file."""
    run(maintenance_m.compact(store, keep_backup=False))
    contract_id = min(store.contract_ids.values())

    async def body():
        await store.writer.start()
        try:
            return await upsert_candle_chunk(
                store.writer,
                contract_id=contract_id,
                res_id=RES_ID,
                range_from=SATURDAY,
                range_to=SATURDAY,
                coverage=CoverageRow(
                    status="ok", include_oi=True, columns_json=COLUMNS_JSON, task_id=900
                ),
                batch=candles_to_arrow(epoch_payload(SATURDAY), COLUMNS, contract_id, RES_ID),
            )
        finally:
            await store.writer.stop()

    result = run(body())
    assert result.rows_written == BARS
    assert (
        store.connection.execute(
            "SELECT count(*) FROM candles WHERE contract_id = ? AND CAST(ts AS DATE) = ?",
            [contract_id, SATURDAY],
        ).fetchone()[0]
        == BARS
    )
    assert store.connection.execute("SELECT count(*) FROM candles").fetchone()[0] == (
        TOTAL_ROWS + BARS
    )


def test_the_backup_is_kept_by_default_and_holds_the_data_from_before(store):
    backup = store.db_path.with_name(store.db_path.name + ".before-compaction")
    run(maintenance_m.compact(store, keep_backup=True))
    assert backup.exists()

    opened = DuckStore(backup, app_version="0.0.0-test")
    opened.open()
    try:
        assert opened.connection.execute("SELECT count(*) FROM candles").fetchone()[0] == (
            TOTAL_ROWS
        )
    finally:
        opened.close()


def test_the_backup_is_removed_when_the_caller_asks_for_no_backup(store):
    backup = store.db_path.with_name(store.db_path.name + ".before-compaction")
    run(maintenance_m.compact(store, keep_backup=False))
    assert not backup.exists()
    assert not backup.with_name(backup.name + ".wal").exists()
    assert store.db_path.exists()


def test_the_disk_guard_refuses_before_the_database_is_ever_closed(store):
    monkeypatched = maintenance_m.COMPACTION_MARGIN
    maintenance_m.COMPACTION_MARGIN = 10**9
    try:
        with pytest.raises(maintenance_m.InsufficientDisk) as raised:
            run(maintenance_m.compact(store))
    finally:
        maintenance_m.COMPACTION_MARGIN = monkeypatched

    assert raised.value.needed > raised.value.free
    assert store.is_open
    assert store.connection.execute("SELECT count(*) FROM candles").fetchone()[0] == TOTAL_ROWS
    assert not store.db_path.with_name(store.db_path.name + ".compact").exists()


# -- interrupting a compaction ------------------------------------------------------------


class _RenameShim:
    """Stands in for the ``os`` module so one rename inside the swap can be made to fail.

    A power cut during the swap is not otherwise reproducible in a test, and the swap is the
    only part of this application where the user's whole dataset is in flight at once.
    """

    def __init__(self, real, fail_after: int) -> None:
        self._real = real
        self._fail_after = fail_after
        self.calls = 0

    def __getattr__(self, name):
        return getattr(self._real, name)

    def replace(self, source, destination):
        self.calls += 1
        if self.calls > self._fail_after:
            raise OSError("interrupted")
        return self._real.replace(source, destination)


def test_an_interrupt_before_the_first_rename_keeps_the_data_but_leaves_the_store_closed(
    store, monkeypatch
):
    """The data is safe here. The process is not.

    Everything up to the swap writes only the target file, so market.duckdb still holds every
    row. But compact() closes the live DuckStore before the first rename and only reopens it
    after the last one, and nothing on the failure path puts it back. The application is left
    holding a closed store and answers every subsequent query with a closed database error until
    the process is restarted, even though the file on disk is perfectly intact.
    """
    before = dump(store.connection, "candles", "contract_id, res_id, ts")
    monkeypatch.setattr(maintenance_m, "os", _RenameShim(os, fail_after=0))

    with pytest.raises(OSError):
        run(maintenance_m.compact(store))

    monkeypatch.setattr(maintenance_m, "os", os)
    assert store.db_path.exists()
    assert store.is_open is False
    with pytest.raises(Exception):
        store.connection.execute("SELECT count(*) FROM candles")

    # The file itself lost nothing, which is the half that is safe.
    store.open()
    assert dump(store.connection, "candles", "contract_id, res_id, ts") == before

    # The stray target is removed by the next run rather than left to confuse anybody.
    target = store.db_path.with_name(store.db_path.name + ".compact")
    assert target.exists()
    run(maintenance_m.compact(store, keep_backup=False))
    assert not target.exists()
    assert dump(store.connection, "candles", "contract_id, res_id, ts") == before


def test_compaction_is_not_safe_to_interrupt_between_the_two_renames(store, monkeypatch):
    """Measured, and the reason a compaction must never be made automatic or scheduled.

    The swap is two renames with no journal between them. The first moves market.duckdb aside to
    market.duckdb.before-compaction and the second moves the rewritten file into its place. An
    interruption in that window leaves no market.duckdb at all, and the next start of the
    application creates an empty one and reports zero candles without an error, because an
    absent DuckDB file is indistinguishable from a first run.

    Both halves of the data are still on disk, so the loss is recoverable by hand, and this test
    proves the recovery as well as the hazard. Nothing in the application performs it.
    """
    before = dump(store.connection, "candles", "contract_id, res_id, ts")
    db_path = store.db_path
    backup = db_path.with_name(db_path.name + ".before-compaction")
    target = db_path.with_name(db_path.name + ".compact")

    shim = _RenameShim(os, fail_after=1)
    monkeypatch.setattr(maintenance_m, "os", shim)
    with pytest.raises(OSError):
        run(maintenance_m.compact(store))
    monkeypatch.setattr(maintenance_m, "os", os)

    # The hazard, asserted against the file system rather than described.
    assert not db_path.exists()
    assert not store.is_open
    assert backup.exists() and target.exists()

    restarted = DuckStore(db_path, app_version="0.0.0-test")
    restarted.open()
    try:
        assert restarted.connection.execute("SELECT count(*) FROM candles").fetchone()[0] == 0
    finally:
        restarted.close()
    db_path.unlink()

    # The recovery, which a human has to know to perform.
    os.replace(backup, db_path)
    recovered = DuckStore(db_path, app_version="0.0.0-test")
    recovered.open()
    try:
        assert dump(recovered.connection, "candles", "contract_id, res_id, ts") == before
    finally:
        recovered.close()


# -- what compaction does to the file size ------------------------------------------------


def churn(connection, *, rows: int, cycles: int) -> None:
    """Delete-then-insert cycles, which is what actually fragments a DuckDB file."""
    for _ in range(cycles):
        connection.execute(
            "INSERT INTO candles SELECT 900000 + (i % 40), 2, "
            "  TIMESTAMP '2024-01-01 09:15:00' + INTERVAL (i) MINUTE, "
            "  CAST(100 + random() * 50 AS DECIMAL(11,4)), "
            "  CAST(100 + random() * 50 AS DECIMAL(11,4)), "
            "  CAST(100 + random() * 50 AS DECIMAL(11,4)), "
            "  CAST(100 + random() * 50 AS DECIMAL(11,4)), "
            "  CAST(random() * 100000 AS BIGINT), CAST(random() * 100000 AS BIGINT) "
            f" FROM range({rows}) t(i)"
        )
        connection.execute("DELETE FROM candles WHERE volume % 2 = 0")
        connection.execute("CHECKPOINT")


def test_compaction_reclaims_space_on_a_churned_file(store):
    """The case the feature exists for: repeated rewrites, then a full copy."""
    churn(store.connection, rows=150_000, cycles=4)
    rows_before = store.connection.execute("SELECT count(*) FROM candles").fetchone()[0]
    size_before = store.db_path.stat().st_size

    result = run(maintenance_m.compact(store, keep_backup=False))
    store.connection.execute("CHECKPOINT")

    assert store.connection.execute("SELECT count(*) FROM candles").fetchone()[0] == rows_before
    assert store.db_path.stat().st_size < size_before
    assert result.bytes_reclaimed > 0


def test_compacting_a_well_clustered_store_grows_the_file_and_then_recommends_itself(tmp_path):
    """Measured defect, pinned so the behaviour cannot change without this test saying so.

    A store loaded once, in order, is already as compact as DuckDB will make it. Copying it
    through an ATTACHed database uses roughly twice as many blocks for the same rows at the same
    per column compression, so the rewrite makes the file substantially larger rather than
    smaller. The file then measures above BLOAT_SUGGEST_RATIO against the modelled size, so the
    storage screen starts recommending a compaction, and a second run reclaims nothing at all.

    The rows are all still correct. What is wrong is the file, the standing recommendation, and
    on a real store the downtime of a rewrite that cannot help.
    """
    duck = DuckStore(tmp_path / "clustered.duckdb", app_version="0.0.0-test")
    duck.open()
    try:
        duck.connection.execute(
            "INSERT INTO candles SELECT 900000 + (i % 40), 2, "
            "  TIMESTAMP '2024-01-01 09:15:00' + INTERVAL (i) MINUTE, "
            "  100.5000, 101.2500, 99.7500, 100.0000, i, i FROM range(200000) t(i)"
        )
        duck.connection.execute("CHECKPOINT")
        rows = duck.connection.execute("SELECT count(*) FROM candles").fetchone()[0]
        size_before = duck.db_path.stat().st_size
        report_before = run(maintenance_m.storage_report(duck.reader, db_path=duck.db_path))
        assert report_before.compaction_suggested is False

        first = run(maintenance_m.compact(duck, keep_backup=False))
        duck.connection.execute("CHECKPOINT")
        size_after = duck.db_path.stat().st_size

        assert duck.connection.execute("SELECT count(*) FROM candles").fetchone()[0] == rows
        assert size_after > size_before
        assert first.bytes_reclaimed < 0

        report_after = run(maintenance_m.storage_report(duck.reader, db_path=duck.db_path))
        assert report_after.compaction_suggested is True
        assert report_after.bloat_ratio > report_before.bloat_ratio

        # And running it again settles at the same size, so the recommendation never clears.
        second = run(maintenance_m.compact(duck, keep_backup=False))
        duck.connection.execute("CHECKPOINT")
        assert duck.db_path.stat().st_size == size_after
        assert (
            run(
                maintenance_m.storage_report(duck.reader, db_path=duck.db_path)
            ).compaction_suggested
            is True
        )

        # The second run nonetheless reports bytes reclaimed, because bytes_after is measured
        # the instant the file reopens and the reopen has not yet written out the schema it
        # re-applies. The number in the Optimise notification is therefore smaller than the file
        # the user will actually find on disk.
        assert second.bytes_reclaimed > 0
        assert second.bytes_after < duck.db_path.stat().st_size
    finally:
        if duck.is_open:
            duck.close()


# -- checkpoint and the storage report ----------------------------------------------------


def test_checkpoint_folds_a_real_write_ahead_log_into_the_file(store):
    """The WAL size on both sides is the only visible evidence the checkpoint did anything."""
    store.connection.execute(
        "INSERT INTO candles SELECT 900000 + (i % 8), 2, "
        "  TIMESTAMP '2024-02-01 09:15:00' + INTERVAL (i) MINUTE, "
        "  100.5000, 101.2500, 99.7500, 100.0000, i, i FROM range(5000) t(i)"
    )
    wal = store.db_path.with_name(store.db_path.name + ".wal")
    assert wal.exists() and wal.stat().st_size > 0

    result = run(maintenance_m.checkpoint(store))

    assert result.wal_bytes_before > 0
    assert result.wal_bytes_after < result.wal_bytes_before
    assert (wal.stat().st_size if wal.exists() else 0) == result.wal_bytes_after


def test_the_storage_report_measures_the_files_it_is_given(store, tmp_path):
    exports = tmp_path / "exports"
    exports.mkdir()
    (exports / "one.parquet").write_bytes(b"x" * 2048)
    raw = tmp_path / "raw"
    (raw / "2025" / "03").mkdir(parents=True)
    (raw / "2025" / "03" / "body.json").write_bytes(b"y" * 512)
    sqlite_path = tmp_path / "config.sqlite3"
    sqlite_path.write_bytes(b"z" * 128)

    report = run(
        maintenance_m.storage_report(
            store.reader,
            db_path=store.db_path,
            sqlite_path=sqlite_path,
            exports_dir=exports,
            raw_dir=raw,
        )
    )

    assert report.candle_rows == TOTAL_ROWS
    assert report.exports_bytes == 2048
    assert report.raw_payload_bytes == 512
    assert report.sqlite_bytes == 128
    assert report.duckdb_bytes == store.db_path.stat().st_size
    assert report.modelled_bytes == int(TOTAL_ROWS * maintenance_m.BYTES_PER_CANDLE_ROW)
    assert report.bytes_per_row == pytest.approx(report.duckdb_bytes / TOTAL_ROWS)
    assert report.free_disk_bytes > 0


def test_the_bloat_suggestion_is_off_for_an_empty_store(tmp_path):
    """Zero rows models zero bytes, and a ratio built on a division by zero must not suggest."""
    empty = DuckStore(tmp_path / "empty.duckdb", app_version="0.0.0-test")
    empty.open()
    try:
        report = run(maintenance_m.storage_report(empty.reader, db_path=empty.db_path))
        assert report.candle_rows == 0
        assert report.modelled_bytes == 0
        assert report.bloat_ratio == 1.0
        assert report.compaction_suggested is False
        assert report.bytes_per_row == 0.0
    finally:
        empty.close()


# -- the assertions, against deliberately broken rows -------------------------------------


def test_the_duplicate_assertion_is_empty_on_a_well_formed_store(store):
    """Half of a detector's job. One that always returns [] passes every other test here."""
    assert run(maintenance_m.duplicate_rows(store.reader)) == []


def test_the_duplicate_assertion_finds_each_broken_key_and_counts_the_copies(store):
    connection = store.connection
    ids = sorted(store.contract_ids.values())
    # One key duplicated three times and another twice, so the ordering can be checked too.
    connection.execute(
        "INSERT INTO candles SELECT * FROM candles WHERE contract_id = ? "
        " ORDER BY ts LIMIT 1",
        [ids[0]],
    )
    connection.execute(
        "INSERT INTO candles SELECT * FROM candles WHERE contract_id = ? "
        " ORDER BY ts LIMIT 1",
        [ids[0]],
    )
    connection.execute(
        "INSERT INTO candles SELECT * FROM candles WHERE contract_id = ? "
        " ORDER BY ts LIMIT 1",
        [ids[1]],
    )

    found = run(maintenance_m.duplicate_rows(store.reader))

    assert [(row[0], row[3]) for row in found] == [(ids[0], 3), (ids[1], 2)]
    assert all(row[1] == RES_ID for row in found)
    assert found[0][2] == min(
        connection.execute(
            "SELECT ts FROM candles WHERE contract_id = ?", [ids[0]]
        ).fetchall()
    )[0]
    assert run(maintenance_m.duplicate_rows(store.reader, limit=1)) == found[:1]

    checks = {row["check_name"]: row["offending"] for row in run(
        maintenance_m.health_checks(store.reader)
    )}
    assert checks["duplicate_keys"] == 2


def test_the_coverage_reconciliation_is_empty_on_a_well_formed_store(store):
    assert run(maintenance_m.reconcile_coverage(store.reader)) == []


def test_the_coverage_reconciliation_catches_a_ledger_that_claims_too_many_rows(store):
    ids = sorted(store.contract_ids.values())
    store.connection.execute(
        "UPDATE candle_coverage SET row_count = 99 WHERE contract_id = ?", [ids[0]]
    )
    found = run(maintenance_m.reconcile_coverage(store.reader))
    assert [(row["contract_id"], row["claimed"], row["actual"]) for row in found] == [
        (ids[0], 99, BARS)
    ]


def test_the_coverage_reconciliation_catches_rows_deleted_behind_the_ledger(store):
    """The other direction: the rows went and the ledger still says they are there."""
    ids = sorted(store.contract_ids.values())
    store.connection.execute("DELETE FROM candles WHERE contract_id = ?", [ids[1]])
    found = run(maintenance_m.reconcile_coverage(store.reader))
    assert [(row["contract_id"], row["claimed"], row["actual"]) for row in found] == [
        (ids[1], BARS, 0)
    ]


def test_the_coverage_reconciliation_ignores_a_chunk_that_never_claimed_rows(store):
    """An 'empty' or 'error' chunk has no rows by definition and is not a disagreement."""
    ids = sorted(store.contract_ids.values())
    store.connection.execute(
        "INSERT INTO candle_coverage (contract_id, res_id, range_from, range_to, status, "
        " row_count, include_oi, columns_json, task_id, fetched_at) "
        "VALUES (?, ?, DATE '2025-01-02', DATE '2025-01-02', 'empty', 0, true, ?, 800, "
        " TIMESTAMP '2025-01-02 18:00:00')",
        [ids[0], RES_ID, COLUMNS_JSON],
    )
    store.connection.execute(
        "INSERT INTO candle_coverage (contract_id, res_id, range_from, range_to, status, "
        " row_count, include_oi, columns_json, task_id, fetched_at) "
        "VALUES (?, ?, DATE '2025-01-03', DATE '2025-01-03', 'error', 4321, true, ?, 801, "
        " TIMESTAMP '2025-01-03 18:00:00')",
        [ids[0], RES_ID, COLUMNS_JSON],
    )
    assert run(maintenance_m.reconcile_coverage(store.reader)) == []


def test_the_coverage_reconciliation_orders_the_worst_disagreement_first(store):
    ids = sorted(store.contract_ids.values())
    store.connection.execute(
        "UPDATE candle_coverage SET row_count = 5 WHERE contract_id = ?", [ids[0]]
    )
    store.connection.execute(
        "UPDATE candle_coverage SET row_count = 500 WHERE contract_id = ?", [ids[1]]
    )
    found = run(maintenance_m.reconcile_coverage(store.reader))
    assert [row["contract_id"] for row in found] == [ids[1], ids[0]]
    assert run(maintenance_m.reconcile_coverage(store.reader, limit=1)) == found[:1]


def test_every_health_check_is_clean_before_anything_is_broken(store):
    checks = {row["check_name"]: row["offending"] for row in run(
        maintenance_m.health_checks(store.reader)
    )}
    assert set(checks) == {
        "duplicate_keys",
        "coverage_row_mismatch",
        "contracts_with_no_bars",
        "spot_id_out_of_reserved_range",
        "contract_id_below_first_block",
        "contract_id_outside_block",
        "overlapping_id_blocks",
        "unaligned_id_blocks",
        "candles_without_a_contract",
    }
    assert all(value == 0 for value in checks.values()), checks


def health(store) -> dict[str, int]:
    return {row["check_name"]: row["offending"] for row in run(
        maintenance_m.health_checks(store.reader)
    )}


def test_a_spot_contract_pushed_out_of_its_reserved_range_is_reported(store):
    store.connection.execute(
        "UPDATE dim_contract SET contract_id = ? WHERE kind = 'SPOT'", [SPOT_ID_MAX + 1]
    )
    checks = health(store)
    assert checks["spot_id_out_of_reserved_range"] == 1
    assert checks["overlapping_id_blocks"] == 0


def test_an_option_id_below_the_first_block_is_reported(store):
    ids = sorted(store.contract_ids.values())
    store.connection.execute(
        "UPDATE dim_contract SET contract_id = 5 WHERE contract_id = ?", [ids[0]]
    )
    checks = health(store)
    assert checks["contract_id_below_first_block"] == 1
    assert checks["contract_id_outside_block"] == 1


def test_two_expiries_sharing_an_id_block_are_reported(store):
    """The worst failure this system has: a query pruning on contract_id returns other rows."""
    connection = store.connection
    lo, hi = connection.execute(
        "SELECT contract_id_lo, contract_id_hi FROM dim_expiry LIMIT 1"
    ).fetchone()
    connection.execute(
        "INSERT INTO dim_expiry (expiry_id, underlying_id, expiry_date, contract_id_lo, "
        " contract_id_hi, discovered_at) VALUES (9999, 1, DATE '2025-04-24', ?, ?, "
        " TIMESTAMP '2025-03-26 08:00:00')",
        [lo, hi],
    )
    checks = health(store)
    assert checks["overlapping_id_blocks"] == 1


def test_a_block_that_does_not_start_on_a_block_boundary_is_reported(store):
    store.connection.execute(
        "UPDATE dim_expiry SET contract_id_lo = contract_id_lo + 1 "
        " WHERE contract_id_lo IS NOT NULL"
    )
    checks = health(store)
    assert checks["unaligned_id_blocks"] == 1


def test_a_bar_whose_contract_has_gone_is_reported(store):
    ids = sorted(store.contract_ids.values())
    store.connection.execute("DELETE FROM dim_contract WHERE contract_id = ?", [ids[0]])
    checks = health(store)
    assert checks["candles_without_a_contract"] == 1


def test_a_contract_with_no_bars_at_all_is_reported(store):
    ids = sorted(store.contract_ids.values())
    store.connection.execute("DELETE FROM contract_bounds WHERE contract_id = ?", [ids[0]])
    checks = health(store)
    assert checks["contracts_with_no_bars"] == 1


# -- the observed trading calendar --------------------------------------------------------


def test_the_trading_calendar_takes_its_session_bounds_from_the_bars_not_from_a_rule(store):
    """Session hours are not constants. The NSE derivatives close moved, and weekends open.

    The rebuild records the observed first and last bar of each day, including a Saturday, and
    nothing here may be inferred from a weekday rule or a fixed session length.
    """
    spot_id = store.connection.execute(
        "SELECT spot_contract_id FROM dim_underlying WHERE underlying_id = 1"
    ).fetchone()[0]

    async def body():
        await store.writer.start()
        try:
            for day in (DAY, SATURDAY):
                await upsert_candle_chunk(
                    store.writer,
                    contract_id=spot_id,
                    res_id=RES_ID,
                    range_from=day,
                    range_to=day,
                    coverage=CoverageRow(
                        status="ok",
                        include_oi=False,
                        columns_json=COLUMNS_JSON,
                        task_id=600 + day.day,
                    ),
                    batch=candles_to_arrow(epoch_payload(day), COLUMNS, spot_id, RES_ID),
                )
            return await maintenance_m.refresh_trading_days(store.writer, res_id=RES_ID)
        finally:
            await store.writer.stop()

    assert run(body()) == 2

    rows = store.connection.execute(
        "SELECT exchange, trade_date, session_open, session_close, bar_count, contract_count "
        "  FROM dim_trading_day WHERE derived_from = 'spot_bars' ORDER BY trade_date"
    ).fetchall()
    assert [row[1] for row in rows] == [DAY, SATURDAY]
    for row in rows:
        day = row[1]
        assert row[0] == "NSE"
        assert row[2] == bar_time(day, 0)
        assert row[3] == bar_time(day, BARS - 1)
        assert row[4] == BARS
        assert row[5] == 1
    assert SATURDAY.weekday() == 5, "the Saturday session is the point of this test"

    # Rerunning is a replace, not an accumulate, and the seeded row from another source stays.
    assert _rerun(store) == 2
    assert (
        store.connection.execute(
            "SELECT count(*) FROM dim_trading_day WHERE derived_from = 'spot_bars'"
        ).fetchone()[0]
        == 2
    )
    assert (
        store.connection.execute(
            "SELECT count(*) FROM dim_trading_day WHERE derived_from = 'seed'"
        ).fetchone()[0]
        == 1
    )


def _rerun(store) -> int:
    async def body():
        await store.writer.start()
        try:
            return await maintenance_m.refresh_trading_days(store.writer, res_id=RES_ID)
        finally:
            await store.writer.stop()

    return run(body())


def test_a_calendar_rebuild_that_fails_leaves_the_previous_rows_in_place(store, monkeypatch):
    """The rebuild deletes before it inserts, so the transaction is the only thing saving it."""
    spot_id = store.connection.execute(
        "SELECT spot_contract_id FROM dim_underlying WHERE underlying_id = 1"
    ).fetchone()[0]

    async def seed():
        await store.writer.start()
        try:
            await upsert_candle_chunk(
                store.writer,
                contract_id=spot_id,
                res_id=RES_ID,
                range_from=DAY,
                range_to=DAY,
                coverage=CoverageRow(
                    status="ok", include_oi=False, columns_json=COLUMNS_JSON, task_id=700
                ),
                batch=candles_to_arrow(epoch_payload(DAY), COLUMNS, spot_id, RES_ID),
            )
            return await maintenance_m.refresh_trading_days(store.writer, res_id=RES_ID)
        finally:
            await store.writer.stop()

    assert run(seed()) == 1
    before = dump(store.connection, "dim_trading_day", "exchange, trade_date")

    real = maintenance_m._refresh_trading_days_sync

    def explode(cur, res_id):
        cur.execute("BEGIN TRANSACTION")
        cur.execute("DELETE FROM dim_trading_day WHERE derived_from = 'spot_bars'")
        cur.execute("ROLLBACK")
        raise RuntimeError("the rebuild died after the delete")

    monkeypatch.setattr(maintenance_m, "_refresh_trading_days_sync", explode)
    with pytest.raises(RuntimeError):
        _rerun(store)
    monkeypatch.setattr(maintenance_m, "_refresh_trading_days_sync", real)

    assert dump(store.connection, "dim_trading_day", "exchange, trade_date") == before
