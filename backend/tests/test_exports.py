"""Export round trips, value for value, against the store the rows came from.

Nothing in this project had ever produced an export end to end before this file, so every
assertion here compares a file that was actually written against the rows that are actually in
the database. Not a byte size, not a row count on its own, not a return value.

Three things are checked that a row count would miss.

Prices are DECIMAL(11,4). A price that becomes a float somewhere between the store and the file
still compares equal for most values, so the physical column type and the written text are
asserted rather than the numeric value alone. A DOUBLE writes 100.5 where a DECIMAL(11,4) writes
100.5000, and that difference is the whole test.

ts_utc_epoch is documented as seconds since the Unix epoch. DuckDB's epoch() returns a DOUBLE, so
without a cast the CSV carries 1732160760.0 and the Parquet column is a float64. The written text
and the Parquet schema are both asserted here, because the sidecar claiming BIGINT while the file
holds a DOUBLE is exactly the silent wrong answer this suite exists to catch.

Open interest is NULL for an index and for any chunk fetched with include_oi off. A null that
becomes a zero on the way out turns missing data into a real reading of zero contracts, which no
consumer can detect afterwards, so both formats are checked for nulls as well as for values.
"""

from __future__ import annotations

import asyncio
import csv
import json
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

import duckdb
import pytest

from expirymanager.db import exports as exports_m
from expirymanager.db.arrow import IST_OFFSET_SECONDS, candles_to_arrow
from expirymanager.db.duck import DuckStore
from expirymanager.db.writer import CoverageRow
from expirymanager.db.writes import (
    ContractRow,
    UnderlyingRow,
    upsert_candle_chunk,
    upsert_contracts,
    upsert_underlying,
)

COLUMNS_OI = ["timestamp", "open", "high", "low", "close", "volume", "open_interest"]
COLUMNS_NO_OI = ["timestamp", "open", "high", "low", "close", "volume"]
COLUMNS_OI_JSON = json.dumps(COLUMNS_OI, separators=(",", ":"))
COLUMNS_NO_OI_JSON = json.dumps(COLUMNS_NO_OI, separators=(",", ":"))

EXPIRY = date(2025, 3, 27)
DAY = date(2025, 3, 26)
RES_ID = 2
STRIKES = (22900, 23000)

# Bar times chosen to break anything that assumes a session. Midnight IST is the previous day in
# UTC, and 23:59 is the far end of the same IST day, so a conversion that works on dates rather
# than instants lands on the wrong day for both. The middle two are the real session edges, and
# 15:40 is the close the NSE derivatives segment moved to, which is why nothing may hardcode it.
BAR_MINUTES = (
    (0, 0),
    (9, 15),
    (15, 40),
    (23, 59),
)

# Every one of these is a deliberate edge of DECIMAL(11,4). 100.5000 exposes a DOUBLE by losing
# its trailing zeros in text. 1234567.8901 is the widest value the type holds, eleven digits with
# four after the point. 0.0025 is the currency derivative tick that two decimal places truncate,
# which is the measured reason the scale is 4 and not 2.
PRICES = ("100.5000", "1234567.8901", "0.0025", "149.3075")


def utc_epoch_for_ist(moment: datetime) -> int:
    """The UTC second for a naive IST wall clock time, computed independently of the export."""
    return int(moment.replace(tzinfo=timezone.utc).timestamp()) - IST_OFFSET_SECONDS


def bar_time(index: int) -> datetime:
    hour, minute = BAR_MINUTES[index]
    return datetime(DAY.year, DAY.month, DAY.day, hour, minute)


BARS = len(BAR_MINUTES)


def payload(*, with_oi: bool, null_oi_at: int | None = None) -> list[list[object]]:
    rows: list[list[object]] = []
    for index in range(BARS):
        row: list[object] = [
            utc_epoch_for_ist(bar_time(index)),
            PRICES[0],
            PRICES[1],
            PRICES[2],
            PRICES[3],
            1000 + index,
        ]
        if with_oi:
            row.append(None if index == null_oi_at else 50000 + index)
        rows.append(row)
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
        for position, (_, contract_id) in enumerate(ids.items()):
            # One contract in the set is written with include_oi off, so the export has to carry
            # a wholly null oi column beside populated ones, and one populated chunk hides a
            # single null bar inside it.
            with_oi = position != 0
            await upsert_candle_chunk(
                writer,
                contract_id=contract_id,
                res_id=RES_ID,
                range_from=DAY,
                range_to=DAY,
                coverage=CoverageRow(
                    status="ok",
                    include_oi=with_oi,
                    columns_json=COLUMNS_OI_JSON if with_oi else COLUMNS_NO_OI_JSON,
                    task_id=task_id,
                ),
                batch=candles_to_arrow(
                    payload(with_oi=with_oi, null_oi_at=2 if with_oi else None),
                    COLUMNS_OI if with_oi else COLUMNS_NO_OI,
                    contract_id,
                    RES_ID,
                ),
            )
            task_id += 1
        return ids
    finally:
        await store.writer.stop()


@pytest.fixture
def seeded(tmp_path: Path):
    store = DuckStore(tmp_path / "market.duckdb", app_version="0.0.0-test")
    store.open()
    ids = asyncio.run(_seed(store))
    try:
        yield (store, ids, tmp_path / "exports")
    finally:
        if store.is_open:
            store.close()


def export(store, spec, exports_dir, export_id="e1"):
    return asyncio.run(
        exports_m.run_export(store, spec, exports_dir=exports_dir, export_id=export_id)
    )


def read_file(path: Path, sql: str) -> list[tuple]:
    """Read an export back through a connection that has never seen this application's store."""
    con = duckdb.connect()
    try:
        return con.execute(sql.replace("{file}", str(path))).fetchall()
    finally:
        con.close()


CONTRACTS = len(STRIKES) * 2
TOTAL_ROWS = CONTRACTS * BARS


# -- what is actually in the store, so the file can be compared against it -------------------


def store_rows(store) -> list[tuple]:
    return store.connection.execute(
        "SELECT contract_id, res_id, ts, open, high, low, close, volume, oi FROM candles "
        " ORDER BY contract_id, res_id, ts"
    ).fetchall()


def test_the_seed_holds_the_decimal_and_null_shape_the_round_trips_depend_on(seeded):
    """Guard the fixture itself, so a later change cannot quietly make every export test weaker."""
    store, _, _ = seeded
    rows = store_rows(store)
    assert len(rows) == TOTAL_ROWS
    assert all(isinstance(row[3], Decimal) for row in rows)
    assert {str(rows[0][index]) for index in (3, 4, 5, 6)} == set(PRICES)
    ois = [row[8] for row in rows]
    assert ois.count(None) == BARS + (CONTRACTS - 1)
    assert any(value is not None for value in ois)


# -- parquet ---------------------------------------------------------------------------------


def test_a_parquet_export_reproduces_every_stored_value_row_for_row(seeded):
    store, _, exports_dir = seeded
    result = export(store, exports_m.ExportSpec(), exports_dir)

    written = read_file(
        result.path,
        "SELECT contract_id, res_id, ts_ist, ts_utc_epoch, open, high, low, close, volume, oi "
        "  FROM read_parquet('{file}') ORDER BY contract_id, res_id, ts_utc_epoch",
    )
    assert len(written) == TOTAL_ROWS == result.row_count

    for stored, exported in zip(store_rows(store), written):
        contract_id, res_id, ts, open_, high, low, close, volume, oi = stored
        assert exported[0] == contract_id
        assert exported[1] == res_id
        assert exported[2] == ts.strftime("%Y-%m-%d %H:%M:%S")
        assert exported[3] == utc_epoch_for_ist(ts)
        # Decimal equality, and the type with it. float(open_) == exported[4] would pass on a
        # DOUBLE column and that is the failure being excluded.
        assert [exported[4], exported[5], exported[6], exported[7]] == [open_, high, low, close]
        assert all(isinstance(value, Decimal) for value in exported[4:8])
        assert exported[8] == volume
        assert exported[9] == oi
        assert (exported[9] is None) == (oi is None)


def test_the_parquet_price_columns_are_still_decimal_11_4(seeded):
    store, _, exports_dir = seeded
    result = export(store, exports_m.ExportSpec(), exports_dir)
    described = dict(
        (row[0], row[1])
        for row in read_file(result.path, "DESCRIBE SELECT * FROM read_parquet('{file}')")
    )
    for column in ("open", "high", "low", "close"):
        assert described[column] == "DECIMAL(11,4)"


def test_the_parquet_epoch_column_is_an_integer_not_a_double(seeded):
    """DuckDB's epoch() returns a DOUBLE, so the export has to cast it.

    A backtester reading a column documented as epoch seconds would otherwise get a float64 and
    have to round it back, and a reader that trusts the documented type reads it as an integer
    and gets whatever its language does with 1732160760.0.
    """
    store, _, exports_dir = seeded
    result = export(store, exports_m.ExportSpec(), exports_dir)
    described = dict(
        (row[0], row[1])
        for row in read_file(result.path, "DESCRIBE SELECT * FROM read_parquet('{file}')")
    )
    assert described["ts_utc_epoch"] == "BIGINT"

    values = read_file(result.path, "SELECT DISTINCT ts_utc_epoch FROM read_parquet('{file}')")
    assert all(isinstance(row[0], int) and not isinstance(row[0], bool) for row in values)


def test_the_raw_parquet_export_also_carries_an_integer_epoch(seeded):
    store, _, exports_dir = seeded
    result = export(store, exports_m.ExportSpec(denormalise=False), exports_dir, export_id="raw")
    described = dict(
        (row[0], row[1])
        for row in read_file(result.path, "DESCRIBE SELECT * FROM read_parquet('{file}')")
    )
    assert described["ts_utc_epoch"] == "BIGINT"
    assert described["open"] == "DECIMAL(11,4)"
    assert set(described) == {
        "contract_id",
        "res_id",
        "ts_ist",
        "ts_utc_epoch",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "oi",
    }


def test_the_sidecar_declares_the_types_the_parquet_file_actually_holds(seeded):
    """The sidecar is what a reader six months from now trusts, so it may not disagree."""
    store, _, exports_dir = seeded
    result = export(store, exports_m.ExportSpec(), exports_dir)
    sidecar = json.loads(
        result.path.with_name(result.path.name + ".schema.json").read_text(encoding="utf-8")
    )
    declared = {column["name"]: column["type"] for column in sidecar["columns"]}
    actual = dict(
        (row[0], row[1])
        for row in read_file(result.path, "DESCRIBE SELECT * FROM read_parquet('{file}')")
    )
    assert declared == actual
    assert declared["ts_utc_epoch"] == "BIGINT"
    assert sidecar["conventions"]["ist_offset_seconds"] == IST_OFFSET_SECONDS


def test_midnight_and_the_last_minute_of_the_ist_day_round_trip_to_the_right_utc_instant(seeded):
    """IST midnight is the previous day in UTC. A date-based conversion lands a day out."""
    store, _, exports_dir = seeded
    result = export(store, exports_m.ExportSpec(), exports_dir)
    pairs = read_file(
        result.path,
        "SELECT DISTINCT ts_ist, ts_utc_epoch FROM read_parquet('{file}') ORDER BY ts_utc_epoch",
    )
    assert [row[0] for row in pairs] == [
        bar_time(index).strftime("%Y-%m-%d %H:%M:%S") for index in range(BARS)
    ]
    assert [row[1] for row in pairs] == [utc_epoch_for_ist(bar_time(index)) for index in range(BARS)]

    midnight = pairs[0]
    assert midnight[0] == "2025-03-26 00:00:00"
    assert (
        datetime.fromtimestamp(midnight[1], tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
        == "2025-03-25 18:30"
    )
    last = pairs[-1]
    assert last[0] == "2025-03-26 23:59:00"
    assert (
        datetime.fromtimestamp(last[1], tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
        == "2025-03-26 18:29"
    )


def test_the_parquet_sha256_matches_the_bytes_on_disk(seeded):
    store, _, exports_dir = seeded
    result = export(store, exports_m.ExportSpec(), exports_dir)
    assert result.sha256 == exports_m.sha256_file(result.path)
    assert result.byte_size == result.path.stat().st_size
    assert result.files == (result.path,)


# -- csv -------------------------------------------------------------------------------------


def csv_rows(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    """The CSV read as text, not through a type sniffer that would hide the bug."""
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        return (list(reader.fieldnames or []), list(reader))


def test_the_csv_epoch_column_is_written_without_a_decimal_point(seeded):
    """The deferred defect: 1732160760.0 in a column documented as epoch seconds."""
    store, _, exports_dir = seeded
    result = export(store, exports_m.ExportSpec(format="csv"), exports_dir, export_id="c1")
    _, rows = csv_rows(result.path)

    assert len(rows) == TOTAL_ROWS
    written = [row["ts_utc_epoch"] for row in rows]
    assert all("." not in value for value in written)
    assert all("e" not in value.lower() for value in written)
    assert sorted({int(value) for value in written}) == sorted(
        {utc_epoch_for_ist(bar_time(index)) for index in range(BARS)}
    )


def test_the_csv_prices_keep_all_four_decimal_places(seeded):
    """A DOUBLE writes 100.5 and 0.0025 loses nothing visible. The text is the evidence."""
    store, _, exports_dir = seeded
    result = export(store, exports_m.ExportSpec(format="csv"), exports_dir, export_id="c1")
    _, rows = csv_rows(result.path)

    for column, expected in zip(("open", "high", "low", "close"), PRICES):
        assert {row[column] for row in rows} == {expected}
        assert len({row[column] for row in rows}.pop().split(".")[1]) == 4


def test_the_csv_writes_a_null_open_interest_as_an_empty_field_not_a_zero(seeded):
    """A null that arrives as 0 turns missing data into a real reading of zero open contracts."""
    store, _, exports_dir = seeded
    result = export(store, exports_m.ExportSpec(format="csv"), exports_dir, export_id="c1")
    _, rows = csv_rows(result.path)

    empty = [row for row in rows if row["oi"] == ""]
    assert len(empty) == BARS + (CONTRACTS - 1)
    assert "0" not in {row["oi"] for row in empty}

    stored_nulls = store.connection.execute(
        "SELECT count(*) FROM candles WHERE oi IS NULL"
    ).fetchone()[0]
    assert len(empty) == stored_nulls


def test_the_csv_reproduces_every_stored_value_row_for_row(seeded):
    store, _, exports_dir = seeded
    result = export(store, exports_m.ExportSpec(format="csv"), exports_dir, export_id="c1")
    _, rows = csv_rows(result.path)
    rows.sort(key=lambda row: (int(row["contract_id"]), int(row["res_id"]), int(row["ts_utc_epoch"])))

    for stored, exported in zip(store_rows(store), rows):
        contract_id, res_id, ts, open_, high, low, close, volume, oi = stored
        assert int(exported["contract_id"]) == contract_id
        assert int(exported["res_id"]) == res_id
        assert exported["ts_ist"] == ts.strftime("%Y-%m-%d %H:%M:%S")
        assert int(exported["ts_utc_epoch"]) == utc_epoch_for_ist(ts)
        assert [Decimal(exported[name]) for name in ("open", "high", "low", "close")] == [
            open_,
            high,
            low,
            close,
        ]
        assert int(exported["volume"]) == volume
        assert (None if exported["oi"] == "" else int(exported["oi"])) == oi


def test_the_csv_header_names_every_denormalised_column(seeded):
    store, _, exports_dir = seeded
    result = export(store, exports_m.ExportSpec(format="csv"), exports_dir, export_id="c1")
    header, _ = csv_rows(result.path)
    assert header == [
        "symbol",
        "underlying_symbol",
        "underlying_name",
        "kind",
        "instrument_class",
        "exchange",
        "expiry_date",
        "strike",
        "option_type",
        "lot_size",
        "resolution",
        "ts_ist",
        "ts_utc_epoch",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "oi",
        "contract_id",
        "res_id",
    ]


# -- the hive archive ------------------------------------------------------------------------


def test_the_hive_archive_reads_back_as_a_partitioned_dataset_with_its_catalog(seeded):
    """The partition columns live in the directory names, so the read has to give them back."""
    store, _, exports_dir = seeded
    spec = exports_m.ExportSpec(layout="hive", scope=exports_m.ExportScope(include_catalog=True))
    result = export(store, spec, exports_dir, export_id="arch")

    glob = str(result.path / "candles" / "**" / "*.parquet")
    con = duckdb.connect()
    try:
        rows = con.execute(
            f"SELECT underlying_symbol, expiry_date, count(*), "
            f"       count(oi), min(ts_utc_epoch), max(ts_utc_epoch) "
            f"  FROM read_parquet('{glob}', hive_partitioning = true) "
            " GROUP BY 1, 2"
        ).fetchall()
        described = dict(
            (row[0], row[1])
            for row in con.execute(
                f"DESCRIBE SELECT * FROM read_parquet('{glob}', hive_partitioning = true)"
            ).fetchall()
        )
        catalog = {
            table: con.execute(
                f"SELECT count(*) FROM read_parquet('{result.path / (table + '.parquet')}')"
            ).fetchone()[0]
            for table in exports_m.CATALOG_TABLES
        }
    finally:
        con.close()

    assert rows == [
        (
            "NSE:NIFTY50-INDEX",
            EXPIRY,
            TOTAL_ROWS,
            TOTAL_ROWS - (BARS + CONTRACTS - 1),
            utc_epoch_for_ist(bar_time(0)),
            utc_epoch_for_ist(bar_time(BARS - 1)),
        )
    ]
    assert described["ts_utc_epoch"] == "BIGINT"
    assert described["open"] == "DECIMAL(11,4)"
    assert catalog["dim_underlying"] == 1
    assert catalog["dim_contract"] == CONTRACTS + 1
    assert catalog["dim_expiry"] == 1
    assert catalog["dim_resolution"] > 0


def test_the_archive_manifest_records_the_scope_and_the_row_count(seeded):
    store, _, exports_dir = seeded
    spec = exports_m.ExportSpec(
        layout="hive",
        scope=exports_m.ExportScope(include_catalog=True, underlying_id=1, option_type="CE"),
    )
    result = export(store, spec, exports_dir, export_id="arch2")
    manifest = json.loads((result.path / "manifest.json").read_text(encoding="utf-8"))
    schema = json.loads((result.path / "schema.json").read_text(encoding="utf-8"))

    assert manifest["export_id"] == "arch2"
    assert manifest["row_count"] == result.row_count == len(STRIKES) * BARS
    assert manifest["scope"]["underlying_id"] == 1
    assert manifest["scope"]["option_type"] == "CE"
    assert schema["partitioned_by"] == list(exports_m.HIVE_PARTITION)
    assert schema["catalog_tables"] == list(exports_m.CATALOG_TABLES)
    assert schema["row_group_size"] == exports_m.ARCHIVE_ROW_GROUP
    assert result.sha256 is None
    assert result.byte_size == sum(path.stat().st_size for path in result.files)


# -- scoping ---------------------------------------------------------------------------------


def test_a_timestamp_scope_uses_a_half_open_right_edge(seeded):
    """ts_to excludes its own instant, which is the boundary every off by one lands on."""
    store, _, exports_dir = seeded
    spec = exports_m.ExportSpec(
        format="csv",
        scope=exports_m.ExportScope(ts_from=bar_time(1), ts_to=bar_time(3)),
    )
    result = export(store, spec, exports_dir, export_id="win")
    _, rows = csv_rows(result.path)

    assert result.row_count == CONTRACTS * 2
    assert sorted({int(row["ts_utc_epoch"]) for row in rows}) == [
        utc_epoch_for_ist(bar_time(1)),
        utc_epoch_for_ist(bar_time(2)),
    ]


def test_a_contract_scope_writes_exactly_that_contract(seeded):
    store, ids, exports_dir = seeded
    wanted = sorted(ids.values())[1]
    spec = exports_m.ExportSpec(scope=exports_m.ExportScope(contract_ids=(wanted,)))
    result = export(store, spec, exports_dir, export_id="one")
    written = read_file(
        result.path, "SELECT DISTINCT contract_id FROM read_parquet('{file}')"
    )
    assert written == [(wanted,)]
    assert result.row_count == BARS


def test_a_scope_that_matches_nothing_writes_a_readable_empty_file(seeded):
    """Zero rows is a legitimate answer. A reader still has to be able to open the result."""
    store, _, exports_dir = seeded
    spec = exports_m.ExportSpec(scope=exports_m.ExportScope(option_type="XX"))
    result = export(store, spec, exports_dir, export_id="none")

    assert result.row_count == 0
    assert result.path.exists()
    described = read_file(result.path, "DESCRIBE SELECT * FROM read_parquet('{file}')")
    assert "ts_utc_epoch" in {row[0] for row in described}
    assert read_file(result.path, "SELECT count(*) FROM read_parquet('{file}')") == [(0,)]


# -- failure and replacement -----------------------------------------------------------------


def test_a_failed_export_leaves_no_finished_file_and_the_next_run_recovers(seeded, monkeypatch):
    """The documented promise: a partial temp file, never a truncated file that looks finished."""
    store, _, exports_dir = seeded
    exports_dir.mkdir(parents=True, exist_ok=True)

    real = exports_m._copy_options
    monkeypatch.setattr(exports_m, "_copy_options", lambda spec, *, partitioned: "FORMAT NOSUCH")
    with pytest.raises(duckdb.Error):
        export(store, exports_m.ExportSpec(), exports_dir, export_id="broken")
    assert not (exports_dir / "broken.parquet").exists()
    assert not (exports_dir / "broken.parquet.schema.json").exists()

    monkeypatch.setattr(exports_m, "_copy_options", real)
    result = export(store, exports_m.ExportSpec(), exports_dir, export_id="broken")
    assert result.row_count == TOTAL_ROWS
    assert [path.name for path in exports_dir.iterdir() if path.name.startswith(".")] == []
    assert read_file(result.path, "SELECT count(*) FROM read_parquet('{file}')") == [(TOTAL_ROWS,)]


def test_rerunning_an_export_id_replaces_the_data_and_the_sidecar_together(seeded):
    store, _, exports_dir = seeded
    first = export(store, exports_m.ExportSpec(), exports_dir, export_id="same")
    sidecar = first.path.with_name(first.path.name + ".schema.json")
    assert json.loads(sidecar.read_text(encoding="utf-8"))["row_count"] == TOTAL_ROWS

    second = export(
        store,
        exports_m.ExportSpec(scope=exports_m.ExportScope(option_type="PE")),
        exports_dir,
        export_id="same",
    )
    assert second.path == first.path
    assert read_file(second.path, "SELECT count(*) FROM read_parquet('{file}')") == [
        (len(STRIKES) * BARS,)
    ]
    assert json.loads(sidecar.read_text(encoding="utf-8"))["row_count"] == len(STRIKES) * BARS


def test_a_hive_export_replaces_a_single_file_export_of_the_same_id(seeded):
    """The two layouts use different names for the same id, so the stale one has to go."""
    store, _, exports_dir = seeded
    single = export(store, exports_m.ExportSpec(), exports_dir, export_id="swap")
    assert single.path.is_file()

    archive = export(
        store, exports_m.ExportSpec(layout="hive"), exports_dir, export_id="swap"
    )
    assert archive.path.is_dir()
    assert single.path.exists()
    assert [path.name for path in exports_dir.iterdir() if path.name.startswith(".")] == []


def test_the_disk_guard_refuses_before_anything_is_written(seeded, monkeypatch):
    store, _, exports_dir = seeded
    monkeypatch.setattr(exports_m, "BYTES_PER_ROW", {"parquet": 10**15, "csv": 10**15})
    with pytest.raises(exports_m.InsufficientDisk) as raised:
        export(store, exports_m.ExportSpec(), exports_dir, export_id="huge")
    assert raised.value.needed > raised.value.free
    assert not (exports_dir / "huge.parquet").exists()
    assert not (exports_dir / ".huge.parquet.partial").exists()
